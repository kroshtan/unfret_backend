"""
"Spotted recently": real checks of real product pages, run by the operator and published only after review.

**Where a case comes from.** Always a check the operator ran: ``python -m veridex.showcase add URL`` (or promoting a
labelled report in ``make reports``, which sends only the page's address) has the server fetch the public page itself
and run the full analysis on it, exactly as a paid URL-only check would, without any account. A user's own check is
never shown: nothing from the user-check paths (results, reports, their accounts) is read here, and the table has no
column that could hold an account, a payer or a report.

**What is stored.** The product title, the shop's host, the product URL the operator gave, when it was checked, the
analysis response as a paying user gets it (score, explanation, assessment, verdict; without balance or report
token, which belong to an account) and optionally a screenshot (in the row, at most :data:`MAX_SCREENSHOT_BYTES`).

**Review.** A case is a ``draft`` until the owner publishes it; ``removed`` takes it off the site for good (with the
reason, and the screenshot is dropped). Only published cases are ever served.

**What the public sees** (:func:`public_view`): facts, not accusations. The band keeps its symbol but is worded as
what Unfret would tell a shopper about this listing (:data:`SHOWCASE_BANDS`); the score number is not shown, and
neither "scam" nor "fraud" appears anywhere: a sentence or a quote that contains one is left out (the band's
neutral headline replaces such a headline), and a case whose address contains one can't be published.
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import structlog

from veridex import page_fetch, shopper
from veridex.facts.schema import PageFacts
from veridex.schemas.responses import AnalyzeResponse

logger = structlog.get_logger("veridex")

Origin = Literal["operator", "report"]
Status = Literal["draft", "published", "removed"]
PublicBand = Literal["reliable", "concerns", "careful", "warning"]

MAX_SCREENSHOT_BYTES = 800_000
MAX_TITLE_CHARS = 300
MAX_URL_CHARS = 2000
LANGUAGE = "en"  # the site's pages are in English

# Per verdict band: the public band name, its symbol (the same as the extension and /check) and a sentence about
# the listing, not the shop. The worst band's internal name is never shown.
SHOWCASE_BANDS: dict[shopper.Band, tuple[PublicBand, str, str]] = {
    "reliable": ("reliable", "✓", "Unfret found nothing worrying on this listing"),
    "concerns": ("concerns", "!", "Unfret would suggest a closer look at this listing"),
    "careful": ("careful", "⚠", "Unfret would point out warning signs on this listing"),
    "junk_or_scam": ("warning", "✕", "Unfret would warn you about this listing"),
}
# Words the showcase never uses: it states what a page showed, it doesn't call a shop names.
FORBIDDEN_RE = re.compile(r"scam|fraud", re.IGNORECASE)

_COLUMNS = (
    "id, origin, title, host, product_url, checked_at, response, screenshot_type, octet_length(screenshot) AS "
    "screenshot_bytes, status, published_at, removed_at, removed_reason"
)
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


class ShowcaseError(Exception):
    """A showcase action that can't be done; the message says why (shown to the operator)."""


class NotAssessedError(ShowcaseError):
    """The analysis didn't assess the page (score -1): not a product page or a shop."""


@dataclass(frozen=True)
class Analysed:
    """An operator-run check, ready to be stored as a draft."""

    title: str
    host: str
    product_url: str
    checked_at: datetime
    response: dict[str, Any] = field(repr=False)


def clean_url(url: str) -> str:
    """
    The product URL to store and link to: the operator's address without its fragment.

    :param url: The address the operator gave.
    :return: The address.
    :raises ShowcaseError: If it isn't an absolute http(s) address of a reasonable length.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ShowcaseError("Give the product page's full address (https://…).")
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    if len(cleaned) > MAX_URL_CHARS:
        raise ShowcaseError("That address is too long.")
    return cleaned


def host_of(url: str) -> str:
    """
    The shop or marketplace host shown with a case, without ``www.``.

    :param url: The product URL.
    :return: The host, lower case.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


async def analyse(graph: Any, url: str, *, timeout_s: float, title: str | None = None) -> Analysed:
    """
    Fetch a public product page and run the full analysis on it, as the operator (no account, nothing charged).

    The same path as a URL-only check: :func:`veridex.page_fetch.fetch_page`, then the deep graph, answered in
    English. Nothing about the URL is logged. An address that isn't a public web page raises
    :class:`ShowcaseError` (from :func:`clean_url`) or :class:`~veridex.page_fetch.PageUnreadableError`.

    :param graph: The compiled analysis graph.
    :param url: The product page.
    :param timeout_s: How long the analysis may take.
    :param title: The title to show; the page's own product title when not given.
    :return: The check.
    :raises NotAssessedError: If the analysis didn't assess the page (not a product page or a shop).
    """
    product_url = clean_url(url)
    html = await page_fetch.fetch_page(product_url)
    state: dict[str, object] = {"page_content": html, "url": product_url, "flags": [], "language": LANGUAGE}
    async with asyncio.timeout(timeout_s):
        result: dict[str, Any] = await graph.ainvoke(state)
    if int(result["score"]) < 0:
        raise NotAssessedError("Unfret didn't assess this page (not a product page or a shop), so there's no case.")
    response = AnalyzeResponse(
        score=result["score"],
        explanation=result["explanation"],
        assessment=result.get("assessment"),
        verdict=result.get("verdict"),
    ).model_dump(mode="json", exclude_none=True)
    facts = result.get("facts")
    page_title = facts.title if isinstance(facts, PageFacts) else ""
    host = host_of(product_url)
    chosen = " ".join((title or page_title or host).split())[:MAX_TITLE_CHARS]
    logger.info("showcase_checked", score=response["score"])
    return Analysed(chosen or host, host, product_url, datetime.now(UTC), response)


async def add(pool: asyncpg.Pool, analysed: Analysed, origin: Origin) -> int:
    """
    Store a check as a draft case.

    :param pool: The database pool.
    :param analysed: The check.
    :param origin: ``operator`` (an address the operator gave) or ``report`` (a labelled report's page, re-checked).
    :return: The case's id.
    """
    case_id: int = await pool.fetchval(
        "INSERT INTO showcase_cases (origin, title, host, product_url, checked_at, response) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING id",
        origin,
        analysed.title,
        analysed.host,
        analysed.product_url,
        analysed.checked_at,
        json.dumps(analysed.response, ensure_ascii=False),
    )
    logger.info("showcase_draft_added", case=case_id, origin=origin)
    return case_id


def _row(record: asyncpg.Record) -> dict[str, Any]:
    row = dict(record)
    if isinstance(row["response"], str):
        row["response"] = json.loads(row["response"])
    return row


async def cases(pool: asyncpg.Pool, status: Status | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """
    Cases for the operator, newest first.

    :param pool: The database pool.
    :param status: Only cases with this status; all when ``None``.
    :param limit: How many.
    :return: The rows (without the screenshot's bytes).
    """
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM showcase_cases WHERE $1::text IS NULL OR status = $1 "  # noqa: S608 - fixed columns
        "ORDER BY id DESC LIMIT $2",
        status,
        limit,
    )
    return [_row(r) for r in rows]


async def get(pool: asyncpg.Pool, case_id: int) -> dict[str, Any] | None:
    """
    One case, whatever its status (the operator's view).

    :param pool: The database pool.
    :param case_id: The case.
    :return: The row (without the screenshot's bytes), or ``None``.
    """
    row = await pool.fetchrow(f"SELECT {_COLUMNS} FROM showcase_cases WHERE id = $1", case_id)  # noqa: S608
    return None if row is None else _row(row)


async def publish(pool: asyncpg.Pool, case_id: int, now: datetime | None = None) -> dict[str, Any]:
    """
    Publish a draft: from now on it is on the site.

    :param pool: The database pool.
    :param case_id: The case.
    :param now: Current time (injectable for tests).
    :return: The published row.
    :raises ShowcaseError: If there is no such draft, or its address contains a word the showcase never shows.
    """
    row = await get(pool, case_id)
    if row is None or row["status"] != "draft":
        raise ShowcaseError("Only a draft can be published." if row else "No such case.")
    if FORBIDDEN_RE.search(row["product_url"]):
        raise ShowcaseError("The product address contains a word the showcase never shows; remove this case.")
    await pool.execute(
        "UPDATE showcase_cases SET status = 'published', published_at = $2 WHERE id = $1 AND status = 'draft'",
        case_id,
        now or datetime.now(UTC),
    )
    logger.info("showcase_published", case=case_id)
    published = await get(pool, case_id)
    assert published is not None  # noqa: S101 - just updated
    return published


async def remove(pool: asyncpg.Pool, case_id: int, reason: str, now: datetime | None = None) -> bool:
    """
    Take a case off the site for good (a draft that won't be published, or a published one), dropping its screenshot.

    :param pool: The database pool.
    :param case_id: The case.
    :param reason: Why, for the record (e.g. the shop owner's request).
    :param now: Current time (injectable for tests).
    :return: Whether a case was removed (``False`` if there is none or it was removed already).
    """
    status = await pool.execute(
        "UPDATE showcase_cases SET status = 'removed', removed_at = $2, removed_reason = $3, screenshot = NULL, "
        "screenshot_type = NULL WHERE id = $1 AND status <> 'removed'",
        case_id,
        now or datetime.now(UTC),
        reason.strip()[:500],
    )
    removed = str(status).endswith(" 1")
    if removed:
        logger.info("showcase_removed", case=case_id)
    return removed


async def set_title(pool: asyncpg.Pool, case_id: int, title: str) -> bool:
    """
    Change the title shown with a case that isn't removed.

    :param pool: The database pool.
    :param case_id: The case.
    :param title: The new title.
    :return: Whether a case was changed.
    :raises ShowcaseError: For an empty title.
    """
    text = " ".join(title.split())[:MAX_TITLE_CHARS]
    if not text:
        raise ShowcaseError("Give a title.")
    status = await pool.execute(
        "UPDATE showcase_cases SET title = $2 WHERE id = $1 AND status <> 'removed'", case_id, text
    )
    return str(status).endswith(" 1")


def image_type(data: bytes) -> str | None:
    """
    The type of a screenshot from its first bytes: PNG, JPEG or WebP.

    :param data: The image.
    :return: Its media type, or ``None`` for anything else.
    """
    for magic, kind in _IMAGE_MAGIC:
        if data.startswith(magic):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def set_screenshot(pool: asyncpg.Pool, case_id: int, data: bytes) -> str:
    """
    Attach a screenshot to a case that isn't removed (replacing any earlier one).

    :param pool: The database pool.
    :param case_id: The case.
    :param data: A PNG, JPEG or WebP image of at most :data:`MAX_SCREENSHOT_BYTES`.
    :return: Its media type.
    :raises ShowcaseError: For another kind of file, one too large, or no such case.
    """
    if len(data) > MAX_SCREENSHOT_BYTES:
        raise ShowcaseError(f"The screenshot is larger than {MAX_SCREENSHOT_BYTES // 1000} kB; shrink it first.")
    kind = image_type(data)
    if kind is None:
        raise ShowcaseError("The screenshot must be a PNG, JPEG or WebP image.")
    status = await pool.execute(
        "UPDATE showcase_cases SET screenshot = $2, screenshot_type = $3 WHERE id = $1 AND status <> 'removed'",
        case_id,
        data,
        kind,
    )
    if not str(status).endswith(" 1"):
        raise ShowcaseError("No such case (or it was removed).")
    return kind


# ── Public ─────────────────────────────────────────────────────────────────────────────────────────────────────────


async def published(pool: asyncpg.Pool, limit: int) -> list[dict[str, Any]]:
    """
    The published cases, newest first.

    :param pool: The database pool.
    :param limit: How many.
    :return: The rows.
    """
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM showcase_cases WHERE status = 'published' "  # noqa: S608 - fixed columns
        "ORDER BY published_at DESC, id DESC LIMIT $1",
        limit,
    )
    return [_row(r) for r in rows]


async def published_case(pool: asyncpg.Pool, case_id: int) -> dict[str, Any] | None:
    """
    One case, only if it is published.

    :param pool: The database pool.
    :param case_id: The case.
    :return: The row, or ``None`` for a draft, a removed case or none at all.
    """
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM showcase_cases WHERE id = $1 AND status = 'published'",  # noqa: S608
        case_id,
    )
    return None if row is None else _row(row)


async def screenshot(pool: asyncpg.Pool, case_id: int) -> tuple[bytes, str] | None:
    """
    A published case's screenshot.

    :param pool: The database pool.
    :param case_id: The case.
    :return: The image and its media type, or ``None``.
    """
    row = await pool.fetchrow(
        "SELECT screenshot, screenshot_type FROM showcase_cases "
        "WHERE id = $1 AND status = 'published' AND screenshot IS NOT NULL",
        case_id,
    )
    return None if row is None else (bytes(row["screenshot"]), str(row["screenshot_type"]))


def _clean(text: Any) -> str | None:
    value = str(text or "")
    return None if FORBIDDEN_RE.search(value) else value


def checked_on(when: datetime) -> str:
    """
    A day as the site writes it.

    :param when: The moment.
    :return: E.g. ``9 November 2026``.
    """
    return f"{when.day} {when:%B %Y}"


def _items(kind: str, entries: list[dict[str, Any]], notes: list[str]) -> list[dict[str, Any]]:
    out = []
    for entry in entries:
        title = _clean(entry.get("title"))
        if not title:
            notes.append(f"{kind} {entry.get('type', '')}".strip())
            continue
        detail = _clean(entry.get("detail"))
        if detail is None:
            notes.append(f"{kind} detail: {title}")
        item: dict[str, Any] = {"title": title, "detail": detail or ""}
        if kind == "reason":
            item |= {"severity": entry.get("severity", "low"), "thumbnails": list(entry.get("thumbnails", []))}
        out.append(item)
    return out


def _flags(flags: list[dict[str, Any]], notes: list[str]) -> list[dict[str, Any]]:
    out = []
    for flag in flags:
        label = _clean(flag.get("label"))
        if not label:
            notes.append("detail flag")
            continue
        evidence = _clean(flag.get("evidence"))
        if evidence is None:
            notes.append(f"detail evidence: {label}")
        out.append({"label": label, "evidence": evidence or "", "kind": flag.get("kind", "signal")})
    return out


def public_result(response: dict[str, Any], left_out: list[str] | None = None) -> dict[str, Any]:
    """
    The analysis response as the showcase shows it: shaped like a /check result, worded about the listing.

    The band's label and symbol come from :data:`SHOWCASE_BANDS`; reasons, the things that look good and the
    details are kept as the user saw them, except any text containing a forbidden word (a reason or flag whose
    title does is left out; a detail or quote that does is dropped, keeping the title). No score, explanation,
    feature names or weights.

    :param response: The stored analysis response.
    :param left_out: Collects what was left out, for the operator.
    :return: ``{"band", "verdict": {"band", "label", "symbol", "headline", "reasons", "positives"},
        "assessment": {"flags", "confidence", "model_version"}}``.
    """
    notes = left_out if left_out is not None else []
    verdict = response.get("verdict") or {}
    internal: shopper.Band = verdict.get("band") or shopper.band_for(int(response.get("score", 0)))
    band, symbol, label = SHOWCASE_BANDS[internal]
    headline = _clean(verdict.get("headline"))
    if not headline:
        notes.append("headline")
        headline = _clean(shopper.copy_for(LANGUAGE)["headlines"][internal]) or label + "."
    assessment = response.get("assessment") or {}
    flags = _flags(assessment.get("flags") or [], notes)
    shown: dict[str, Any] = {
        "band": band,
        "verdict": {
            "band": band,
            "label": label,
            "symbol": symbol,
            "headline": headline,
            "reasons": _items("reason", verdict.get("reasons") or [], notes),
            "positives": _items("positive", verdict.get("positives") or [], notes),
        },
    }
    if flags:
        shown["assessment"] = {
            "flags": flags,
            "confidence": assessment.get("confidence"),
            "model_version": _clean(assessment.get("model_version")) or "",
        }
    return shown


def public_view(row: dict[str, Any], left_out: list[str] | None = None) -> dict[str, Any]:
    """
    A case as the public sees it (``GET /v1/showcase``, the case page).

    :param row: The case.
    :param left_out: Collects what was left out of the result, for the operator.
    :return: The case's public fields and its :func:`public_result`.
    """
    notes = left_out if left_out is not None else []
    title = _clean(row["title"])
    if not title:
        notes.append("title")
    result = public_result(row["response"], notes)
    verdict = result["verdict"]
    case_id = int(row["id"])
    return {
        "id": case_id,
        "title": title or row["host"],
        "host": row["host"],
        "product_url": row["product_url"],
        "checked_at": row["checked_at"],
        "checked_on": checked_on(row["checked_at"]),
        "published_at": row["published_at"],
        "band": verdict["band"],
        "label": verdict["label"],
        "symbol": verdict["symbol"],
        "headline": verdict["headline"],
        "page_url": f"/showcase/{case_id}",
        "screenshot_url": f"/showcase/{case_id}/screenshot" if row.get("screenshot_type") else None,
        "result": result,
    }
