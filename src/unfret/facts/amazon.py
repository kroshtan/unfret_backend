"""
Amazon product detail pages, on any Amazon TLD.

Amazon's markup varies by locale and layout generation, so each fact tries the known variants in order (buybox
"tabular" rows, legacy merchant info, detail tables vs detail bullets, histogram rows vs aria labels) and leaves
the fact empty when none match.
"""

import json
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup, Tag

from unfret.facts._parse import parse_count, parse_date, parse_price, parse_rating
from unfret.facts.brand import clean_brand
from unfret.facts.schema import PageFacts, Review

_FIRST_AVAILABLE_RE = re.compile(
    r"date first available|im angebot von amazon|disponible depuis le|datum eerste beschikbaarheid"
    r"|producto en amazon desde|disponibile su amazon",
    re.IGNORECASE,
)
_BRAND_KEY_RE = re.compile(r"brand|marke|marque|merk|marca|manufacturer|hersteller", re.IGNORECASE)
# A row that names the brand itself, not its manufacturer.
_BRAND_ONLY_RE = re.compile(r"(?:brand|marke|marque|merk|marca|marka|märke|varumärke|marchio)$", re.IGNORECASE)
# The byline link's search filter by brand (``field-brandtextbin=Duracell``); a store link's path is the store's
# name, which isn't always the brand's ("AmazonBasics" for Amazon Basics).
_BYLINE_BRAND_PARAMS = ("field-brandtextbin", "field-brand")
# The buybox's labels for who ships and who sells, in Amazon's languages. The current offer box names both in one
# label when one company does both ("Shipper / Seller", "Verzender / Verkoper", "Versender / Verkäufer",
# "Expéditeur / Vendeur"): such a row fills both facts.
_SHIPS_FROM_RE = re.compile(
    r"ships from|shipper|dispatches from|versand|versender|expédié par|expéditeur|verzonden door|verzender"
    r"|enviado desde|enviado por|remitente|spedizion|spedito da|mittente|wysyłka|nadawca|skickas från|avsändare"
    r"|gönderici",
    re.IGNORECASE,
)
_SOLD_BY_RE = re.compile(
    r"sold by|seller|verkäufer|verkauf durch|vendu par|vendeur|verkocht door|verkoper|vendido por|vendedor"
    r"|venduto da|venditore|sprzedawca|sprzedawane przez|säljs av|säljare|satıcı",
    re.IGNORECASE,
)
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
# The price box of the current layouts (desktop and mobile). The price to pay comes first: its accessibility label
# ("€ 20,99 met een besparing van 30 procent"), then its offscreen copy, then its visible parts (symbol, whole,
# fraction): the mobile page (what a phone, and the Android app's WebView, is sent) leaves the offscreen copy empty.
# Only then the older layouts' selectors, which also match a sponsored product's price or a price per unit.
_CORE_PRICE = (
    "#corePrice_feature_div",
    "#corePriceDisplay_desktop_feature_div",
    "#corePriceDisplay_mobile_feature_div",
    "#corePrice_desktop",
    "#corePrice_mobile_feature_div",
)
_PRICE_TO_PAY_LABEL = "#apex-pricetopay-accessibility-label"
_PRICE_TO_PAY_OFFSCREEN = ", ".join(f"{box} .priceToPay .a-offscreen" for box in _CORE_PRICE)
_PRICE_TO_PAY = ", ".join(f"{box} .priceToPay" for box in _CORE_PRICE)
# The reference price, under whatever label the locale gives it ("List Price", "Typical price", "RRP", "UVP",
# "Adviesprijs", "Prix conseillé", "Was"): Amazon marks it as the basis price and strikes it through. A struck price
# in the price box is the fallback, then the older layouts' list price. Each must be above the price to pay (the
# price box also holds a price per unit, "0,04 €/Stück"). A page without the price box (an older layout) has its
# struck price anywhere: the last resort there.
_LIST_PRICE_CANDIDATES = (
    ".basisPrice .a-offscreen",
    ", ".join(f'{box} [data-a-strike="true"] .a-offscreen' for box in _CORE_PRICE),
    "#listPrice, #priceblock_listprice",
    ", ".join(f"{box} .a-text-price .a-offscreen" for box in _CORE_PRICE),
)
_OLD_LIST_PRICE = ".a-text-price .a-offscreen"
_RATING_SELECTORS = (
    "#averageCustomerReviews .a-icon-alt, #acrCustomerReviewLink .a-icon-alt, "
    '[data-hook="average-stars-rating-anywhere"] .a-icon-alt'
)
_RATING_COUNT_SELECTORS = '#acrCustomerReviewText, [data-hook="total-rating-count"]'
_DETAIL_ROW_SELECTORS = (
    "#prodDetails tr, #productDetails_detailBullets_sections1 tr, #productDetails_techSpec_section_1 tr"
)
# The mobile page names a feature's text element by ``offer-display-attribute-name`` instead.
_OFFER_BLOCK_SELECTORS = (
    '[offer-display-feature-name$="-fulfiller-info"], [offer-display-feature-name$="-merchant-info"], '
    '[offer-display-attribute-name$="-fulfiller-info"], [offer-display-attribute-name$="-merchant-info"]'
)
_VARIATION_SELECTORS = "#twister li, #variation_color_name li, #variation_size_name li, #inline-twister li"
# The product gallery, in its order. The desktop page names it in its image block's script
# (``'colorImages': { 'initial': [{"hiRes": …, "large": …, "main": {url: [w, h]}, "variant": "MAIN"}, …] }``, as
# JSON or as a JSON string to ``parseJSON``); the mobile page puts each photo's large copy on its carousel slide
# (``data-a-hires``); the desktop thumbnails (``#altImages``) are the last resort, made large. Only Amazon's product
# images (``/images/I/``): no banners, sprites or pixels (``/images/G/``), no video thumbnails or badges.
GALLERY_MAX = 12
_GALLERY_INITIAL_RE = re.compile(
    r"""['"]colorImages['"]\s*:\s*\{\s*['"]initial['"]\s*:\s*(?:[\w$.]*parseJSON\(\s*')?"""
)
_GALLERY_MOBILE_SELECTORS = "#image-block .media-block-image-tag[data-a-hires]"
_GALLERY_THUMB_SELECTORS = "#altImages li.imageThumbnail img"
_PRODUCT_IMAGE_RE = re.compile(
    r"^https://[A-Za-z0-9.-]*(?:media-amazon\.com|images-amazon\.com)/images/I/([A-Za-z0-9_%+-]+)\.(?:[^/?#]*\.)?(?:jpe?g|png|webp)$",
    re.IGNORECASE,
)
_SIZE_MODIFIER_RE = re.compile(r"\._[^/]*_\.(jpe?g|png|webp)$", re.IGNORECASE)
# The top reviews: the desktop page's (``review``) and the mobile page's (``mobley-review-content``), with the
# title and text under their older hooks or the current ones (``reviewTitle``, ``reviewRichContentContainer``).
_REVIEW_SELECTORS = '[data-hook="review"], [data-hook="mobley-review-content"]'
_REVIEW_STAR_SELECTORS = '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"], .a-icon-alt'
_REVIEW_TITLE_SELECTORS = '[data-hook="review-title"], [data-hook="reviewTitle"]'
_REVIEW_BODY_SELECTORS = '[data-hook="review-body"], [data-hook="reviewRichContentContainer"]'


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
    # Structured data first (the product's JSON-LD, the overview table's and the detail table's brand row, the
    # byline link's brand filter), then the byline's text without the phrase around it; a manufacturer row is
    # the last resort (in _details).
    rows = _detail_rows(soup)
    byline = soup.find(id="bylineInfo")
    candidates = (
        facts.brand,
        *(value for key, value in _overview_rows(soup).items() if _BRAND_ONLY_RE.match(key)),
        *(value for key, value in rows.items() if _BRAND_ONLY_RE.match(key)),
        _byline_param(byline),
        _FORMAT_SUFFIX_RE.sub("", _text(byline)),
    )
    facts.brand = next((brand for c in candidates if (brand := clean_brand(c))), "")


def _overview_rows(soup: BeautifulSoup) -> dict[str, str]:
    rows: dict[str, str] = {}
    for tr in soup.select("#productOverview_feature_div tr"):
        cells = tr.find_all("td")
        if len(cells) >= 2:
            rows.setdefault(_text(cells[0]).strip(_MARKS), _text(cells[1]).strip(_MARKS))
    return rows


def _byline_param(byline: object) -> str:
    if not isinstance(byline, Tag):
        return ""
    link = byline if byline.name == "a" else byline.find("a")
    href = link.get("href") if isinstance(link, Tag) else None
    if not isinstance(href, str):
        return ""
    query = parse_qs(urlsplit(href).query)
    return next((query[p][0] for p in _BYLINE_BRAND_PARAMS if query.get(p)), "")


def _prices(soup: BeautifulSoup, facts: PageFacts) -> None:
    price, currency = parse_price(_text(soup.select_one(_PRICE_TO_PAY_LABEL)))
    if price is None:
        price, currency = _first_price(soup.select(_PRICE_TO_PAY_OFFSCREEN))
    if price is None:
        price, currency = next(
            (found for tag in soup.select(_PRICE_TO_PAY) if (found := _price_parts(tag))[0] is not None), (None, "")
        )
    if price is None:
        price, currency = _first_price(soup.select(_PRICE_SELECTORS))
    facts.price = price if price is not None else facts.price
    facts.currency = currency or facts.currency
    old_layout = soup.select_one(", ".join(_CORE_PRICE)) is None
    for selectors in (*_LIST_PRICE_CANDIDATES, _OLD_LIST_PRICE) if old_layout else _LIST_PRICE_CANDIDATES:
        amounts = [amount for tag in soup.select(selectors) if (amount := parse_price(_text(tag))[0]) is not None]
        if above := [amount for amount in amounts if facts.price is None or amount > facts.price]:
            facts.list_price = above[0]
            return


def _first_price(tags: list[Tag]) -> tuple[float | None, str]:
    return next(
        (found for tag in tags if (found := parse_price(_text(tag)))[0] is not None),
        (None, ""),
    )


def _price_parts(tag: object) -> tuple[float | None, str]:
    # The visible price: "€", "20,", "99" (the whole part may group thousands: "1.299,").
    if not isinstance(tag, Tag) or (whole := parse_count(_text(tag.select_one(".a-price-whole")))) is None:
        return None, ""
    fraction = re.sub(r"\D", "", _text(tag.select_one(".a-price-fraction")))[:2]
    _, currency = parse_price(_text(tag.select_one(".a-price-symbol")))
    return float(f"{whole}.{fraction or 0}"), currency


def _ratings(soup: BeautifulSoup, facts: PageFacts) -> None:
    if (rating := parse_rating(_text(soup.select_one(_RATING_SELECTORS)))) is not None:
        facts.rating = rating
    if (count := parse_count(_text(soup.select_one(_RATING_COUNT_SELECTORS)))) is not None:
        facts.rating_count = count
    facts.rating_histogram = _histogram(soup) or facts.rating_histogram


def _details(soup: BeautifulSoup, facts: PageFacts) -> None:
    for key, value in _detail_rows(soup).items():
        if _FIRST_AVAILABLE_RE.search(key):
            facts.date_first_available = parse_date(value)
        elif not facts.brand and _BRAND_KEY_RE.match(key):
            facts.brand = clean_brand(value)


def _seller(soup: BeautifulSoup, facts: PageFacts) -> None:
    for key, value in _buybox_rows(soup).items():
        if _SHIPS_FROM_RE.search(key):
            facts.ships_from = value[:300]
        if _SOLD_BY_RE.search(key):
            facts.seller = value[:300]
    if not facts.seller:
        facts.seller = _text(soup.find(id="sellerProfileTriggerId") or soup.select_one("#merchant-info a"))[:300]


def _reviews(soup: BeautifulSoup, facts: PageFacts) -> None:
    seen: set[str] = set()
    reviews: list[Review] = []
    for node in soup.select(_REVIEW_SELECTORS):
        review_id = str(node.get("id") or "")
        if review_id and review_id in seen:
            continue  # the same review twice (a page with both layouts' widgets)
        seen.add(review_id)
        reviews.append(
            Review(
                rating=parse_rating(_text(node.select_one(_REVIEW_STAR_SELECTORS))),
                posted=parse_date(_text(node.select_one('[data-hook="review-date"]'))),
                title=_text(node.select_one(_REVIEW_TITLE_SELECTORS))[:300],
                body=_text(node.select_one(_REVIEW_BODY_SELECTORS))[:2000],
                verified=node.select_one('[data-hook^="avp-badge"]') is not None,
            )
        )
    facts.reviews = reviews[:100] or facts.reviews


def _variations_and_images(soup: BeautifulSoup, facts: PageFacts) -> None:
    variations = [
        str(li.get("title") or _text(li)).removeprefix("Click to select ") for li in soup.select(_VARIATION_SELECTORS)
    ]
    facts.variations = [v[:200] for v in variations if v][:100] or facts.variations
    if gallery := _gallery(soup):
        facts.image_urls = gallery  # the product's own photos, not the page's banners and pixels
        return
    image = soup.find(id="landingImage")
    if isinstance(image, Tag):
        src = image.get("data-old-hires") or image.get("src")
        if isinstance(src, str) and src:
            facts.image_urls = [src, *[u for u in facts.image_urls if u != src]][:50]


def _gallery(soup: BeautifulSoup) -> list[str]:
    """
    The product gallery's photos, large, in order: the first of the page's three ways of naming them that does.

    :param soup: The parsed page.
    :return: Up to :data:`GALLERY_MAX` photo URLs, one per photo (empty when the page names none).
    """
    for found in (_gallery_script(soup), _gallery_mobile(soup), _gallery_thumbnails(soup)):
        photos: dict[str, str] = {}
        for url in found:
            if (m := _PRODUCT_IMAGE_RE.match(url)) and m.group(1) not in photos:
                photos[m.group(1)] = url
        if photos:
            return list(photos.values())[:GALLERY_MAX]
    return []


def _gallery_script(soup: BeautifulSoup) -> list[str]:
    for script in soup.find_all("script"):
        text = script.string or ""
        if "colorImages" not in text or (m := _GALLERY_INITIAL_RE.search(text)) is None:
            continue
        try:
            items, _ = json.JSONDecoder().raw_decode(text, m.end())
        except ValueError:
            return []
        return [_gallery_url(i) for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    return []


def _gallery_url(item: dict[str, object]) -> str:
    # The high-resolution copy, else the large one, else the largest the page shows.
    for key in ("hiRes", "large"):
        if isinstance(value := item.get(key), str) and value:
            return value
    main = item.get("main")
    sizes = {
        url: int(size[0])
        for url, size in (main.items() if isinstance(main, dict) else [])
        if isinstance(size, list) and size and str(size[0]).isdigit()
    }
    return max(sizes, key=lambda url: sizes[url]) if sizes else ""


def _gallery_mobile(soup: BeautifulSoup) -> list[str]:
    return [str(tag.get("data-a-hires")) for tag in soup.select(_GALLERY_MOBILE_SELECTORS)]


def _gallery_thumbnails(soup: BeautifulSoup) -> list[str]:
    urls = (img.get("src") for img in soup.select(_GALLERY_THUMB_SELECTORS))
    return [_SIZE_MODIFIER_RE.sub(r"._AC_SL1500_.\1", url) for url in urls if isinstance(url, str)]


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
        feature = str(block.get("offer-display-feature-name") or block.get("offer-display-attribute-name") or "")
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
