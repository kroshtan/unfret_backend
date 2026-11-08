"""
A short-lived, in-memory cache of analysis results per listing.

Listing-level results are only ever held in process memory for about an hour: a persistent cache keyed by listing
would be a log of what people checked, and payments on a public chain carry timestamps that could be matched
against it. Expiry uses a monotonic clock, so no wall-clock time is stored with an entry. A restart empties it.
"""

import hashlib
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit, urlunsplit

RESULT_TTL_S = 3600.0
_MAX_ENTRIES = 5_000

_entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def listing_key(url: str, page_content: str) -> str:
    """
    The cache key for a listing: its URL without query string or fragment, or else the page content.

    Tracking parameters and fragments don't change the listing, so they are dropped; the key is a hash, so the
    URL itself isn't kept in memory either.

    :param url: The page URL ("" when unknown).
    :param page_content: The raw page, used when there's no URL.
    :return: A hex digest.
    """
    if url:
        parts = urlsplit(url.strip())
        basis = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    else:
        basis = "content:" + page_content
    return hashlib.sha256(basis.encode()).hexdigest()


def get(key: str) -> dict[str, Any] | None:
    """
    A cached result, if one is still fresh.

    :param key: From :func:`listing_key` (plus anything else that changes the result, e.g. the tier).
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

    :param key: From :func:`listing_key`.
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
