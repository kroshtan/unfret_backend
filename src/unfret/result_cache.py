"""
A short-lived, in-memory cache of analysis results per listing.

Listing-level results are only ever held in process memory for about an hour: a persistent cache keyed by listing
would be a log of what people checked, and payments on a public chain carry timestamps that could be matched
against it. Expiry uses a monotonic clock, so no wall-clock time is stored with an entry. A restart empties it.

The key covers everything the result depends on: the listing (URL without tracking parameters), a digest of the
exact input that was analysed (page text and client-supplied facts), the tier and the flags. Results
built from what one caller sent are therefore only ever served for byte-identical input: someone who sends crafted
"clean" facts for a scam shop's URL only poisons the entry for their own crafted input, never for other callers.

A deep result served from here costs a check like a fresh one, except for the account that already paid for that
listing within the hour (a reopened result, a page refresh): :func:`mark_paid` notes that, for as long as a result
lives here, as a keyed hash of the account and the listing (the key is random per process), so the note names
neither and a restart forgets it with the results.
"""

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RESULT_TTL_S = 3600.0
_MAX_ENTRIES = 5_000

# Query parameters that track the visitor or the referral, never the product. The browser extension drops the same ones
# (and the private ones below) from a checked page's address before sending it (its src/shared/urls.ts; `make parity`
# there copies this list into its tests), so an address keys the same here whichever client sent it.
_TRACKING_PARAMS = frozenset(
    {
        # ad clicks and campaigns
        "gclid",
        "gbraid",
        "wbraid",
        "dclid",
        "fbclid",
        "msclkid",
        "yclid",
        "ttclid",
        "twclid",
        "li_fat_id",
        "epik",
        "s_kwcid",
        "ef_id",
        "igshid",
        "igsh",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "vero_id",
        "vero_conv",
        "rb_clickid",
        "irclickid",
        "cjevent",
        "awc",
        "sscid",
        "ranmid",
        "raneaid",
        "ransiteid",
        "wickedid",
        # referrals and affiliate tags
        "ref",
        "ref_",
        "ref_src",
        "ref_url",
        "referrer",
        "tag",
        "aff_id",
        "affid",
        "linkcode",
        "linkid",
        "ascsubtag",
        "creativeasin",
        "si",
        "spm",
        "scm",
        "srsltid",
        # Amazon's search and placement trail (the product is the path's ASIN)
        "pd_rd_i",
        "pd_rd_r",
        "pd_rd_w",
        "pd_rd_wg",
        "pf_rd_i",
        "pf_rd_m",
        "pf_rd_p",
        "pf_rd_r",
        "pf_rd_s",
        "pf_rd_t",
        "psc",
        "qid",
        "sr",
        "keywords",
        "crid",
        "sprefix",
        "dib",
        "dib_tag",
        "content-id",
    }
)
_TRACKING_PREFIXES = ("utm_", "hsa_")
# Query parameters that carry a session, a token or an e-mail address in a shop's own links (a password reset, a
# newsletter's click, an old shop's session in the address): never the product.
_PRIVATE_PARAMS = frozenset(
    {
        "token",
        "access_token",
        "auth_token",
        "id_token",
        "reset_token",
        "auth",
        "secret",
        "api_key",
        "apikey",
        "password",
        "pwd",
        "session",
        "sessionid",
        "session_id",
        "sid",
        "oscsid",
        "phpsessid",
        "jsessionid",
        "cfid",
        "cftoken",
        "email",
        "e-mail",
        "e_mail",
        "email_address",
    }
)
# A value that is an e-mail address (also with its @ escaped), or an Unfret pass or backup code, whatever its name.
_PRIVATE_VALUE = re.compile(
    r"[^\s@/?#&=]+(?:@|%40)[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}|uf[kr]_[a-z2-7]{6,}", re.IGNORECASE
)

_entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
# Accounts that paid for a listing within the hour: keyed hash of (account, listing) → expiry (monotonic).
_paid: OrderedDict[str, float] = OrderedDict()
_MAX_PAID = 20_000
_PAID_KEY = secrets.token_bytes(32)


def is_tracking_param(name: str, value: str = "") -> bool:
    """
    Whether a query parameter tracks the visitor or the referral, or carries their session, rather than the product.

    :param name: The parameter's name.
    :param value: Its value (decoded): an e-mail address or an Unfret pass there is never the product either.
    :return: ``True`` for tracking and referral parameters (``utm_*``, ``gclid``, ``ref``, …), sessions, tokens and
        e-mail addresses.
    """
    lowered = name.lower()
    return (
        lowered in _TRACKING_PARAMS
        or lowered in _PRIVATE_PARAMS
        or lowered.startswith(_TRACKING_PREFIXES)
        or bool(_PRIVATE_VALUE.search(value))
    )


def without_tracking(url: str) -> str:
    """
    A page's address without what isn't the page: no fragment, user name or password, or tracking parameters.

    The other query parameters keep their order (a product's ``?id=``, ``?variant=``).

    :param url: An absolute address.
    :return: The cleaned address (``url`` as given when it can't be parsed).
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return url.strip()
    netloc = (parts.hostname or "") + (f":{port}" if port else "")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k, v)]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _listing(url: str) -> str:
    """
    The listing a URL points at: scheme, host and path, plus the query parameters that identify the product.

    Product identifiers in the query (``?id=``, ``?product_id=``, ``?p=``, ``?variant=``) are kept, so two products
    behind the same script are never the same entry; tracking and referral parameters are dropped.

    :param url: The page URL.
    :return: The normalised listing.
    """
    parts = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k, v))
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
    flags: list[str] | None = None,
) -> str:
    """
    The cache key for one analysis request, for every path that analyses pages (extension, stream, x402).

    :param url: The page URL.
    :param page_content: The page HTML or cleaned text that was analysed.
    :param page_facts_json: The client-supplied facts, serialised (``model_dump_json``), if any.
    :param checkout_content: The checkout page, if supplied (it changes the drip-pricing result).
    :param tier: ``full`` (extension), ``quick`` or ``deep`` (x402): different skills, different results.
    :param flags: Extra skills requested.
    :return: The key.
    """
    analysed = "\x1e".join((page_content, page_facts_json, checkout_content))
    base = listing_key(url, analysed)
    # The empty field held the verdict's language; it stays so report tokens issued before still match.
    return f"{base}|{tier}||{','.join(sorted(flags or []))}"


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


def _paid_key(account: str, url: str) -> str:
    return hmac.new(_PAID_KEY, f"{account}\n{_listing(url)}".encode(), hashlib.sha256).hexdigest()


def mark_paid(account: str, url: str, ttl_s: float = RESULT_TTL_S) -> None:
    """
    Note that an account paid for a listing, so its own repeat within ``ttl_s`` is served from here without a charge.

    :param account: The paying account (a wallet or a pass).
    :param url: The page URL; without one nothing is noted.
    :param ttl_s: How long the note lasts, in seconds (as long as a result).
    """
    if not url.strip():
        return
    key = _paid_key(account, url)
    _paid[key] = time.monotonic() + ttl_s
    _paid.move_to_end(key)
    while len(_paid) > _MAX_PAID:
        _paid.popitem(last=False)


def paid_for(account: str, url: str) -> bool:
    """
    Whether an account paid for this listing within the hour (see :func:`mark_paid`).

    :param account: The account.
    :param url: The page URL.
    :return: ``True`` while the note lasts; never for a request without a URL.
    """
    if not url.strip():
        return False
    key = _paid_key(account, url)
    expires = _paid.get(key)
    if expires is None or expires < time.monotonic():
        _paid.pop(key, None)
        return False
    return True


def clear() -> None:
    """Drop every cached result, and every note of who paid for one."""
    _entries.clear()
    _paid.clear()
