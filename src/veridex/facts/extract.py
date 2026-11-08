"""
Extract :class:`~veridex.facts.schema.PageFacts` from raw HTML.

One parse per page: :func:`parse_page` reads the facts from the soup first and then strips it down to the plain
text the LLM sees, so the HTML is parsed once however many skills look at it. Platform-specific extractors
(Amazon, bol.com) fill in what generic markup (JSON-LD, microdata, Open Graph) doesn't carry.
"""

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from veridex.facts import amazon, bolcom
from veridex.facts._parse import parse_date, parse_number, parse_rating
from veridex.facts.schema import Checkbox, PageFacts, Platform, Review
from veridex.html_cleaner import strip_to_text

_HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])"
    r"|(?:left|top|text-indent)\s*:\s*-\d{3,}px",
    re.IGNORECASE,
)
_TIMER_ATTR_RE = re.compile(r"countdown|count-down|timer|hurry|urgency|limited-time|flash-?sale", re.IGNORECASE)
_TIMER_SCRIPT_RE = re.compile(
    r"countdown|setInterval\s*\(.{0,200}?(?:hours|minutes|seconds)|evergreen|deadline\s*=", re.IGNORECASE | re.DOTALL
)
_FEE_RE = re.compile(
    r"[^.\n]{0,80}(?:service fee|handling fee|processing fee|booking fee|convenience fee|admin(?:istration)? fee"
    r"|shipping (?:is )?calculated at checkout|taxes and shipping calculated|plus shipping|excl\.? shipping"
    r"|verzendkosten|servicekosten|administratiekosten|bearbeitungsgebühr|servicegebühr|zzgl\.? versand"
    r"|frais de (?:service|dossier|traitement)|gastos de (?:gestión|envío)|spese di (?:gestione|spedizione))"
    r"[^.\n]{0,80}",
    re.IGNORECASE,
)
# EU VAT numbers per country (VIES formats), matched against a candidate with its separators removed.
_VAT_RE = re.compile(
    r"ATU\d{8}|BE0?\d{9}|BG\d{9,10}|CY\d{8}[A-Z]|CZ\d{8,10}|DE\d{9}|DK\d{8}|EE\d{9}|EL\d{9}|ES[A-Z0-9]\d{7}[A-Z0-9]"
    r"|FI\d{8}|FR[A-HJ-NP-Z0-9]{2}\d{9}|HR\d{11}|HU\d{8}|IE\d[A-Z0-9+*]\d{5}[A-Z]{1,2}|IT\d{11}|LT\d{9,12}|LU\d{8}"
    r"|LV\d{11}|MT\d{8}|NL\d{9}B\d{2}|PL\d{10}|PT\d{9}|RO\d{2,10}|SE\d{12}|SI\d{8}|SK\d{10}|XI\d{9}"
)
# A country prefix followed by a run that may contain spaces or dots ("DE 123 456 789", "NL 1234.56.789.B01").
_VAT_CANDIDATE_RE = re.compile(
    r"(?<![A-Z0-9])(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK|XI)"
    r"[\s.]?[0-9A-Z][0-9A-Z\s.+*]{6,18}"
)
_LEGAL_RE = re.compile(
    r"[^.\n]{0,60}(?:impressum|imprint|kvk|kamer van koophandel|handelsregister|registre du commerce|company number"
    r"|registered in|chamber of commerce|withdraw(?:al)?|widerruf|herroeping|bedenktijd|rétractation|desistimiento"
    r"|recesso|14[ -](?:day|dagen|tage|jours|días|giorni))[^.\n]{0,100}",
    re.IGNORECASE,
)
_POLICY_KINDS: dict[str, re.Pattern[str]] = {
    "refund": re.compile(r"refund|return|retour|widerruf|rétractation|devoluci|reso", re.IGNORECASE),
    "shipping": re.compile(r"shipping|delivery|verzend|versand|livraison|envío|spedizion", re.IGNORECASE),
    "terms": re.compile(r"terms|conditions|voorwaarden|\bagb\b|cgv|condiciones|termini", re.IGNORECASE),
    "privacy": re.compile(r"privacy|datenschutz|confidentialit|privacidad", re.IGNORECASE),
    "contact": re.compile(r"contact|kontakt|contacto|contatti", re.IGNORECASE),
    "imprint": re.compile(r"impressum|imprint|legal notice|mentions légales|colofon|aviso legal", re.IGNORECASE),
}
_SHOPIFY_RE = re.compile(r"cdn\.shopify\.com|Shopify\.theme|myshopify\.com", re.IGNORECASE)
_WOO_RE = re.compile(r"woocommerce|wp-content/plugins/woocommerce", re.IGNORECASE)
_AMAZON_HOST_RE = re.compile(r"(?:^|[\s.])amazon\.[a-z.]+")
_BOL_HOST_RE = re.compile(r"(?:^|[\s.])bol\.com")
_COMPARE_AT_RE = re.compile(r'"compare_at_price"\s*:\s*"?(\d+(?:\.\d+)?)"?')
_WS_RE = re.compile(r"\s+")
_SKIP_HIDDEN = {"script", "style", "noscript", "template", "head", "meta", "link", "svg", "input"}

_MAX_HIDDEN_CHARS = 300
_MAX_SNIPPETS = 20


def parse_page(html: str, url: str = "") -> tuple[PageFacts, str]:
    """
    Extract the facts from a page and the plain text for the LLM, parsing the HTML once.

    :param html: Raw HTML of the page.
    :param url: The page's URL, used to resolve relative links and to recognise the platform.
    :return: The facts and the cleaned page text.
    """
    soup = BeautifulSoup(html, "html.parser")
    facts = extract_facts(soup, html, url)
    return facts, strip_to_text(soup)


def extract_facts(soup: BeautifulSoup, html: str, url: str = "") -> PageFacts:
    """
    Extract the facts from a parsed page without modifying it.

    :param soup: The parsed page.
    :param html: The raw HTML (some signals live in script text the soup keeps as strings).
    :param url: The page's URL.
    :return: The facts.
    """
    facts = PageFacts(url=url, platform=_platform(soup, html, url))
    _scripts_and_meta(soup, url, facts)
    _hidden_text(soup, facts)
    facts.checkboxes = _checkboxes(soup)[:100]
    _timers(soup, facts)
    _text_signals(soup, facts)
    _policy_links(soup, url, facts)
    _images(soup, url, facts)
    _structured_data(soup, facts)
    if facts.platform == "amazon":
        amazon.extract(soup, facts)
    elif facts.platform == "bol.com":
        bolcom.extract(soup, facts)
    elif facts.platform == "shopify":
        _shopify(html, facts)
    if not facts.title:
        facts.title = _text(soup.find("h1")) or _meta(soup, "og:title")
    if not facts.image_urls and (image := _meta(soup, "og:image")):
        facts.image_urls = [urljoin(url, image)]
    return facts


def _platform(soup: BeautifulSoup, html: str, url: str) -> Platform:
    host = (urlparse(url).hostname or "").removeprefix("www.")
    canonical = soup.find("link", rel="canonical")
    canonical_host = (urlparse(str(canonical.get("href", ""))).hostname or "") if isinstance(canonical, Tag) else ""
    hosts = f"{host} {canonical_host}"
    if _AMAZON_HOST_RE.search(hosts) or (soup.find(id="productTitle") and soup.find(id="dp")):
        return "amazon"
    if _BOL_HOST_RE.search(hosts):
        return "bol.com"
    if _SHOPIFY_RE.search(html):
        return "shopify"
    if _WOO_RE.search(html):
        return "woocommerce"
    return "generic"


def _scripts_and_meta(soup: BeautifulSoup, url: str, facts: PageFacts) -> None:
    facts.script_srcs = [urljoin(url, str(s["src"])) for s in soup.find_all("script", src=True)][:300]
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("name") or tag.get("property") or tag.get("http-equiv")
        content = tag.get("content")
        if key and content and len(meta) < 100:
            meta[str(key).lower()[:100]] = str(content)[:300]
    facts.meta = meta


def _hidden_text(soup: BeautifulSoup, facts: PageFacts) -> None:
    hidden: list[str] = []
    for tag in soup.find_all(True):
        if tag.name in _SKIP_HIDDEN:
            continue
        style = str(tag.get("style", ""))
        if tag.has_attr("hidden") or (style and _HIDDEN_STYLE_RE.search(style)):
            text = _text(tag)
            if len(text) >= 20:
                hidden.append(text[:_MAX_HIDDEN_CHARS])
    facts.hidden_text = _dedupe(hidden)[:100]


def _checkboxes(soup: BeautifulSoup) -> list[Checkbox]:
    labels_for = {str(lab["for"]): _text(lab) for lab in soup.find_all("label", attrs={"for": True})}
    boxes: list[Checkbox] = []
    for box in soup.find_all("input", attrs={"type": re.compile("^checkbox$", re.IGNORECASE)}):
        label = labels_for.get(str(box.get("id", "")), "")
        if not label and (parent := box.find_parent("label")):
            label = _text(parent)
        if not label and box.parent is not None:
            label = _text(box.parent)
        boxes.append(Checkbox(label=label[:300], name=str(box.get("name", ""))[:300], checked=box.has_attr("checked")))
    return boxes


def _timers(soup: BeautifulSoup, facts: PageFacts) -> None:
    elements = soup.find_all(True, attrs={"class": _TIMER_ATTR_RE}) + soup.find_all(True, id=_TIMER_ATTR_RE)
    timers = [
        f"<{tag.name} {_attr_summary(tag)}> {_text(tag)[:80]}".strip()
        for tag in elements
        if tag.name not in {"script", "style"}
    ]
    for script in soup.find_all("script", src=False):
        body = script.string or ""
        if m := _TIMER_SCRIPT_RE.search(body):
            timers.append(f"script: {_squash(body[max(0, m.start() - 60) : m.end() + 120])}")
    timers.extend(f"script src: {src}" for src in facts.script_srcs if _TIMER_ATTR_RE.search(src))
    facts.timers = _dedupe(timers)[:50]


def _text_signals(soup: BeautifulSoup, facts: PageFacts) -> None:
    text = _squash(soup.get_text(" "))
    facts.fee_mentions = _dedupe(_squash(m.group()) for m in _FEE_RE.finditer(text))[:_MAX_SNIPPETS]
    facts.vat_ids = _dedupe(_vat_ids(text))[:10]
    facts.legal_mentions = _dedupe(_squash(m.group()) for m in _LEGAL_RE.finditer(text))[:_MAX_SNIPPETS]


def _vat_ids(text: str) -> list[str]:
    found: list[str] = []
    for m in _VAT_CANDIDATE_RE.finditer(text):
        squashed = re.sub(r"[\s.]", "", m.group())
        # The run may swallow what follows the number; keep its longest prefix that is a valid VAT format.
        for end in range(len(squashed), 7, -1):
            if _VAT_RE.fullmatch(squashed[:end]):
                found.append(squashed[:end])
                break
    return found


def _policy_links(soup: BeautifulSoup, url: str, facts: PageFacts) -> None:
    links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        label = f"{_text(a)} {a['href']}"
        for kind, pattern in _POLICY_KINDS.items():
            if kind not in links and pattern.search(label):
                links[kind] = urljoin(url, str(a["href"]))[:500]
    facts.policy_links = links


def _images(soup: BeautifulSoup, url: str, facts: PageFacts) -> None:
    images: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("data-old-hires") or img.get("src") or img.get("data-src")
        if isinstance(src, str) and src and not src.startswith("data:") and not src.lower().endswith(".svg"):
            images.append(urljoin(url, src))
    facts.image_urls = _dedupe(images)[:50]


def _json_ld_products(soup: BeautifulSoup) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []

    def walk(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kind = node.get("@type", "")
            if kind == "Product" or (isinstance(kind, list) and "Product" in kind):
                products.append(node)
            for key in ("@graph", "mainEntity", "itemListElement"):
                if key in node:
                    walk(node[key])

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or ""))
        except ValueError, TypeError:
            continue
    return products


def _structured_data(soup: BeautifulSoup, facts: PageFacts) -> None:
    products = _json_ld_products(soup)
    facts.json_ld = [_trim(p) for p in products[:20]]
    if products:
        product = products[0]
        facts.title = facts.title or str(product.get("name") or "")[:2000]
        brand = product.get("brand")
        facts.brand = facts.brand or str(brand.get("name", "") if isinstance(brand, dict) else brand or "")[:300]
        _apply_offer(product.get("offers"), facts)
        _apply_rating(product.get("aggregateRating"), facts)
        _apply_reviews(product.get("review"), facts)
        _apply_images(product.get("image"), facts)
    if facts.price is None:
        facts.price = _number(_meta(soup, "product:price:amount") or _meta(soup, "og:price:amount"))
        facts.currency = facts.currency or _meta(soup, "product:price:currency") or _meta(soup, "og:price:currency")
    if not facts.reviews:
        _microdata_reviews(soup, facts)


def _apply_offer(offers: object, facts: PageFacts) -> None:
    offer = offers[0] if isinstance(offers, list) and offers else offers
    if not isinstance(offer, dict):
        return
    if facts.price is None:
        facts.price = _number(offer.get("price") or offer.get("lowPrice"))
    facts.currency = facts.currency or str(offer.get("priceCurrency") or "")[:8]
    seller = offer.get("seller")
    if isinstance(seller, dict) and not facts.seller:
        facts.seller = str(seller.get("name") or "")[:300]


def _apply_rating(rating: object, facts: PageFacts) -> None:
    if not isinstance(rating, dict):
        return
    if facts.rating is None:
        facts.rating = _number(rating.get("ratingValue"), 5)
    if facts.rating_count is None:
        count = _number(rating.get("ratingCount") or rating.get("reviewCount"))
        facts.rating_count = int(count) if count is not None else None


def _apply_reviews(reviews: object, facts: PageFacts) -> None:
    items = reviews if isinstance(reviews, list) else [reviews] if isinstance(reviews, dict) else []
    for r in items[:100]:
        if not isinstance(r, dict):
            continue
        rating_node = r.get("reviewRating")
        stars = rating_node.get("ratingValue") if isinstance(rating_node, dict) else rating_node
        facts.reviews.append(
            Review(
                rating=_number(stars, 5),
                posted=parse_date(str(r.get("datePublished") or "")),
                title=str(r.get("name") or "")[:300],
                body=str(r.get("reviewBody") or "")[:2000],
            )
        )


def _apply_images(images: object, facts: PageFacts) -> None:
    items = images if isinstance(images, list) else [images] if images else []
    found = [i.get("url") if isinstance(i, dict) else i for i in items]
    urls = [u for u in found if isinstance(u, str) and u]
    facts.image_urls = _dedupe([*urls, *facts.image_urls])[:50]


def _microdata_reviews(soup: BeautifulSoup, facts: PageFacts) -> None:
    for container in soup.find_all(itemprop="review")[:100]:
        rating = container.find(itemprop="ratingValue")
        posted = container.find(itemprop="datePublished")
        body = container.find(itemprop="reviewBody")
        facts.reviews.append(
            Review(
                rating=parse_rating(str(rating.get("content") or _text(rating))) if isinstance(rating, Tag) else None,
                posted=parse_date(str(posted.get("content") or _text(posted))) if isinstance(posted, Tag) else None,
                body=_text(body)[:2000] if isinstance(body, Tag) else "",
            )
        )


def _shopify(html: str, facts: PageFacts) -> None:
    # The struck-through "was" price. Shopify's product JSON stores it in cents; only accept a value that is at
    # least the listed price, so a mismatched or empty compare-at price is never reported as a discount.
    if facts.list_price is not None or facts.price is None or not (m := _COMPARE_AT_RE.search(html)):
        return
    cents = float(m.group(1))
    for candidate in (round(cents / 100, 2), cents):
        if candidate > facts.price:
            facts.list_price = candidate
            return


def _trim(node: dict[str, Any]) -> dict[str, Any]:
    keep = ("@type", "name", "brand", "sku", "gtin", "gtin13", "gtin12", "gtin8", "mpn", "offers", "aggregateRating")
    trimmed = {k: node[k] for k in keep if k in node}
    raw = json.dumps(trimmed, default=str)
    return trimmed if len(raw) <= 4_000 else {k: trimmed[k] for k in ("@type", "name", "brand") if k in trimmed}


def _meta(soup: BeautifulSoup, key: str) -> str:
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return str(tag.get("content", "")) if isinstance(tag, Tag) else ""


def _number(value: object, maximum: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    number = float(value) if isinstance(value, int | float) else parse_number(str(value))
    if number is None or number < 0 or (maximum is not None and number > maximum):
        return None
    return number


def _text(tag: object) -> str:
    return _squash(tag.get_text(" ")) if isinstance(tag, Tag) else ""


def _squash(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _attr_summary(tag: Tag) -> str:
    parts = [f'id="{tag["id"]}"'] if tag.get("id") else []
    if classes := tag.get("class"):
        parts.append(f'class="{" ".join(classes) if isinstance(classes, list) else classes}"')
    return " ".join(parts)[:120]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
