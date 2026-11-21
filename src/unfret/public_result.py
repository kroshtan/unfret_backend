"""
A result as the public sees it: on a shared result's page (:mod:`unfret.shares`), never a shopper's own screen.

Facts, not accusations. The band keeps its symbol but is worded as what Unfret would tell a shopper about this
listing (:data:`PUBLIC_BANDS`); the score number is not shown, and neither "scam" nor "fraud" appears anywhere: a
sentence or a quote that contains one is left out (the band's neutral headline replaces such a headline).
"""

import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from unfret import card as result_card
from unfret import shopper

PublicBand = Literal["reliable", "concerns", "careful", "warning"]

MAX_URL_CHARS = 2000

# Per verdict band: the public band name, its symbol (the same as the extension and /check) and a sentence about
# the listing, not the shop. The worst band's internal name is never shown.
PUBLIC_BANDS: dict[shopper.Band, tuple[PublicBand, str, str]] = {
    "reliable": ("reliable", "✓", "Unfret found nothing worrying on this listing"),
    "concerns": ("concerns", "!", "Unfret would suggest a closer look at this listing"),
    "careful": ("careful", "⚠", "Unfret would point out warning signs on this listing"),
    "junk_or_scam": ("warning", "✕", "Unfret would warn you about this listing"),
}
# Words a public result never uses: it states what a page showed, it doesn't call a shop names.
FORBIDDEN_RE = re.compile(r"scam|fraud", re.IGNORECASE)
# The only links a reason may carry: archived copies of the shop's address (domain_history), in the Internet Archive
# or as their record in Common Crawl's index.
_ARCHIVE_PREFIX = ("https://web.archive.org/web/", "https://index.commoncrawl.org/CC-MAIN-")


def clean_url(url: str) -> str:
    """
    A product address to show and link to: the address without its fragment.

    :param url: The address that was checked.
    :return: The address.
    :raises ValueError: If it isn't an absolute http(s) address of a reasonable length.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("not an absolute http(s) address")
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    if len(cleaned) > MAX_URL_CHARS:
        raise ValueError("address too long")
    return cleaned


def host_of(url: str) -> str:
    """
    The shop or marketplace host shown with a result, without ``www.``.

    :param url: The product URL.
    :return: The host, lower case.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _clean(text: Any) -> str | None:
    value = str(text or "")
    return None if FORBIDDEN_RE.search(value) else value


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
        if source := _clean(entry.get("source")):
            item["source"] = source  # only when the result has one, so older results hash as before
        if links := [str(u) for u in entry.get("links") or [] if str(u).startswith(_ARCHIVE_PREFIX)][:3]:
            item["links"] = links  # likewise: only when there are any
        out.append(item)
    return out


_SAME_PRODUCT_TEXT = ("title", "detail", "where", "domain", "price_text")
_SAME_PRODUCT_KEPT = ("others", "price", "currency", "price_seen_on", "image", "own_image")


def _card(verdict: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    """
    The result card's fields of a verdict, in public wording; only those the verdict has (older results have none).

    The checklist, the number of checks, the next step and the line about a step that didn't finish (without what
    the check cost, which is the payer's business) come from the catalogue. The same product elsewhere names
    another shop's domain, its price and the two thumbnails; it is left out whole when any of its words is one a
    public result never uses.
    """
    card: dict[str, Any] = {}
    checklist = [
        {
            "key": str(i.get("key", "")),
            "label": label,
            "status": i.get("status", "checked"),
            "note": _clean(i.get("note")) or "",
            # Only when the result has them (an older one doesn't), so older results hash as before.
            **({"checks": i["checks"]} if isinstance(i.get("checks"), int) else {}),
        }
        for i in verdict.get("checklist") or []
        if (label := _clean(i.get("label")))
    ]
    if checklist:
        card["checklist"] = checklist
    if isinstance(verdict.get("checks_run"), int):
        card["checks_run"] = verdict["checks_run"]
    step = verdict.get("next_step") or {}
    if text := _clean(step.get("text")):
        card["next_step"] = {"type": str(step.get("type", "")), "text": text}
    missing = (verdict.get("incomplete") or {}).get("steps") or []
    if steps := [str(s) for s in missing if s in shopper.catalogue()["incomplete"]["steps"]]:
        # Without what the check cost: whoever reads a shared result didn't pay for it.
        card["incomplete"] = {"steps": steps, "text": result_card.incomplete_text(steps, public=True)}
    same = verdict.get("same_product") or {}
    if same:
        if any(_clean(same.get(k)) is None for k in _SAME_PRODUCT_TEXT):
            notes.append("same product")
        else:
            card["same_product"] = {k: same.get(k) for k in (*_SAME_PRODUCT_TEXT, *_SAME_PRODUCT_KEPT)}
    return card


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
    The analysis response in public wording: shaped like a /check result, worded about the listing.

    The band's label and symbol come from :data:`PUBLIC_BANDS`; reasons, the things that look good and the details
    are kept as the user saw them, except any text containing a forbidden word (a reason or flag whose title does is
    left out; a detail or quote that does is dropped, keeping the title). No score, explanation, feature names or
    weights.

    :param response: The analysis response.
    :param left_out: Collects what was left out.
    :return: ``{"band", "verdict": {"band", "label", "symbol", "headline", "reasons", "positives"},
        "assessment": {"flags", "confidence", "model_version"}}``.
    """
    notes = left_out if left_out is not None else []
    verdict = response.get("verdict") or {}
    internal: shopper.Band = verdict.get("band") or shopper.band_for(int(response.get("score", 0)))
    band, symbol, label = PUBLIC_BANDS[internal]
    headline = _clean(verdict.get("headline"))
    if not headline:
        notes.append("headline")
        headline = _clean(shopper.catalogue()["headlines"][internal]) or label + "."
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
            **_card(verdict, notes),
        },
    }
    if flags:
        shown["assessment"] = {
            "flags": flags,
            "confidence": assessment.get("confidence"),
            "model_version": _clean(assessment.get("model_version")) or "",
        }
    return shown
