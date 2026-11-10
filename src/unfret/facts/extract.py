"""
Extract :class:`~unfret.facts.schema.PageFacts` from raw HTML.

One parse per page: :func:`parse_page` reads the facts from the soup first and then strips it down to the plain
text the LLM sees, so the HTML is parsed once however many skills look at it. Platform-specific extractors
(Amazon, bol.com, Walmart) fill in what generic markup (JSON-LD, microdata, Open Graph) doesn't carry.
"""

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup, Tag

from unfret.facts import amazon, bolcom, walmart
from unfret.facts._parse import parse_date, parse_number, parse_rating
from unfret.facts.schema import Checkbox, PageFacts, Platform, Review
from unfret.html_cleaner import strip_to_text

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
    r"(?:service fee|handling fee|processing fee|booking fee|convenience fee|admin(?:istration)? fee"
    r"|shipping (?:is )?calculated at checkout|taxes and shipping calculated|plus shipping|excl\.? shipping"
    r"|verzendkosten|servicekosten|administratiekosten|bearbeitungsgebühr|servicegebühr|zzgl\.? versand"
    r"|frais de (?:service|dossier|traitement)|gastos de (?:gestión|envío)|spese di (?:gestione|spedizione))",
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
    r"(?:impressum|imprint|kvk|kamer van koophandel|handelsregister|registre du commerce|company number"
    r"|registered in|chamber of commerce|withdraw(?:al)?|widerruf|herroeping|bedenktijd|rétractation|desistimiento"
    r"|recesso|14[ -](?:day|dagen|tage|jours|días|giorni))",
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
_WALMART_HOST_RE = re.compile(r"(?:^|[\s.])walmart\.com\b")
_COMPARE_AT_RE = re.compile(r'"compare_at_price"\s*:\s*("?)(\d+(?:\.\d+)?)\1')
_VARIANT_PRICE_RE = re.compile(r'"price"\s*:\s*("?)(\d+(?:\.\d+)?)\1')
_VARIANT_ID_RE = re.compile(r'"id"\s*:\s*"?(\d{6,})')
_VARIANT_WINDOW = 1_500  # chars around a compare-at price that belong to the same variant object
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
    elif facts.platform == "walmart":
        walmart.extract(soup, facts)
    elif facts.platform == "shopify":
        _shopify(html, facts, url)
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
    if _WALMART_HOST_RE.search(hosts):
        return "walmart"
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
    facts.fee_mentions = _dedupe(_snippets(_FEE_RE, text, 80, 80))[:_MAX_SNIPPETS]
    facts.vat_ids = _dedupe(_vat_ids(text))[:10]
    facts.legal_mentions = _dedupe(_snippets(_LEGAL_RE, text, 60, 100))[:_MAX_SNIPPETS]


def _snippets(pattern: re.Pattern[str], text: str, before: int, after: int) -> Iterable[str]:
    # The keyword plus its sentence (up to `before`/`after` chars either side). Widening by slicing instead of a
    # leading `[^.]{0,80}` in the pattern keeps matching linear: that prefix retried 80 starts at every character.
    for m in pattern.finditer(text):
        lo, hi = max(m.start() - before, 0), min(m.end() + after, len(text))
        head = text[lo : m.start()]
        tail = text[m.end() : hi]
        head = head[head.rfind(".") + 1 :] if "." in head else head
        cut = tail.find(".")
        yield _squash(head + m.group() + (tail[:cut] if cut >= 0 else tail))


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


# A product with variants is a schema.org ProductGroup (Google's variant markup, which Shopify themes emit) whose
# offers sit on its variants (``hasVariant``); a product page may also state its rating in a separate block whose
# ``itemReviewed`` is the product.
_PRODUCT_TYPES = frozenset({"Product", "ProductGroup"})


def _kinds(node: dict[str, Any]) -> set[str]:
    kind = node.get("@type", "")
    return {k for k in (kind if isinstance(kind, list) else [kind]) if isinstance(k, str) and k}


def _json_ld(soup: BeautifulSoup) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    products: list[dict[str, Any]] = []
    ratings: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            _walk_json_ld(json.loads(script.string or ""), products, ratings)
        except ValueError, TypeError:
            continue
    return products, ratings


def _walk_json_ld(node: object, products: list[dict[str, Any]], ratings: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json_ld(item, products, ratings)
    elif isinstance(node, dict):
        if _kinds(node) & _PRODUCT_TYPES:
            products.append(node)
        elif (rating := _product_rating(node)) is not None:
            ratings.append(rating)
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                _walk_json_ld(node[key], products, ratings)


def _about_a_product(item: object) -> bool:
    return isinstance(item, dict) and bool(_kinds(item) & _PRODUCT_TYPES)


def _product_rating(node: dict[str, Any]) -> dict[str, Any] | None:
    # An AggregateRating of a product, on its own or wrapped in an untyped block; never a shop's own rating.
    kinds = _kinds(node)
    rating = node if "AggregateRating" in kinds else node.get("aggregateRating") if not kinds else None
    return rating if isinstance(rating, dict) and _about_a_product(rating.get("itemReviewed")) else None


def _variant_offers(product: dict[str, Any]) -> object:
    variants = product.get("hasVariant")
    for variant in variants if isinstance(variants, list) else [variants]:
        if isinstance(variant, dict) and variant.get("offers"):
            return variant["offers"]
    return None


def _structured_data(soup: BeautifulSoup, facts: PageFacts) -> None:
    products, ratings = _json_ld(soup)
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
    if ratings:
        _apply_rating(ratings[0], facts)
    if facts.price is None:
        facts.price = _number(_meta(soup, "product:price:amount") or _meta(soup, "og:price:amount"))
        facts.currency = facts.currency or _meta(soup, "product:price:currency") or _meta(soup, "og:price:currency")
    if facts.price is None and products:
        # A ProductGroup's own price is its variants' (the page's Open Graph price, read above, is the one on show).
        _apply_offer(_variant_offers(products[0]), facts)
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


def _shopify_amount(digits: str) -> float:
    # Shopify's product JSON stores integer amounts in cents; a decimal ("29.99", from a theme's money filter) is
    # already in the currency. An integer is never read as whole units: that turned "2495" into a 99% discount.
    return float(digits) if "." in digits else round(int(digits) / 100, 2)


def shopify_compare_at(html: str, price: float, url: str = "") -> float | None:
    """
    The struck-through "was" price of the variant on show, from Shopify's product JSON.

    Each compare-at price is bound to its variant: the one named by ``?variant=`` in the URL, else the one whose
    own price is the listed price. Without such a match, only a compare-at price every variant shares counts. A
    value at or below the listed price (an empty or stale compare-at) is never a discount.

    :param html: The page HTML.
    :param price: The listed price.
    :param url: The page URL (for ``?variant=``).
    :return: The was-price, or ``None``.
    """
    variant = parse_qs(urlsplit(url).query).get("variant", [""])[0]
    candidates: list[tuple[float, bool, bool]] = []  # (was, is the URL's variant, own price is the listed price)
    for m in _COMPARE_AT_RE.finditer(html):
        lo = max(m.start() - _VARIANT_WINDOW, 0)
        ids = _VARIANT_ID_RE.findall(html, lo, m.start())
        prices = list(_VARIANT_PRICE_RE.finditer(html, lo, m.end() + _VARIANT_WINDOW))
        nearest = min(prices, key=lambda p: abs(p.start() - m.start()), default=None)
        own = _shopify_amount(nearest.group(2)) if nearest else None
        candidates.append((_shopify_amount(m.group(2)), bool(variant) and ids[-1:] == [variant], own == price))
    if not candidates:
        return None
    chosen = [c for c in candidates if c[1]] or [c for c in candidates if c[2]]
    values = {c[0] for c in (chosen or candidates)}
    if len(values) != 1:
        return None  # variants disagree and none is the one on show
    was = values.pop()
    return was if was > price else None


def _shopify(html: str, facts: PageFacts, url: str) -> None:
    if facts.list_price is None and facts.price is not None:
        facts.list_price = shopify_compare_at(html, facts.price, url)


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
