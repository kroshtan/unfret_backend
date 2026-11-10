"""
Shared results: a shopper makes their own check public, as a link, and nothing on it says who they are.

**The check behind a share.** Every assessed analysis result carries a ``share_token``: an HMAC over the product's
address (:func:`share_url`), a hash of the result exactly as it would be shown (:func:`result_hash`), whether the
check included the shopper's checkout page, the product title and an expiry (``share_token_ttl_s``, 7 days). It
names no account: nothing is stored when a check runs, and the token can't link a share to whoever paid for it.
``POST /v1/shares`` takes the token back with the address and the result and refuses anything that doesn't match,
so a share can only show a verdict Unfret really gave for that page. It is sent without ``Authorization``.

**What is public.** The result in the showcase's wording (:func:`unfret.showcase.public_result`: the band as what
Unfret would tell a shopper about the listing, no score, never "scam" or "fraud"), without the page's own words:
a reason whose detail quotes the page keeps only its title, a headline that quotes the page is replaced by the
band's catalogue headline, and the details keep their labels but lose their evidence. A check that included the
shopper's checkout page is never shared. The page says "Checked by an Unfret user on <day>".

**What is stored** (``shared_results``, migration 16): the id (128 random bits), the shop's host, the product
address (without fragment, credentials and tracking parameters), the title, the day of the check, that public
result, a SHA-256 hash of the delete code (the code itself is shown once), when it expires (90 days, a day boundary)
and, after an objection, why it is hidden. No account, wallet, IP address, time of day or view count. Expired
shares are deleted by the hourly housekeeping.

**Taking one down.** The sharer deletes it with the delete code. A shop owner's objection hides it at once; the
operator then reviews it (``/v1/admin/shares``): unhide or delete.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import structlog

from unfret import result_cache, shopper, showcase

logger = structlog.get_logger("unfret")

ID_RE = re.compile(r"^[a-z2-7]{26}$")
MAX_TITLE_CHARS = 200
MAX_OBJECTION_CHARS = 500
LANGUAGE = showcase.LANGUAGE

# A quotation of the page: “…” (how the verdict quotes it), "…", „…“ and «…».
_QUOTE_RE = re.compile(r"“[^”]*”|\"[^\"]*\"|„[^“”]*[“”]|«[^»]*»")
_INTERNAL_BAND = {public: internal for internal, (public, _, _) in showcase.SHOWCASE_BANDS.items()}
_COLUMNS = "id, host, product_url, title, checked_on, result, expires_at, hidden_reason"

DeleteOutcome = Literal["deleted", "wrong_code", "missing"]


class ShareError(Exception):
    """A share that can't be made; ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        """
        Describe the refusal.

        :param code: ``BAD_SHARE_TOKEN``, ``SHARE_TOKEN_EXPIRED``, ``CHECKOUT_NOT_SHAREABLE`` or ``NOT_SHAREABLE``.
        :param detail: A friendly message.
        """
        super().__init__(detail)
        self.code, self.detail = code, detail


def share_url(url: str) -> str:
    """
    The product address a share links to: no fragment, no user name or password, no tracking parameters.

    :param url: The address that was checked.
    :return: The address.
    :raises ShareError: ``NOT_SHAREABLE`` if it isn't an absolute http(s) address of a reasonable length.
    """
    try:
        parts = urlsplit(showcase.clean_url(url))
        port = parts.port
    except (showcase.ShowcaseError, ValueError) as exc:
        raise ShareError("NOT_SHAREABLE", "Only a check of a web page's address can be shared.") from exc
    netloc = (parts.hostname or "") + (f":{port}" if port else "")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not result_cache.is_tracking_param(k)]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _canonical(value: Any) -> Any:
    # Numbers as floats: a browser writes 1.0 as 1, and the hash must not depend on who serialised the result.
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return str(value)


def result_hash(response: dict[str, Any]) -> str:
    """
    A hash of a result as the showcase wording shows it (what a share is made from).

    :param response: The analysis response (``score``, ``verdict``, ``assessment``; other fields are ignored).
    :return: 32 hex characters.
    """
    shown = _canonical(showcase.public_result(response))
    text = json.dumps(shown, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _mac(secret: str, product_url: str, payload: str) -> str:
    message = f"share:{product_url}\n{payload}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:32]


def _title(text: str) -> str:
    return " ".join(text.split())[:MAX_TITLE_CHARS]


def issue_token(
    secret: str,
    *,
    url: str,
    title: str,
    response: dict[str, Any],
    checkout: bool,
    ttl_s: int,
    now: float | None = None,
) -> str | None:
    """
    The token that lets whoever was shown this result share it (no account in it).

    :param secret: The session secret.
    :param url: The checked page's address.
    :param title: The product's title, as the page gave it.
    :param response: The analysis response, as the caller gets it.
    :param checkout: Whether the check included the shopper's checkout page (such a result is never shared).
    :param ttl_s: How long it may be shared.
    :param now: Current time (injectable for tests).
    :return: The token, or ``None`` for results that can't be shared (not assessed, no web address) or while the
        secret isn't set.
    """
    score = response.get("score")
    if not secret or not isinstance(score, int) or not 0 <= score <= 100:
        return None
    try:
        product_url = share_url(url)
    except ShareError:
        return None
    expires = int((time.time() if now is None else now) + ttl_s)
    payload = "|".join((result_hash(response), "1" if checkout else "0", str(expires), _title(title)))
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{encoded}.{_mac(secret, product_url, payload)}"


@dataclass(frozen=True)
class Claim:
    """What a valid share token vouches for."""

    product_url: str
    title: str
    checked_on: date


def check_token(
    secret: str, token: str, *, url: str, response: dict[str, Any], ttl_s: int, now: float | None = None
) -> Claim:
    """
    Verify a share token against the address and the result sent with it.

    :param secret: The session secret.
    :param token: The token from the result.
    :param url: The checked page's address, as sent back.
    :param response: The result, as sent back.
    :param ttl_s: The token lifetime it was issued with (the check's day is its expiry minus this).
    :param now: Current time (injectable for tests).
    :return: The product address, title and day of the check.
    :raises ShareError: ``BAD_SHARE_TOKEN`` for a forged token or one that doesn't match the address or result,
        ``SHARE_TOKEN_EXPIRED`` for an old one, ``CHECKOUT_NOT_SHAREABLE`` if the check included a checkout page.
    """
    try:
        product_url = share_url(url)
    except ShareError as exc:
        raise ShareError("BAD_SHARE_TOKEN", "This result can't be shared; check the page again.") from exc
    encoded, _, mac = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        digest, checkout, expires, title = payload.split("|", 3)
        valid = hmac.compare_digest(mac.encode(), _mac(secret, product_url, payload).encode())
        expires_at = int(expires)
    except ValueError:  # bad base64, not UTF-8, too few fields, not a number
        valid, digest, checkout, title, expires_at = False, "", "", "", 0
    if not valid or digest != result_hash(response):
        raise ShareError("BAD_SHARE_TOKEN", "This result doesn't match the check it claims to be.")
    if expires_at < (time.time() if now is None else now):
        raise ShareError("SHARE_TOKEN_EXPIRED", "This check is too old to share; check the page again.")
    if checkout != "0":
        raise ShareError(
            "CHECKOUT_NOT_SHAREABLE", "A check that included your checkout page can't be shared: it showed your cart."
        )
    return Claim(product_url, title, datetime.fromtimestamp(expires_at - ttl_s, UTC).date())


def public_share_result(response: dict[str, Any]) -> dict[str, Any]:
    """
    A result as a share shows it: the showcase wording, without the page's own words.

    :param response: The analysis response.
    :return: The :func:`~unfret.showcase.public_result` shape; a reason or positive whose detail quotes the page
        keeps only its title, a quoting headline becomes the band's catalogue headline, and flags lose their
        evidence (their labels stay).
    """
    shown = showcase.public_result(response)
    verdict = shown["verdict"]
    if _QUOTE_RE.search(verdict["headline"]):
        catalogue = str(shopper.copy_for(LANGUAGE)["headlines"][_INTERNAL_BAND[verdict["band"]]])
        verdict["headline"] = verdict["label"] + "." if showcase.FORBIDDEN_RE.search(catalogue) else catalogue
    for item in [*verdict["reasons"], *verdict["positives"]]:
        if _QUOTE_RE.search(item["detail"]):
            item["detail"] = ""
    for flag in shown.get("assessment", {}).get("flags", []):
        flag["evidence"] = ""
    return shown


def shown_title(title: str, host: str) -> str:
    """
    The title a share shows: the page's, unless empty or containing a word the showcase never uses.

    :param title: The product's title.
    :param host: The shop's host (the fallback).
    :return: The title.
    """
    text = _title(title)
    return text if text and not showcase.FORBIDDEN_RE.search(text) else host


def day_in_words(day: date) -> str:
    """
    A day as the site writes it.

    :param day: The day.
    :return: E.g. ``9 November 2026``.
    """
    return f"{day.day} {day:%B %Y}"


def preview(claim: Claim, response: dict[str, Any]) -> dict[str, Any]:
    """
    Exactly what a share would make public (nothing is stored).

    :param claim: The verified token.
    :param response: The result.
    :return: ``title``, ``host``, ``product_url``, ``checked_on``, ``checked_on_text``, the band's fields and
        ``result``.
    :raises ShareError: ``NOT_SHAREABLE`` if the address contains a word the showcase never shows.
    """
    if showcase.FORBIDDEN_RE.search(claim.product_url):
        raise ShareError("NOT_SHAREABLE", "This page's address can't be shown on Unfret's site.")
    host = showcase.host_of(claim.product_url)
    title = shown_title(claim.title, host)
    return _view(title, host, claim.product_url, claim.checked_on, public_share_result(response))


def _view(title: str, host: str, product_url: str, checked_on: date, result: dict[str, Any]) -> dict[str, Any]:
    verdict = result["verdict"]
    return {
        "title": title,
        "host": host,
        "product_url": product_url,
        "checked_on": checked_on,
        "checked_on_text": day_in_words(checked_on),
        "band": verdict["band"],
        "label": verdict["label"],
        "symbol": verdict["symbol"],
        "headline": verdict["headline"],
        "result": result,
    }


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """
    A stored share as the public sees it (``GET /v1/shares/{id}``, ``/r/{id}``).

    :param row: The share.
    :return: Its :func:`preview` fields plus ``id``, ``link`` and ``expires_at``.
    """
    view = _view(row["title"], row["host"], row["product_url"], row["checked_on"], row["result"])
    return {"id": row["id"], "link": f"/r/{row['id']}", "expires_at": row["expires_at"], **view}


# ── Storage ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _random_code() -> str:
    return base64.b32encode(secrets.token_bytes(16)).decode().rstrip("=").lower()  # 128 bits, 26 characters


def code_hash(code: str) -> bytes:
    """
    The stored form of a delete code.

    :param code: The code as the sharer typed it (case and surrounding spaces don't matter).
    :return: Its SHA-256 digest.
    """
    return hashlib.sha256(code.strip().lower().encode()).digest()


@dataclass(frozen=True)
class Created:
    """A new share: its id and the delete code, shown this once."""

    share_id: str
    delete_code: str
    expires_at: datetime


async def create(pool: asyncpg.Pool, view: dict[str, Any], *, days: int, now: datetime | None = None) -> Created:
    """
    Store a share.

    :param pool: The database pool.
    :param view: Its :func:`preview`.
    :param days: How long it stays up.
    :param now: Current time (injectable for tests).
    :return: The id and the delete code.
    """
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    # A day boundary, so the row doesn't keep the time the share was made.
    expires_at = datetime(today.year, today.month, today.day, tzinfo=UTC) + timedelta(days=days)
    share_id, delete_code = _random_code(), _random_code()
    await pool.execute(
        "INSERT INTO shared_results (id, host, product_url, title, checked_on, result, delete_hash, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)",
        share_id,
        view["host"],
        view["product_url"],
        view["title"],
        view["checked_on"],
        json.dumps(view["result"], ensure_ascii=False),
        code_hash(delete_code),
        expires_at,
    )
    logger.info("share_created")
    return Created(share_id, delete_code, expires_at)


def _row(record: asyncpg.Record) -> dict[str, Any]:
    row = dict(record)
    if isinstance(row["result"], str):
        row["result"] = json.loads(row["result"])
    return row


async def visible(pool: asyncpg.Pool, share_id: str) -> dict[str, Any] | None:
    """
    A share the public may see: not hidden, not expired.

    :param pool: The database pool.
    :param share_id: The id.
    :return: The row, or ``None``.
    """
    if not ID_RE.match(share_id):
        return None
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM shared_results "  # noqa: S608 - fixed columns
        "WHERE id = $1 AND hidden_reason IS NULL AND expires_at > NOW()",
        share_id,
    )
    return None if row is None else _row(row)


async def delete(pool: asyncpg.Pool, share_id: str, code: str) -> DeleteOutcome:
    """
    Delete a share with its delete code (hidden ones too).

    :param pool: The database pool.
    :param share_id: The id.
    :param code: The delete code.
    :return: ``deleted``, ``wrong_code`` or ``missing`` (no such share, or it expired).
    """
    if not ID_RE.match(share_id):
        return "missing"
    stored = await pool.fetchval(
        "SELECT delete_hash FROM shared_results WHERE id = $1 AND expires_at > NOW()", share_id
    )
    if stored is None:
        return "missing"
    if not hmac.compare_digest(bytes(stored), code_hash(code)):
        return "wrong_code"
    await pool.execute("DELETE FROM shared_results WHERE id = $1", share_id)
    logger.info("share_deleted", by="sharer")
    return "deleted"


async def hide(pool: asyncpg.Pool, share_id: str, reason: str) -> bool:
    """
    Hide a visible share at once, for the operator to review (a shop owner's objection).

    :param pool: The database pool.
    :param share_id: The id.
    :param reason: Why, as the objector put it (may be empty).
    :return: Whether a visible share was hidden.
    """
    if not ID_RE.match(share_id):
        return False
    text = " ".join(reason.split())[:MAX_OBJECTION_CHARS]
    status = await pool.execute(
        "UPDATE shared_results SET hidden_reason = $2 WHERE id = $1 AND hidden_reason IS NULL AND expires_at > NOW()",
        share_id,
        f"Objection: {text}" if text else "Objection (no reason given)",
    )
    hidden = str(status).endswith(" 1")
    if hidden:
        logger.info("share_hidden", by="objection")
    return hidden


async def listing(pool: asyncpg.Pool, *, hidden_only: bool, limit: int) -> list[dict[str, Any]]:
    """
    Shares for the operator, hidden ones (awaiting review) first.

    :param pool: The database pool.
    :param hidden_only: Only hidden shares.
    :param limit: How many.
    :return: The rows (without the delete hash).
    """
    rows = await pool.fetch(
        f"SELECT {_COLUMNS} FROM shared_results "  # noqa: S608 - fixed columns
        "WHERE NOT $1::boolean OR hidden_reason IS NOT NULL "
        "ORDER BY hidden_reason IS NULL, expires_at DESC, id LIMIT $2",
        hidden_only,
        limit,
    )
    return [_row(r) for r in rows]


async def unhide(pool: asyncpg.Pool, share_id: str) -> bool:
    """
    Put a hidden share back after review.

    :param pool: The database pool.
    :param share_id: The id.
    :return: Whether a hidden share was restored.
    """
    status = await pool.execute(
        "UPDATE shared_results SET hidden_reason = NULL WHERE id = $1 AND hidden_reason IS NOT NULL", share_id
    )
    return str(status).endswith(" 1")


async def remove(pool: asyncpg.Pool, share_id: str) -> bool:
    """
    Delete a share for good (the operator, after review).

    :param pool: The database pool.
    :param share_id: The id.
    :return: Whether there was one.
    """
    status = await pool.execute("DELETE FROM shared_results WHERE id = $1", share_id)
    removed = str(status).endswith(" 1")
    if removed:
        logger.info("share_deleted", by="operator")
    return removed


async def purge(pool: asyncpg.Pool, now: datetime | None = None) -> int:
    """
    Delete expired shares (housekeeping).

    :param pool: The database pool.
    :param now: Current time (injectable for tests).
    :return: How many were deleted.
    """
    status = await pool.execute("DELETE FROM shared_results WHERE expires_at <= $1", now or datetime.now(UTC))
    return int(str(status).rsplit(" ", 1)[-1] or 0)
