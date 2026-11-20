"""
A product page's photo gallery, on any shop system or marketplace: the product's own photos, large, in order.

The photo steps of a deep check (``reverse_image``, ``photo_claims``) read ``image_urls`` from the front, so the
product's photos go first, and the page's other images (logos, banners, icons, other products) after them. Where a
page names its photos, in that order:

1. **The main photo** the extraction already put first: the product's structured data (schema.org ``Product`` /
   ``ProductGroup`` ``image``, an array or one) or, without it, a product page's Open Graph image.
2. **A marketplace's own data**: AliExpress's ``imagePathList``, Temu's ``gallery`` (Amazon's, Walmart's and the
   peer-to-peer platforms' galleries are read by their own modules, after this one).
3. **A shop system's product JSON**: Shopify themes' ``data-product-json`` / ``ProductJson-…`` (``images``,
   ``media``).
4. **Gallery markup**: images with a zoom or full-size attribute (``data-zoom-image``, WooCommerce's
   ``data-large_image``, eBay's ``data-zoom-src``, …) and images inside a gallery, carousel or slider (by class or
   id), never inside a header, menu, footer or a block of related products; each image's largest copy (its zoom
   attribute, else the largest in its ``srcset``, else its lazy or plain source). A theme's URL template
   (``…_{width}x.jpg``) is no photo.

The same photo in several sizes (a Shopify ``_800x`` copy, a WordPress ``-300x300`` thumbnail, eBay's ``s-l500``,
AliExpress's ``_220x220``) counts once (:func:`photo_key`), first seen kept. At most :data:`GALLERY_MAX` photos; only
on a page about a product.
"""

import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from unfret.facts.schema import PageFacts

GALLERY_MAX = 12

# Attributes that name an image's large copy, in order of preference.
_ZOOM_ATTRS = (
    "data-zoom-image",
    "data-zoom-image-url",
    "data-zoom-src",
    "data-zoom",
    "data-large_image",
    "data-large-image",
    "data-full",
    "data-full-src",
    "data-full-size",
    "data-big",
    "data-a-hires",
    "data-old-hires",
)
_LAZY_ATTRS = ("data-src", "data-lazy-src", "data-original", "src")
_SRCSET_ATTRS = ("data-srcset", "srcset")
_CONTAINER_RE = re.compile(
    r"gallery|carousel|slider|slideshow|swiper|slick|splide|flickity|filmstrip|thumbnails?\b"
    r"|product[-_]?(?:media|images?|photos?|single__photo|slides?)",
    re.IGNORECASE,
)
_NOT_PRODUCT_RE = re.compile(
    r"related|recommend|upsell|up-sell|cross-?sell|similar|recently|also[-_]?(?:bought|viewed|like)|reviews?\b"
    r"|testimonial|logo|payment|banner|instagram|blog|video",
    re.IGNORECASE,
)
_OUTSIDE_TAGS = frozenset({"header", "nav", "footer", "aside"})
_ANCESTORS = 8
_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|avif)(?:$|[?#_])", re.IGNORECASE)
_PRODUCT_OG_TYPES = frozenset({"product", "product.item", "product.group", "og:product"})
_ALIEXPRESS_HOST_RE = re.compile(r"(?:^|\.)aliexpress\.[a-z.]+$")
_TEMU_HOST_RE = re.compile(r"(?:^|\.)temu\.com$")
_ALIEXPRESS_RE = re.compile(r'"imagePathList"\s*:\s*')
_TEMU_RE = re.compile(r'"gallery"\s*:\s*')
_SHOPIFY_JSON_ID_RE = re.compile(r"^ProductJson", re.IGNORECASE)

# The same photo's size variants, as photo_key strips them.
_KEY_PATTERNS = (
    (re.compile(r"\._[^/]*_(?=\.[a-z0-9]+$)"), ""),  # Amazon's ._AC_SL1500_
    (re.compile(r"(\.(?:jpe?g|png|webp))_[^/]*$"), r"\1"),  # AliExpress's .jpg_220x220q75.jpg_.webp
    (
        re.compile(
            r"_(?:\d+x\d*|\d*x\d+|pico|icon|thumb|small|compact|medium|large|grande|original|master)"
            r"(?:_crop_[a-z]+)?(?:@\dx)?(?=\.[a-z0-9]+$)"
        ),
        "",
    ),  # Shopify's _800x, _grande
    (re.compile(r"-(?:\d+x\d+|scaled|e\d{9,})(?=\.[a-z0-9]+$)"), ""),  # WordPress's -300x300, -scaled
    (re.compile(r"/s-l\d+(?=\.[a-z0-9]+$)"), "/s-l"),  # eBay's s-l1600
    (re.compile(r"/\d+x\d+(?=\.[a-z0-9]+$)"), ""),  # bol.com's /550x550.jpg
)


def photo_key(url: str) -> str:
    """
    One key for a photo's copies in every size.

    :param url: The image URL.
    :return: Its host and path, lower case, without the query and the size markers shop systems add.
    """
    key = re.sub(r"^https?:", "", url.split("#", maxsplit=1)[0].split("?", maxsplit=1)[0].lower())
    for pattern, replacement in _KEY_PATTERNS:
        key = pattern.sub(replacement, key)
    return key


def dedupe_photos(urls: Iterable[str]) -> list[str]:
    """
    The URLs with each photo once (:func:`photo_key`), the first seen kept.

    :param urls: Image URLs, in order.
    :return: The same order, without the other copies.
    """
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url and (key := photo_key(url)) not in seen:
            seen.add(key)
            out.append(url)
    return out


def extract(soup: BeautifulSoup, url: str, facts: PageFacts, *, main: list[str]) -> None:
    """
    Put the product's gallery first in ``facts.image_urls``.

    :param soup: The parsed page.
    :param url: The page's URL.
    :param facts: Updated in place.
    :param main: The photos the structured data (or the product's Open Graph image) named, already first.
    """
    host = (urlparse(url).hostname or "").lower()
    found = [*_marketplace_json(soup, host), *_shop_json(soup)]
    product_page = bool(main or found) or _meta(soup, "og:type").strip().lower() in _PRODUCT_OG_TYPES
    if not product_page and facts.platform not in {"bol.com", "ebay", "walmart"}:
        return
    photos = [
        u
        for u in (_absolute(url, p) for p in [*main, *found, *_markup(soup)])
        if u.startswith("https://") and "{" not in u and _IMAGE_EXT_RE.search(u)
    ]
    gallery = dedupe_photos(photos)[:GALLERY_MAX]
    if len(gallery) > len(dedupe_photos(main)):
        facts.image_urls = dedupe_photos([*gallery, *facts.image_urls])[:50]


def _absolute(base: str, url: str) -> str:
    url = url.strip()
    return "https:" + url if url.startswith("//") else urljoin(base, url)


def _meta(soup: BeautifulSoup, key: str) -> str:
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return str(tag.get("content", "")) if isinstance(tag, Tag) else ""


def _decode_after(text: str, pattern: re.Pattern[str]) -> object:
    if (m := pattern.search(text)) is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text, m.end())
    except ValueError:
        return None
    return value


def _urls(items: object, *keys: str) -> list[str]:
    """Strings of a list, or each dict's first string under ``keys``."""
    out: list[str] = []
    for item in items if isinstance(items, list) else []:
        value = next((item[k] for k in keys if isinstance(item.get(k), str)), "") if isinstance(item, dict) else item
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _marketplace_json(soup: BeautifulSoup, host: str) -> list[str]:
    keys: tuple[str, ...] = ()
    if _ALIEXPRESS_HOST_RE.search(host):
        pattern = _ALIEXPRESS_RE
    elif _TEMU_HOST_RE.search(host):
        pattern, keys = _TEMU_RE, ("url", "imgUrl", "src")
    else:
        return []
    for script in soup.find_all("script"):
        text = script.string or ""
        if pattern.search(text) and (urls := _urls(_decode_after(text, pattern), *keys)):
            return urls
    return []


def _shop_json(soup: BeautifulSoup) -> list[str]:
    for script in soup.find_all("script", type="application/json"):
        if not (script.has_attr("data-product-json") or _SHOPIFY_JSON_ID_RE.match(str(script.get("id") or ""))):
            continue
        try:
            data: Any = json.loads(script.string or "")
        except ValueError:
            continue
        product = data.get("product") if isinstance(data, dict) and isinstance(data.get("product"), dict) else data
        if not isinstance(product, dict):
            continue
        media = [m for m in product.get("media") or [] if isinstance(m, dict) and m.get("media_type") == "image"]
        if urls := _urls(product.get("images"), "src") or _urls(media, "src"):
            return urls
    return []


def _markup(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    for tag in soup.find_all(_candidate):
        zoom = next((str(tag[a]) for a in _ZOOM_ATTRS if tag.get(a)), "")
        if not _in_product_block(tag) or (not zoom and not _in_gallery(tag)):
            continue
        src = zoom or _largest(tag) or next((str(tag[a]) for a in _LAZY_ATTRS if tag.get(a)), "")
        if src and not src.startswith("data:"):
            found.append(src)
    return found


def _candidate(tag: Tag) -> bool:
    return tag.name == "img" or any(tag.get(a) for a in _ZOOM_ATTRS)


def _names(tag: Tag) -> str:
    classes = tag.get("class")
    return " ".join([*(classes if isinstance(classes, list) else [str(classes or "")]), str(tag.get("id") or "")])


def _in_product_block(tag: Tag) -> bool:
    node: Tag | None = tag
    while node is not None and node.name not in {"body", "[document]"}:
        if node.name in _OUTSIDE_TAGS or _NOT_PRODUCT_RE.search(_names(node)):
            return False
        node = node.parent
    return True


def _in_gallery(tag: Tag) -> bool:
    node: Tag | None = tag
    for _ in range(_ANCESTORS):
        if node is None or node.name in {"body", "[document]"}:
            return False
        if _CONTAINER_RE.search(_names(node)):
            return True
        node = node.parent
    return False


def _largest(tag: Tag) -> str:
    best, width = "", -1
    for attr in _SRCSET_ATTRS:
        for part in str(tag.get(attr) or "").split(","):
            bits = part.split()
            if len(bits) == 2 and re.fullmatch(r"\d+w", bits[1]) and int(bits[1][:-1]) > width:
                best, width = bits[0], int(bits[1][:-1])
        if best:
            return best
    return ""
