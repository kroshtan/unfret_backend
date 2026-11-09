"""
Amazon product detail pages, on any Amazon TLD.

Amazon's markup varies by locale and layout generation, so each fact tries the known variants in order (buybox
"tabular" rows, legacy merchant info, detail tables vs detail bullets, histogram rows vs aria labels) and leaves
the fact empty when none match.
"""

import re

from bs4 import BeautifulSoup, Tag

from veridex.facts._parse import parse_count, parse_date, parse_price, parse_rating
from veridex.facts.schema import PageFacts, Review

_BYLINE_RE = re.compile(
    r"^(?:visit the|besuchen sie den|visitez la boutique|bezoek de|visita la tienda de|visita lo store di)\s+(.+?)"
    r"(?:\s+store|-store|-shop|\s+boutique)?$|^(?:brand|marke|marque|merk|marca)\s*:\s*(.+)$",
    re.IGNORECASE,
)
_FIRST_AVAILABLE_RE = re.compile(
    r"date first available|im angebot von amazon|disponible depuis le|datum eerste beschikbaarheid"
    r"|producto en amazon desde|disponibile su amazon",
    re.IGNORECASE,
)
_BRAND_KEY_RE = re.compile(r"brand|marke|marque|merk|marca|manufacturer|hersteller", re.IGNORECASE)
_SHIPS_FROM_RE = re.compile(r"ships from|versand|expédié par|verzonden door|enviado desde|spedizione", re.IGNORECASE)
_SOLD_BY_RE = re.compile(r"sold by|verkäufer|vendu par|verkocht door|vendido por|venduto da", re.IGNORECASE)
_STAR_ROW_RE = re.compile(r"([1-5])\s*(?:star|stern|étoile|ster|estrella|stell)", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
_ARIA_HISTOGRAM_RE = re.compile(
    r"(\d{1,3})\s*(?:percent|prozent|pour cent|procent|por ciento|per cento).*?([1-5])", re.IGNORECASE
)
_WS_RE = re.compile(r"\s+")
# Media bylines carry the format after the artist ("Etta James (Artist) Format: Audio CD").
_FORMAT_SUFFIX_RE = re.compile(r"\s+(?:format|formaat|formato)\s*:.*$", re.IGNORECASE)
# Amazon wraps detail-table cells in left-to-right / right-to-left marks.
_MARKS = " :" + chr(0x200E) + chr(0x200F)

_PRICE_SELECTORS = (
    "#corePrice_feature_div .a-offscreen, #corePriceDisplay_desktop_feature_div .a-offscreen, "
    "#priceblock_ourprice, #priceblock_dealprice, .a-price .a-offscreen"
)
_LIST_PRICE_SELECTORS = ".basisPrice .a-offscreen, .a-text-price .a-offscreen, #listPrice, #priceblock_listprice"
_DETAIL_ROW_SELECTORS = (
    "#prodDetails tr, #productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr"
)
_OFFER_BLOCK_SELECTORS = (
    '[offer-display-feature-name$="-fulfiller-info"], [offer-display-feature-name$="-merchant-info"]'
)
_VARIATION_SELECTORS = "#twister li, #variation_color_name li, #variation_size_name li, #inline-twister li"
_REVIEW_STAR_SELECTORS = '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"], .a-icon-alt'


def extract(soup: BeautifulSoup, facts: PageFacts) -> None:
    """
    Fill Amazon-specific facts into ``facts`` (generic markup has already been read).

    :param soup: The parsed page.
    :param facts: The facts to complete; updated in place.
    """
    _title_and_brand(soup, facts)
    _prices(soup, facts)
    _ratings(soup, facts)
    _details(soup, facts)
    _seller(soup, facts)
    _reviews(soup, facts)
    _variations_and_images(soup, facts)


def _title_and_brand(soup: BeautifulSoup, facts: PageFacts) -> None:
    facts.title = _text(soup.find(id="productTitle")) or facts.title
    byline = _FORMAT_SUFFIX_RE.sub("", _text(soup.find(id="bylineInfo")))
    if byline:
        m = _BYLINE_RE.match(byline)
        facts.brand = ((m.group(1) or m.group(2) or "") if m else byline).strip()[:300]


def _prices(soup: BeautifulSoup, facts: PageFacts) -> None:
    if price_tag := soup.select_one(_PRICE_SELECTORS):
        price, currency = parse_price(_text(price_tag))
        facts.price = price if price is not None else facts.price
        facts.currency = currency or facts.currency
    if list_tag := soup.select_one(_LIST_PRICE_SELECTORS):
        list_price, _ = parse_price(_text(list_tag))
        if list_price is not None and (facts.price is None or list_price > facts.price):
            facts.list_price = list_price


def _ratings(soup: BeautifulSoup, facts: PageFacts) -> None:
    if (rating := parse_rating(_text(soup.select_one("#averageCustomerReviews .a-icon-alt")))) is not None:
        facts.rating = rating
    if (count := parse_count(_text(soup.find(id="acrCustomerReviewText")))) is not None:
        facts.rating_count = count
    facts.rating_histogram = _histogram(soup) or facts.rating_histogram


def _details(soup: BeautifulSoup, facts: PageFacts) -> None:
    for key, value in _detail_rows(soup).items():
        if _FIRST_AVAILABLE_RE.search(key):
            facts.date_first_available = parse_date(value)
        elif not facts.brand and _BRAND_KEY_RE.match(key):
            facts.brand = value[:300]


def _seller(soup: BeautifulSoup, facts: PageFacts) -> None:
    for key, value in _buybox_rows(soup).items():
        if _SHIPS_FROM_RE.search(key):
            facts.ships_from = value[:300]
        elif _SOLD_BY_RE.search(key):
            facts.seller = value[:300]
    if not facts.seller:
        facts.seller = _text(soup.find(id="sellerProfileTriggerId") or soup.select_one("#merchant-info a"))[:300]


def _reviews(soup: BeautifulSoup, facts: PageFacts) -> None:
    reviews = [
        Review(
            rating=parse_rating(_text(node.select_one(_REVIEW_STAR_SELECTORS))),
            posted=parse_date(_text(node.select_one('[data-hook="review-date"]'))),
            title=_text(node.select_one('[data-hook="review-title"]'))[:300],
            body=_text(node.select_one('[data-hook="review-body"]'))[:2000],
            verified=node.select_one('[data-hook^="avp-badge"]') is not None,
        )
        for node in soup.select('[data-hook="review"]')[:100]
    ]
    facts.reviews = reviews or facts.reviews


def _variations_and_images(soup: BeautifulSoup, facts: PageFacts) -> None:
    variations = [
        str(li.get("title") or _text(li)).removeprefix("Click to select ") for li in soup.select(_VARIATION_SELECTORS)
    ]
    facts.variations = [v[:200] for v in variations if v][:100] or facts.variations
    image = soup.find(id="landingImage")
    if isinstance(image, Tag):
        src = image.get("data-old-hires") or image.get("src")
        if isinstance(src, str) and src:
            facts.image_urls = [src, *[u for u in facts.image_urls if u != src]][:50]


def _histogram(soup: BeautifulSoup) -> dict[int, float]:
    shares: dict[int, float] = {}
    for row in soup.select("#histogramTable tr, #histogramTable li, #cm_cr_dp_d_rating_histogram li"):
        text = _text(row)
        aria = " ".join(str(a.get("aria-label", "")) for a in row.select("[aria-label]"))
        if aria and (m := _ARIA_HISTOGRAM_RE.search(aria)):
            shares[int(m.group(2))] = int(m.group(1)) / 100
        elif (star := _STAR_ROW_RE.search(text)) and (pct := _PERCENT_RE.search(text)):
            shares[int(star.group(1))] = int(pct.group(1)) / 100
    return shares if len(shares) >= 3 else {}


def _detail_rows(soup: BeautifulSoup) -> dict[str, str]:
    rows: dict[str, str] = {}
    for tr in soup.select(_DETAIL_ROW_SELECTORS):
        key, value = tr.find("th"), tr.find("td")
        if isinstance(key, Tag) and isinstance(value, Tag):
            rows[_text(key).strip(_MARKS)] = _text(value).strip(_MARKS)
    for li in soup.select("#detailBullets_feature_div li"):
        parts = [_text(span) for span in li.select("span.a-text-bold, span.a-list-item > span")]
        if len(parts) >= 2:
            rows[parts[0].strip(_MARKS)] = parts[-1].strip(_MARKS)
    return rows


def _buybox_rows(soup: BeautifulSoup) -> dict[str, str]:
    rows: dict[str, str] = {}
    for row in soup.select("#tabular-buybox tr, #tabular-buybox .tabular-buybox-container > div"):
        cells = [_text(c) for c in row.select("td, .tabular-buybox-text")]
        if len(cells) >= 2:
            rows[cells[0]] = cells[1]
    for label, value in _offer_display_rows(soup).items():
        rows.setdefault(label, value)
    merchant = _text(soup.find(id="merchant-info"))
    if merchant and (m := re.search(r"ships from (.+?) and sold by (.+?)\.", merchant, re.IGNORECASE)):
        rows.setdefault("Ships from", m.group(1))
        rows.setdefault("Sold by", m.group(2))
    elif merchant and (m := re.search(r"ships from and sold by (.+?)\.", merchant, re.IGNORECASE)):
        rows.setdefault("Ships from", m.group(1))
        rows.setdefault("Sold by", m.group(1))
    return rows


def _offer_display_rows(soup: BeautifulSoup) -> dict[str, str]:
    # The offer display shows each feature as a label element and a text element, either nested in one block or
    # as two siblings that share the feature name (the current amazon.com layout).
    rows: dict[str, str] = {}
    labels: dict[str, str] = {}
    values: dict[str, str] = {}
    for block in soup.select(_OFFER_BLOCK_SELECTORS):
        feature = str(block.get("offer-display-feature-name", ""))
        classes: list[str] = list(block.get("class") or [])
        label = _text(block.select_one(".offer-display-feature-label"))
        value = _text(block.select_one(".offer-display-feature-text-message, .offer-display-feature-text"))
        if label and value:
            rows[label] = value
        elif "offer-display-feature-label" in classes:
            labels.setdefault(feature, _text(block))
        elif "offer-display-feature-text" in classes:
            values.setdefault(feature, _text(block.select_one(".offer-display-feature-text-message") or block))
    for feature, label in labels.items():
        if label and values.get(feature):
            rows.setdefault(label, values[feature])
    return rows


def _text(tag: object) -> str:
    return _WS_RE.sub(" ", tag.get_text(" ")).strip() if isinstance(tag, Tag) else ""
