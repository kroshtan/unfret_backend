"""
The business facts on a page: what a shop or service company publishes about itself, and nothing about the visitor.

Contacts, addresses and identity statements are read only from what the business publishes: structured data
(Organization/LocalBusiness JSON-LD), ``tel:`` and ``mailto:`` links, and contact areas (the footer, contact, legal
and imprint sections). Areas that could hold the visitor's own details are skipped whatever they contain: forms and
their fields, account menus, greetings, checkout, billing and delivery areas, and any element marked as an address
outside a contact area. The browser extension applies the same rules to the live page (``src/extract/business.ts``,
kept in parity), where the visitor may be logged in.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, CData, NavigableString, Tag

from unfret.facts.schema import PRIVATE_SELLER_PLATFORMS, Email, Licence, Link, PageFacts, Phone, PhoneKind

# ── Which areas are the visitor's and which the business's ──────────────────────────────────────────────────

_PRIVATE_TAGS = frozenset({"form", "input", "textarea", "select"})
# Amazon's account and delivery-location widgets.
_PRIVATE_IDS = frozenset(
    {
        "nav-belt",
        "nav-main",
        "nav-tools",
        "glow-ingress-block",
        "nav-global-location-slot",
        "contextualIngressPt",
        "deliveryBlockMessage",
    }
)
# Matched in id, class, data-testid and aria-label (lower case).
PRIVATE_WORDS = (
    "account",
    "greeting",
    "customer-name",
    "checkout",
    "billing",
    "deliver-to",
    "delivery-address",
    "shipping-address",
    "login",
    "signin",
    "sign-in",
    "user-menu",
    "usermenu",
)
# Matched in id and class: the areas where a business publishes its own details.
BUSINESS_WORDS = (
    "footer",
    "contact",
    "imprint",
    "impressum",
    "legal",
    "colofon",
    "colophon",
    "about",
    "company",
    "kontakt",
)
_BUSINESS_ITEMTYPES = ("organization", "localbusiness", "postaladdress")
_SKIP_TAGS = frozenset({"script", "style", "template"})


def _attr(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value is not None else ""


def is_private(tag: Tag) -> bool:
    """
    Whether an element may hold the visitor's own details (its whole subtree is skipped).

    :param tag: The element.
    :return: ``True`` for form fields, account, greeting, checkout, billing and delivery areas.
    """
    if tag.name in _PRIVATE_TAGS or tag.has_attr("autocomplete") or _attr(tag, "id") in _PRIVATE_IDS:
        return True
    marks = " ".join(_attr(tag, a) for a in ("id", "class", "data-testid", "aria-label")).lower()
    return any(word in marks for word in PRIVATE_WORDS)


def is_business(tag: Tag) -> bool:
    """
    Whether an element is an area where the business publishes its own details.

    :param tag: The element.
    :return: ``True`` for the footer, contact/legal/imprint/about sections and Organization or address microdata.
    """
    if tag.name == "footer":
        return True
    marks = f"{_attr(tag, 'id')} {_attr(tag, 'class')}".lower()
    itemtype = _attr(tag, "itemtype").lower()
    return any(word in marks for word in BUSINESS_WORDS) or any(t in itemtype for t in _BUSINESS_ITEMTYPES)


def is_address_marked(tag: Tag) -> bool:
    """
    Whether an element is an address: kept only inside a business area.

    An ``<address>`` element, or one whose id or class mentions "address".

    :param tag: The element.
    :return: ``True`` for an address element or one marked as an address.
    """
    return tag.name == "address" or "address" in f"{_attr(tag, 'id')} {_attr(tag, 'class')}".lower()


@dataclass
class _Scan:
    public: list[str] = field(default_factory=list)  # all text outside private areas
    business: list[str] = field(default_factory=list)  # text inside business areas
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, text) outside private areas
    alts: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _scan(soup: BeautifulSoup) -> _Scan:
    # Document order, skipping private subtrees; an element marked as an address is private unless it sits in (or
    # is) a business area.
    scan = _Scan()
    stack: list[tuple[Any, bool]] = [(child, False) for child in reversed(soup.contents)]
    while stack:
        node, business = stack.pop()
        if isinstance(node, Tag):
            inside = business or is_business(node)
            if node.name in _SKIP_TAGS or is_private(node) or (is_address_marked(node) and not inside):
                continue
            _collect(node, inside, scan)
            stack.extend((child, inside) for child in reversed(node.contents))
        elif type(node) in {NavigableString, CData}:
            scan.public.append(str(node))
            if business:
                scan.business.append(str(node))
    return scan


def public_text(soup: BeautifulSoup) -> str:
    """
    The page's text outside the areas that could hold the visitor's details (forms, account menus, checkout).

    :param soup: The parsed page.
    :return: The text, whitespace collapsed.
    """
    return _squash(" ".join(_scan(soup).public))


def _collect(tag: Tag, inside: bool, scan: _Scan) -> None:
    if tag.name == "a" and tag.has_attr("href"):
        scan.links.append((_attr(tag, "href"), _squash(tag.get_text(" "))))
    elif tag.name == "img" and tag.has_attr("alt"):
        scan.alts.append(_attr(tag, "alt"))
    is_address = tag.name == "address" or _attr(tag, "itemprop") == "address"
    if inside and is_address and not tag.find(list(_PRIVATE_TAGS)):
        scan.addresses.append(_squash(tag.get_text(" ")))


# ── Patterns ────────────────────────────────────────────────────────────────────────────────────────────────

# Payment methods: (name, pattern, case sensitive). Letters around a name are excluded explicitly (not \b), so the
# browser's JavaScript port matches the same text.
_PAYMENT_METHODS: tuple[tuple[str, str, bool], ...] = (
    ("paypal", r"paypal", False),
    ("credit_card", r"credit ?cards?|creditcard|kreditkarte|carte bancaire|carte de cr[ée]dit", False),
    ("visa", r"(?<![a-z0-9])visa(?![a-z0-9])", False),
    ("mastercard", r"master ?card", False),
    ("amex", r"american express|(?<![a-z0-9])amex(?![a-z0-9])", False),
    ("maestro", r"(?<![a-z0-9])maestro(?![a-z0-9])", False),
    ("ideal", r"(?<![A-Za-z0-9])iDEAL(?![A-Za-z0-9])", True),
    ("bancontact", r"bancontact", False),
    ("klarna", r"klarna", False),
    ("afterpay", r"afterpay|clearpay|riverty", False),
    ("sofort", r"(?<![a-z0-9])sofort(?![a-z0-9])", False),
    ("giropay", r"giropay", False),
    ("apple_pay", r"apple ?pay", False),
    ("google_pay", r"google ?pay|g ?pay(?![a-z0-9])", False),
    ("sepa", r"(?<![a-z0-9])sepa(?![a-z0-9])", False),
    ("bank_transfer", r"bank ?transfer|wire transfer|overboeking|bankoverschrijving|überweisung|virement", False),
    ("tikkie", r"tikkie", False),
    (
        "crypto",
        r"bitcoin|cryptocurrenc|crypto ?(?:payment|currency|currencies)|ethereum|(?<![a-z0-9])usdt(?![a-z0-9])",
        False,
    ),
    ("gift_card", r"gift ?cards?|cadeaukaart|gutschein|itunes card|google play card|steam card", False),
    ("western_union", r"western union", False),
    ("moneygram", r"moneygram", False),
    ("cash_on_delivery", r"cash on delivery|rembours|nachnahme|contre-remboursement", False),
    ("cash", r"(?<![a-z0-9])cash(?![a-z0-9])|(?<![a-z0-9])contant(?![a-z0-9])", False),
)
_PAYMENT_RES = tuple(
    (name, re.compile(pattern, 0 if case else re.IGNORECASE)) for name, pattern, case in _PAYMENT_METHODS
)
_FOUNDED_RE = re.compile(
    r"(?<![a-z0-9])(?:since|established(?: in)?|est\.|founded(?: in)?|opgericht(?: in)?|sinds|seit|gegr[üu]ndet"
    r"(?: im jahr)?|depuis|fond[ée]e? en|desde)\s+(?:1[89]\d\d|20[0-3]\d)(?![0-9])",
    re.IGNORECASE,
)
_KVK_RE = re.compile(
    r"(?<![a-z])(?:kvk|k\.v\.k\.|kamer van koophandel|chamber of commerce|coc)(?:[\s.:#-]|nummer|number|nr|no)*"
    r"(\d{4} ?\d{4})(?![0-9])",
    re.IGNORECASE,
)
_LICENCE_RE = re.compile(
    r"(?<![A-Za-z])(?:([A-Z]{2})\s+)?(?:[Ss]tate\s+)?(?:[Cc]ontractor'?s?\s+)?"
    r"(?:[Ll]ic(?:ense|ence)?|LIC(?:ENSE|ENCE)?|CSLB|ROC)\.?\s*(?:#|[Nn]o\.?|[Nn]umber|[Nn]r\.?)?\s*:?\s*#?\s*"
    r"([A-Z]{0,4}-?\d{5,10})(?![0-9])"
)
US_STATES = frozenset(
    ("AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA")
    + ("ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK")
    + ("OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY")
)
_CERTIFICATION_RE = re.compile(
    r"(?<![a-z])(?:licensed|insured|bonded|certified|gecertificeerd|erkend|keurmerk|techniek nederland|installq"
    r"|(?<![a-z])vca(?![a-z])|gas safe|niceic|(?<![a-z])bbb(?![a-z])|kiwa|(?<![a-z])skg(?![a-z]))",
    re.IGNORECASE,
)
_PROMISE_RE = re.compile(
    r"24/7|24 uur|24 hours|24h(?![a-z0-9])|within \d+ (?:minutes|hours)|binnen \d+ (?:minuten|uur)|same[ -]day"
    r"|(?:de)?zelfde dag|spoedservice|(?<![a-z])spoed(?![a-z])|emergency",
    re.IGNORECASE,
)
_PRICING_RE = re.compile(
    r"call-?out(?: fee| charge)?|voorrijkosten|free (?:quote|estimate)|gratis offerte|vrijblijvende offerte"
    r"|fixed price|vaste prijs|upfront|vooraf betalen|vooruitbetaling|aanbetaling|(?<![a-z])deposit(?![a-z])"
    r"|cash only|alleen contant|per hour|per uur|uurtarief|hourly rate",
    re.IGNORECASE,
)
_LEGAL_FORM_RE = re.compile(
    r"(?<![A-Za-z])(B\.V\.|BV|V\.O\.F\.|VOF|N\.V\.|NV|[Ee]enmanszaak|L\.L\.C\.|LLC|PLLC|LLP|Inc\.|Inc|Corp\.|Corp"
    r"|Corporation|Ltd\.|Ltd|Limited|GmbH|SARL|SAS|S\.L\.|S\.r\.l\.)(?![A-Za-z])"
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9-])"
)
_NOT_A_BUSINESS_EMAIL_RE = re.compile(
    r"@(?:example\.|sentry|wixpress\.com|shopify\.com|domain\.com|email\.com|yourdomain|test\.)"
    r"|^(?:noreply|no-reply|donotreply|name|user|your|email|info@example)",
    re.IGNORECASE,
)
_FILE_SUFFIX_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|css|js)$", re.IGNORECASE)
WEBMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "hotmail.com",
        "hotmail.nl",
        "hotmail.co.uk",
        "outlook.com",
        "outlook.nl",
        "live.com",
        "live.nl",
        "msn.com",
        "yahoo.com",
        "yahoo.nl",
        "yahoo.co.uk",
        "icloud.com",
        "me.com",
        "aol.com",
        "gmx.com",
        "gmx.de",
        "gmx.net",
        "web.de",
        "protonmail.com",
        "proton.me",
        "mail.com",
        "ziggo.nl",
        "kpnmail.nl",
        "planet.nl",
        "home.nl",
        "hetnet.nl",
        "casema.nl",
        "xs4all.nl",
        "telenet.be",
        "skynet.be",
        "comcast.net",
        "att.net",
        "verizon.net",
        "yandex.com",
    }
)
# Phone numbers in contact areas: with a country code, in Dutch national format (0 + nine digits; 0800/0900 numbers),
# or in US format with separators ("(212) 555-1234"). tel: links take any number.
_PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9+])(?:\+\d{1,3}[\s.-]?(?:\(0\)[\s.-]?)?\d[\d\s.-]{6,16}\d"
    r"|0[89]0[069][\s-]?\d{4}(?:\d{3})?"
    r"|0(?:[\s.-]?\d){9}"
    r"|(?:1[\s.-])?\(?[2-9]\d{2}\)?[\s.-]\d{3}[\s.-]\d{4})(?![0-9])"
)
_BANK_RE = re.compile(r"iban|rekening|bank", re.IGNORECASE)
_US_TOLL_FREE = frozenset({"800", "888", "877", "866", "855", "844", "833"})
_COUNTRY_CODES = {"31": "NL", "32": "BE", "44": "GB", "49": "DE", "33": "FR"}
_ORG_TYPES = frozenset(
    {
        "organization",
        "corporation",
        "localbusiness",
        "onlinestore",
        "onlinebusiness",
        "store",
        "professionalservice",
        "homeandconstructionbusiness",
        "plumber",
        "electrician",
        "locksmith",
        "hvacbusiness",
        "roofingcontractor",
        "generalcontractor",
        "housepainter",
        "movingcompany",
        "emergencyservice",
        "automotivebusiness",
        "autorepair",
        "dentist",
        "legalservice",
        "attorney",
        "accountingservice",
        "financialservice",
        "realestateagent",
        "travelagency",
        "healthandbeautybusiness",
        "childcare",
        "drycleaningorlaundry",
    }
)
_ORG_KEYS = (
    "@type",
    "name",
    "legalName",
    "telephone",
    "email",
    "address",
    "areaServed",
    "foundingDate",
    "vatID",
    "taxID",
    "priceRange",
    "aggregateRating",
    "sameAs",
)
_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?![A-Za-z0-9])")
_ID_KEYS = ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn")

_MAX_SNIPPETS = 20


# ── Extraction ──────────────────────────────────────────────────────────────────────────────────────────────


def extract(soup: BeautifulSoup, url: str, facts: PageFacts) -> None:
    """
    Fill the business fields of ``facts`` (contacts, identity statements, payment methods, internal links).

    Runs after the product extraction, whose JSON-LD products it reads for public product IDs. On a platform where
    the seller may be a private person (:data:`~unfret.facts.schema.PRIVATE_SELLER_PLATFORMS`), only the product
    IDs and payment methods are read: the rest would describe the platform, or name the seller.

    :param soup: The parsed page (not modified).
    :param url: The page URL (same-site links, the page's own e-mail domain).
    :param facts: The facts to fill.
    """
    scan = _scan(soup)
    public = _squash(" ".join(scan.public))
    business = _squash(" ".join(scan.business))
    host = _host(url)

    facts.product_ids = _product_ids(facts, url)
    facts.payment_methods = _payment_methods(f"{public} {' '.join(scan.alts)}")
    if facts.platform in PRIVATE_SELLER_PLATFORMS:
        return
    orgs = organizations(soup)
    facts.organization_ld = [_trim(o) for o in orgs[:10]]
    facts.phones = _phones(scan, business, orgs, host)
    facts.emails = _emails(scan, business, orgs, host)
    facts.addresses = _dedupe([*(_ld_address(o.get("address")) for o in orgs), *scan.addresses])[:20]
    founding = [f"foundingDate {o['foundingDate']}" for o in orgs if isinstance(o.get("foundingDate"), str)]
    facts.founded_claims = _dedupe([*(m.group() for m in _FOUNDED_RE.finditer(public)), *founding])[:10]
    name = next((_text_value(o.get("name")) for o in orgs if _text_value(o.get("name"))), "")
    facts.business_name = name[:300]
    legal_name = next((_text_value(o.get("legalName")) for o in orgs if _text_value(o.get("legalName"))), "")
    facts.legal_form = _legal_form([name, legal_name, *facts.copyright_lines])
    facts.kvk_numbers = _dedupe(m.group(1).replace(" ", "") for m in _KVK_RE.finditer(public))[:10]
    facts.licences = _licences(public)
    facts.certifications = _dedupe(_snippets(_CERTIFICATION_RE, public, 60, 100))[:_MAX_SNIPPETS]
    facts.promises = _dedupe(_snippets(_PROMISE_RE, public, 60, 100))[:_MAX_SNIPPETS]
    facts.pricing_terms = _dedupe(_snippets(_PRICING_RE, public, 60, 100))[:_MAX_SNIPPETS]
    facts.cities_served = _dedupe(c for o in orgs for c in _areas(o.get("areaServed")))[:100]
    facts.internal_links = _internal_links(scan.links, url, host)


_HOST_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//([^/?#]*)")


def _host(url: str) -> str:
    # By hand rather than urlparse, so the browser port reads the same host from the same string.
    m = _HOST_RE.match(url.strip())
    if not m:
        return ""
    return m.group(1).rsplit("@", 1)[-1].split(":", 1)[0].lower().removeprefix("www.")


def _dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _snippets(pattern: re.Pattern[str], text: str, before: int, after: int) -> list[str]:
    # As extract._snippets: the keyword and its sentence, at most `before`/`after` characters either side.
    found = []
    for m in pattern.finditer(text):
        lo, hi = max(m.start() - before, 0), min(m.end() + after, len(text))
        head, tail = text[lo : m.start()], text[m.end() : hi]
        head = head[head.rfind(".") + 1 :] if "." in head else head
        cut = tail.find(".")
        found.append(_squash(head + m.group() + (tail[:cut] if cut >= 0 else tail)))
    return found


def _text_value(value: object) -> str:
    return _squash(value) if isinstance(value, str) else ""


def _product_ids(facts: PageFacts, url: str) -> dict[str, str]:
    ids: dict[str, str] = {}
    product = facts.json_ld[0] if facts.json_ld else {}
    for key in _ID_KEYS:
        value = product.get(key)
        if isinstance(value, str | int) and not isinstance(value, bool) and str(value).strip():
            ids[key] = str(value).strip()[:40]
    if facts.platform == "amazon" and (m := _ASIN_RE.search(url.split("?", 1)[0])):
        ids["asin"] = m.group(1)
    return ids


def _payment_methods(text: str) -> list[str]:
    return [name for name, pattern in _PAYMENT_RES if pattern.search(text)]


def _kinds(node: dict[str, Any]) -> list[str]:
    kind = node.get("@type", "")
    return [k for k in (kind if isinstance(kind, list) else [kind]) if isinstance(k, str) and k]


def _is_org(node: dict[str, Any]) -> bool:
    return any(k.lower() in _ORG_TYPES for k in _kinds(node))


def organizations(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """
    The page's Organization and LocalBusiness JSON-LD nodes, untrimmed.

    :param soup: The parsed page.
    :return: The nodes, in document order.
    """
    orgs: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if _is_org(node):
                orgs.append(node)
                return
            for key in ("@graph", "mainEntity", "itemListElement", "publisher", "provider"):
                if key in node:
                    walk(node[key])

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or ""))
        except ValueError, TypeError:
            continue
    return orgs


def _trim(node: dict[str, Any]) -> dict[str, Any]:
    trimmed = {k: node[k] for k in _ORG_KEYS if k in node}
    if len(json.dumps(trimmed, default=str)) <= 4_000:
        return trimmed
    return {k: trimmed[k] for k in ("@type", "name", "legalName") if k in trimmed}


def _ld_address(value: object) -> str:
    if isinstance(value, list):
        return _ld_address(value[0]) if value else ""
    if isinstance(value, str):
        return _squash(value)[:300]
    if not isinstance(value, dict):
        return ""
    parts = []
    for key in ("streetAddress", "postalCode", "addressLocality", "addressRegion", "addressCountry"):
        part = value.get(key)
        if isinstance(part, dict):
            part = part.get("name")
        if isinstance(part, str | int) and not isinstance(part, bool) and str(part).strip():
            parts.append(_squash(str(part)))
    return ", ".join(parts)[:300]


def _areas(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    names = [item.get("name") if isinstance(item, dict) else item for item in items]
    return [_squash(n)[:300] for n in names if isinstance(n, str) and n.strip()]


def _ld_strings(orgs: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for org in orgs:
        value = org.get(key)
        values += [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)]
    return values


def phone(raw: str, host: str = "") -> Phone | None:
    """
    A published phone number, normalised and typed.

    A number without a country code is read as Dutch on a ``.nl`` site, as American when it has ten digits and
    doesn't start with 0, and is left untyped otherwise.

    :param raw: The number as written ("+31 (0)20-123 4567", "0800-1234", "(212) 555-1234").
    :param host: The page's host (a national number's country).
    :return: The phone, or ``None`` when it is too short or long to be a number.
    """
    plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw.replace("(0)", ""))
    if not 7 <= len(digits) <= 15 or len(set(digits)) < 3:
        return None
    country = ""
    national = ""
    if plus or digits.startswith("00"):
        full = digits.removeprefix("00") if not plus else digits
        if full.startswith("1") and len(full) == 11:
            country, national = "US", full[1:]
        elif full[:2] in _COUNTRY_CODES:
            country, national = _COUNTRY_CODES[full[:2]], "0" + full[2:]
        number = "+" + full
    elif digits.startswith("0") and host.endswith(".nl"):
        country, national = "NL", digits
        number = "+31" + digits[1:]
    elif not digits.startswith("0") and (len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))):
        country, national = "US", digits[-10:]
        number = "+1" + national
    else:
        number = digits
    if country == "NL" and not 8 <= len(national) <= 11:
        country, number = "", ("+" if plus else "") + digits
    return Phone(number=number[:20], kind=_phone_kind(country, national), country=country)


# Number prefixes per kind, longest first; a number of a known country that matches none is geographic ("local").
_PHONE_PREFIXES: dict[str, tuple[tuple[str, PhoneKind], ...]] = {
    "NL": (
        ("0800", "toll_free"),
        ("090", "premium"),
        ("084", "national"),
        ("085", "national"),
        ("087", "national"),
        ("088", "national"),
        ("091", "national"),
        ("06", "mobile"),
    ),
    "US": (*((code, "toll_free") for code in sorted(_US_TOLL_FREE)), ("900", "premium")),
}


def _phone_kind(country: str, national: str) -> PhoneKind:
    if country not in _PHONE_PREFIXES:
        return "unknown"
    return next((kind for prefix, kind in _PHONE_PREFIXES[country] if national.startswith(prefix)), "local")


def _phones(scan: _Scan, business: str, orgs: list[dict[str, Any]], host: str) -> list[Phone]:
    raw = [href.strip()[4:] for href, _ in scan.links if href.strip().lower().startswith("tel:")]
    raw += _ld_strings(orgs, "telephone")
    # Not a bank account number written in groups ("IBAN NL91 ABNA 0417 1643 00").
    raw += [
        m.group()
        for m in _PHONE_RE.finditer(business)
        if not _BANK_RE.search(business[max(m.start() - 30, 0) : m.start()])
    ]
    phones: list[Phone] = []
    seen: set[str] = set()
    for candidate in raw:
        found = phone(candidate, host)
        if found is not None and found.number not in seen:
            seen.add(found.number)
            phones.append(found)
    return phones[:20]


def email(raw: str, host: str) -> Email | None:
    """
    A published e-mail address and whether it is on the page's own domain or a webmail service.

    :param raw: The address (from a ``mailto:`` link, structured data or a contact area).
    :param host: The page's host, without ``www.``.
    :return: The e-mail, or ``None`` for addresses that belong to platforms, libraries and examples.
    """
    address = raw.strip().strip(".").lower()
    if "@" not in address or _NOT_A_BUSINESS_EMAIL_RE.search(address) or _FILE_SUFFIX_RE.search(address):
        return None
    domain = address.rsplit("@", 1)[1]
    if domain in WEBMAIL_DOMAINS:
        return Email(address=address[:254], kind="webmail")
    own = bool(host) and (domain == host or host.endswith("." + domain) or domain.endswith("." + host))
    return Email(address=address[:254], kind="own_domain" if own else "other")


def _emails(scan: _Scan, business: str, orgs: list[dict[str, Any]], host: str) -> list[Email]:
    raw = [href.strip()[7:].split("?")[0] for href, _ in scan.links if href.strip().lower().startswith("mailto:")]
    raw += _ld_strings(orgs, "email")
    raw += [m.group(1) for m in _EMAIL_RE.finditer(business)]
    emails: list[Email] = []
    seen: set[str] = set()
    for candidate in raw:
        found = email(candidate, host)
        if found is not None and found.address not in seen:
            seen.add(found.address)
            emails.append(found)
    return emails[:20]


def _legal_form(names: list[str]) -> str:
    for name in names:
        if m := _LEGAL_FORM_RE.search(name):
            return m.group(1).replace(".", "").upper()[:20]
    return ""


def _licences(text: str) -> list[Licence]:
    licences: list[Licence] = []
    seen: set[str] = set()
    for m in _LICENCE_RE.finditer(text):
        number = m.group(2)
        if number in seen:
            continue
        seen.add(number)
        state = m.group(1) if m.group(1) in US_STATES else ""
        licences.append(Licence(number=number[:40], state=state))
    return licences[:10]


def _internal_links(links: list[tuple[str, str]], url: str, host: str) -> list[Link]:
    found: list[Link] = []
    seen: set[str] = set()
    for raw, text in links:
        href = raw.strip()
        if not href or href.startswith("#") or re.match(r"(?:mailto|tel|javascript|data):", href, re.IGNORECASE):
            continue
        target = urljoin(url, href)
        target_host = _host(target)
        if (target_host and target_host != host) or target in seen:
            continue
        seen.add(target)
        found.append(Link(text=text[:300], url=target[:500]))
        if len(found) >= 100:
            break
    return found
