"""
A shop's fingerprint: the identifiers that tie web shops to the same operator, and the shape of its template.

Only identifiers that are unique to one account count as links: analytics and tag-manager IDs, advertising pixels,
payment merchant IDs and the shop's ``*.myshopify.com`` name. Themes and apps are shared by thousands of honest
shops; they are recorded as facts about the shop, never as links. Contact e-mail addresses and phone numbers are
personal data: they leave this module only as keyed hashes (:func:`contact_hash`) and are never shown.

Everything here is pure and deterministic: the page source in, the fingerprint out.
"""

import contextlib
import hashlib
import hmac
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

# Identifier kinds that link shops (one account, one operator), with the pattern capturing the ID.
LINK_PATTERNS: dict[str, re.Pattern[str]] = {
    "ga": re.compile(r"\b(G-[A-Z0-9]{8,12}|UA-\d{4,10}-\d{1,4})\b"),
    "gtm": re.compile(r"\b(GTM-[A-Z0-9]{5,9})\b"),
    "google_ads": re.compile(r"\b(AW-\d{9,11})\b"),
    "adsense": re.compile(r"\b(ca-pub-\d{16})\b"),
    "meta_pixel": re.compile(
        r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{13,17})['\"]|facebook\.com/tr\?id=(\d{13,17})", re.IGNORECASE
    ),
    "tiktok_pixel": re.compile(r"ttq\.load\(\s*['\"]([A-Z0-9]{18,22})['\"]|sdkid=([A-Z0-9]{18,22})"),
    "pinterest_tag": re.compile(r"pintrk\(\s*['\"]load['\"]\s*,\s*['\"](\d{13})['\"]"),
    "stripe": re.compile(r"\b(pk_live_[A-Za-z0-9]{24,110})\b"),
    "paypal": re.compile(r"paypal\.com/sdk/js\?[^\"'\s<>]*?client-id=([A-Za-z0-9_-]{60,90})"),
    "myshopify": re.compile(r"\b([a-z0-9][a-z0-9-]{1,60}\.myshopify\.com)\b"),
}
# Values that are placeholders in theme demos and documentation, not anyone's account.
_PLACEHOLDER_RE = re.compile(r"(X{4,}|0{6,}|1234567|ABCDEF|YOUR|EXAMPLE)", re.IGNORECASE)
# Platform-owned stores and SDK hosts that appear on every shop of a platform.
_PLATFORM_HOSTS = frozenset({"checkout.myshopify.com", "shop.myshopify.com", "cdn.myshopify.com", "www.myshopify.com"})

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24})\b", re.IGNORECASE
)
_MAILTO_RE = re.compile(r"mailto:([^\"'?\s<>]+)", re.IGNORECASE)
_TEL_RE = re.compile(r"tel:([+\d][\d\s().-]{6,24})", re.IGNORECASE)
_PLUS_PHONE_RE = re.compile(r"(?<![\w+])(\+\d{1,3}[\s.-]?(?:\(0\)[\s.-]?)?\d[\d\s.-]{6,16}\d)")
# Addresses that belong to platforms, libraries and examples rather than to the shop.
_NOT_A_SHOP_EMAIL_RE = re.compile(
    r"@(?:example\.|sentry|wixpress\.com|shopify\.com|domain\.com|email\.com|yourdomain|test\.)"
    r"|^(?:noreply|no-reply|donotreply|name|user|your|email|info@example)",
    re.IGNORECASE,
)
_FILE_SUFFIX_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|css|js)$", re.IGNORECASE)

MAX_IDS_PER_KIND = 5  # more than this of one kind on one page is a theme demo or an agency page, not one shop
_MAX_TAGS = 5_000
_SHINGLE = 3


@dataclass
class Fingerprint:
    """What a page shows about the shop behind it. Contacts are normalised plaintext until hashed."""

    links: dict[str, set[str]] = field(default_factory=dict)
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    skeleton: int | None = None


def _first_group(match: re.Match[str]) -> str:
    return next(g for g in match.groups() if g)


def link_ids(source: str) -> dict[str, set[str]]:
    """
    The account identifiers on a page, per kind.

    :param source: The page source (raw HTML, or cleaned text plus script URLs and meta tags).
    :return: Kind → IDs; placeholder values and kinds with implausibly many IDs are left out.
    """
    found: dict[str, set[str]] = {}
    for kind, pattern in LINK_PATTERNS.items():
        ids = {_first_group(m) for m in pattern.finditer(source)}
        ids = {i for i in ids if not _PLACEHOLDER_RE.search(i) and i.lower() not in _PLATFORM_HOSTS}
        if kind == "myshopify":
            ids = {i.lower() for i in ids}
        if 0 < len(ids) <= MAX_IDS_PER_KIND:
            found[kind] = ids
    return found


def normalise_email(value: str) -> str | None:
    """
    An e-mail address in its comparable form, or ``None`` when it isn't a shop's own address.

    :param value: A candidate address.
    :return: The lower-cased address.
    """
    email = value.strip().strip(".").lower()
    if "@" not in email or _NOT_A_SHOP_EMAIL_RE.search(email) or _FILE_SUFFIX_RE.search(email):
        return None
    return email


def normalise_phone(value: str) -> str | None:
    """
    A phone number as ``+`` and digits, or ``None`` when it is too short or too long to be one.

    :param value: A candidate number, e.g. ``+31 (0)20-123 4567``.
    :return: E.g. ``+31201234567``; numbers without a country code keep their digits only.
    """
    raw = value.replace("(0)", "")
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15 or len(set(digits)) < 3:
        return None
    return ("+" if raw.strip().startswith("+") else "") + digits


def contacts(source: str) -> tuple[set[str], set[str]]:
    """
    The shop's contact e-mail addresses and phone numbers on a page.

    Phone numbers only from ``tel:`` links and numbers written with a country code, so prices and order numbers
    aren't mistaken for them.

    :param source: The page source.
    :return: ``(emails, phones)``, normalised.
    """
    candidates = [m.group(1) for m in _MAILTO_RE.finditer(source)] + [m.group(1) for m in _EMAIL_RE.finditer(source)]
    emails = {e for c in candidates if (e := normalise_email(c))}
    numbers = [m.group(1) for m in _TEL_RE.finditer(source)] + [m.group(1) for m in _PLUS_PHONE_RE.finditer(source)]
    phones = {p for n in numbers if (p := normalise_phone(n))}
    return set(sorted(emails)[:MAX_IDS_PER_KIND]), set(sorted(phones)[:MAX_IDS_PER_KIND])


def contact_hash(kind: str, value: str, key: bytes) -> bytes:
    """
    The stored form of a contact: a keyed hash, so the table can't be reversed with a phone-number dictionary.

    :param kind: ``"email"`` or ``"phone"``.
    :param value: The normalised value.
    :param key: The server's secret key.
    :return: 32 bytes.
    """
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).digest()


class _Skeleton(HTMLParser):
    """Collects the page's tags with their first class name: the template, not the content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tokens: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.tokens) >= _MAX_TAGS:
            return
        classes = next((v for k, v in attrs if k == "class" and v), "")
        first = re.sub(r"[\d_-]+$|\d+", "", classes.split()[0]) if classes.split() else ""
        self.tokens.append(f"{tag}.{first}" if first else tag)


def skeleton_hash(html: str) -> int | None:
    """
    A 64-bit simhash of the page's tag structure: pages built on the same template have close hashes.

    A shared template is common (one theme serves thousands of shops), so this is recorded as a fact about the
    shop and never used as a link on its own.

    :param html: Raw HTML.
    :return: The hash as a signed 64-bit integer (for a ``BIGINT`` column), or ``None`` for text without markup.
    """
    if html.count("<") < 50:
        return None
    parser = _Skeleton()
    with contextlib.suppress(AssertionError, ValueError):  # malformed markup: use what was parsed
        parser.feed(html)
    tokens = parser.tokens
    if len(tokens) < 30:
        return None
    weights = [0] * 64
    for i in range(len(tokens) - _SHINGLE + 1):
        digest = int.from_bytes(hashlib.blake2b("|".join(tokens[i : i + _SHINGLE]).encode(), digest_size=8).digest())
        for bit in range(64):
            weights[bit] += 1 if digest >> bit & 1 else -1
    value = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


def hamming(a: int, b: int) -> int:
    """
    The number of differing bits between two 64-bit hashes (signed or unsigned).

    :param a: One hash.
    :param b: The other.
    :return: 0–64.
    """
    return ((a ^ b) & (1 << 64) - 1).bit_count()


def fingerprint(source: str) -> Fingerprint:
    """
    Everything this module reads from a page.

    :param source: The page source: raw HTML, or cleaned text with the script URLs and meta tags appended.
    :return: The fingerprint, contacts still in plaintext (hash them before storing).
    """
    emails, phones = contacts(source)
    return Fingerprint(links=link_ids(source), emails=emails, phones=phones, skeleton=skeleton_hash(source))
