"""
A result as the public sees it: on a shared result's page (:mod:`unfret.shares`), never a shopper's own screen.

Facts, not accusations. The band keeps its symbol but is worded as what Unfret would tell a shopper about this
listing (:data:`PUBLIC_BANDS`); the score number is not shown, and neither "scam" nor "fraud" appears anywhere: a
sentence or a quote that contains one is left out (the band's neutral headline replaces such a headline).
"""

import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

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
        },
    }
    if flags:
        shown["assessment"] = {
            "flags": flags,
            "confidence": assessment.get("confidence"),
            "model_version": _clean(assessment.get("model_version")) or "",
        }
    return shown
