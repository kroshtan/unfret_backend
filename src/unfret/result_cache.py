"""
A short-lived, in-memory cache of analysis results per listing.

Listing-level results are only ever held in process memory for about an hour: a persistent cache keyed by listing
would be a log of what people checked, and payments on a public chain carry timestamps that could be matched
against it. Expiry uses a monotonic clock, so no wall-clock time is stored with an entry. A restart empties it.

The key covers everything the result depends on: the listing (URL without tracking parameters), a digest of the
exact input that was analysed (page text and client-supplied facts), the tier, the language and the flags. Results
built from what one caller sent are therefore only ever served for byte-identical input: someone who sends crafted
"clean" facts for a scam shop's URL only poisons the entry for their own crafted input, never for other callers.
"""

import hashlib
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RESULT_TTL_S = 3600.0
_MAX_ENTRIES = 5_000

# Query parameters that track the visitor or the referral, never the product.
_TRACKING_PARAMS = frozenset(
    {
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "ref",
        "ref_",
        "referrer",
        "tag",
        "si",
        "spm",
        "scm",
        "srsltid",
        "pd_rd_r",
        "pd_rd_w",
        "pd_rd_wg",
        "pf_rd_p",
        "pf_rd_r",
        "psc",
        "qid",
        "sr",
        "keywords",
        "crid",
        "sprefix",
    }
)

_entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def is_tracking_param(name: str) -> bool:
    """
    Whether a query parameter tracks the visitor or the referral rather than naming the product.

    :param name: The parameter's name.
    :return: ``True`` for tracking and referral parameters (``utm_*``, ``gclid``, ``ref``, …).
    """
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith("utm_")


def _listing(url: str) -> str:
    """
    The listing a URL points at: scheme, host and path, plus the query parameters that identify the product.

    Product identifiers in the query (``?id=``, ``?product_id=``, ``?p=``, ``?variant=``) are kept, so two products
    behind the same script are never the same entry; tracking and referral parameters are dropped.

    :param url: The page URL.
    :return: The normalised listing.
    """
    parts = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def listing_key(url: str, analysed: str) -> str:
    """
    The cache key for a listing analysed from a given input.

    :param url: The page URL ("" when unknown).
    :param analysed: Everything the analysis read (page content, client facts, …), serialised; it's hashed.
    :return: A hex digest; neither the URL nor the content is kept in memory.
    """
    digest = hashlib.sha256(analysed.encode()).hexdigest()
    basis = f"{_listing(url) if url else ''}\n{digest}"
    return hashlib.sha256(basis.encode()).hexdigest()


def key_for(
    *,
    url: str,
    page_content: str,
    page_facts_json: str = "",
    checkout_content: str = "",
    tier: str = "full",
    language: str = "en",
    flags: list[str] | None = None,
) -> str:
    """
    The cache key for one analysis request, for every path that analyses pages (extension, stream, x402).

    :param url: The page URL.
    :param page_content: The page HTML or cleaned text that was analysed.
    :param page_facts_json: The client-supplied facts, serialised (``model_dump_json``), if any.
    :param checkout_content: The checkout page, if supplied (it changes the drip-pricing result).
    :param tier: ``full`` (extension), ``quick`` or ``deep`` (x402): different skills, different results.
    :param language: The verdict's language.
    :param flags: Extra skills requested.
    :return: The key.
    """
    analysed = "\x1e".join((page_content, page_facts_json, checkout_content))
    base = listing_key(url, analysed)
    return f"{base}|{tier}|{language}|{','.join(sorted(flags or []))}"


def get(key: str) -> dict[str, Any] | None:
    """
    A cached result, if one is still fresh.

    :param key: From :func:`key_for`.
    :return: The result, or ``None``.
    """
    entry = _entries.get(key)
    if entry is None or entry[0] < time.monotonic():
        _entries.pop(key, None)
        return None
    return entry[1]


def put(key: str, result: dict[str, Any], ttl_s: float = RESULT_TTL_S) -> None:
    """
    Cache a result for ``ttl_s`` seconds.

    :param key: From :func:`key_for`.
    :param result: The result to cache.
    :param ttl_s: Lifetime in seconds.
    """
    _entries[key] = (time.monotonic() + ttl_s, result)
    _entries.move_to_end(key)
    while len(_entries) > _MAX_ENTRIES:
        _entries.popitem(last=False)


def clear() -> None:
    """Drop every cached result."""
    _entries.clear()
