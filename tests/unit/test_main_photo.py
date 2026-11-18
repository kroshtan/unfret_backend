"""
A product page's main photo is its product photo, not the first picture in its markup.

The photo checks (the deep lookup and the quick check's record) take ``facts.image_urls[0]``. A Shopify product page
whose JSON-LD is a ``ProductGroup`` without photos starts its markup with the header (a logo, a flag, a menu's
picture of another product), so the quick check fetched the logo, found it too small and recorded no photo. The Open
Graph image (the product's featured photo) and the structured data's photos (also as ``contentUrl`` and on a group's
variants) now come first. The shops and pages here are made up.
"""

import json
from typing import Any

import pytest

from tests.unit.test_image_index import StubEmbedder, encode, product_photo
from unfret import image_index, observations, recording, scoring
from unfret.config import settings
from unfret.facts import parse_page
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import reverse_image
from unfret.image_store import MemoryImageStore
from unfret.observations import FETCHED, QUICK, Kept
from unfret.x402.verdict import skills_for

_LOAD_MODEL = scoring.load_model  # the real one, captured before the suite's autouse fixture hides it
URL = "https://www.pinewalk.example/products/trail-runner"
PHOTO = "https://www.pinewalk.example/cdn/shop/files/trail-runner-moss.png?v=17"
LOGO = "https://www.pinewalk.example/cdn/shop/files/pinewalk-logo_160x.png?v=3"
FLAG = "https://www.pinewalk.example/cdn/shop/files/flag-us_48x.png?v=1"
MENU = "https://www.pinewalk.example/cdn/shop/files/menu-wool-lounger.jpg?v=9&width=640"
GROUP: dict[str, Any] = {
    "@context": "https://schema.org/",
    "@type": "ProductGroup",
    "name": "Trail Runner",
    "brand": {"@type": "Brand", "name": "Pinewalk"},
    "hasVariant": [
        {
            "@type": "Product",
            "sku": "TR-MOSS-9",
            "offers": {"@type": "Offer", "price": "95.00", "priceCurrency": "USD"},
        }
    ],
}


def shopify_page(group: dict[str, Any] = GROUP, og_type: str = "product") -> str:
    """A Shopify product page as its server sends it: the header's pictures come before the product's."""
    return (
        "<!doctype html><html><head><title>Trail Runner – Pinewalk</title>"
        f"<meta property='og:type' content='{og_type}'>"
        f"<meta property='og:image' content='{PHOTO.replace('https:', 'http:')}'>"
        f"<meta property='og:image:secure_url' content='{PHOTO}'>"
        "<meta property='og:price:amount' content='95.00'><meta property='og:price:currency' content='USD'>"
        "</head><body>"
        f'<header><a href="/"><img src="{LOGO.removeprefix("https:")}" alt="Pinewalk" width="160" height="40"></a>'
        f'<button class="locale"><img src="{FLAG.removeprefix("https:")}" alt="" width="48" height="48"> USD</button>'
        f'<nav><a href="/collections/men">Men<img src="{MENU.removeprefix("https:")}" alt="Wool Lounger"></a></nav>'
        "</header><main><section class='product-media'>"
        f'<img src="{PHOTO.removeprefix("https:")}&width=1280" alt="Trail Runner, moss, side view">'
        "</section><h1>Trail Runner</h1><p class='price'>$95</p><button>Add to cart</button>"
        f"<p>{'A light trail shoe with a grippy sole. ' * 30}</p></main>"
        f"<script type='application/ld+json'>{json.dumps(group)}</script></body></html>"
    )


def test_a_product_pages_open_graph_photo_comes_before_its_header() -> None:
    facts, _ = parse_page(shopify_page(), URL)
    assert facts.image_urls[0] == PHOTO
    assert reverse_image.main_photo(facts, "storefront") == PHOTO
    assert {LOGO, FLAG, MENU} <= set(facts.image_urls)  # the page's pictures are all still there, after it


def test_structured_data_photos_come_first_as_url_content_url_or_on_a_variant() -> None:
    variant = {**GROUP, "hasVariant": [{**GROUP["hasVariant"][0], "image": "/cdn/shop/files/variant-moss.jpg"}]}
    facts, _ = parse_page(shopify_page(variant), URL)
    assert facts.image_urls[0] == "https://www.pinewalk.example/cdn/shop/files/variant-moss.jpg"

    objects = {**GROUP, "image": [{"@type": "ImageObject", "contentUrl": "https://img.pinewalk.example/a_s5.jpg"}]}
    facts, _ = parse_page(shopify_page(objects), URL)
    assert facts.image_urls[0] == "https://img.pinewalk.example/a_s5.jpg"


@pytest.mark.parametrize("og_type", ["website", "article"])
def test_a_page_that_isnt_about_a_product_keeps_its_order(og_type: str) -> None:
    html = shopify_page(og_type=og_type).replace("ProductGroup", "Organization")
    facts, _ = parse_page(html, URL)
    assert facts.image_urls[0] == LOGO  # a service company's Open Graph image is often its logo: not promoted


async def test_a_quick_check_of_such_a_page_records_the_product_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    quick = _LOAD_MODEL.__wrapped__(scoring.MODEL_QUICK_PATH)
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: quick)
    monkeypatch.setattr(image_index, "_embedder", StubEmbedder())
    monkeypatch.setattr(image_index, "embedder", lambda: image_index._embedder)
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(settings, "price_observations", False)
    monkeypatch.setattr(observations, "witnesses", observations.Witnesses())
    monkeypatch.setattr(observations, "writes", observations.WriteLimit(observations.witnesses))
    store = MemoryImageStore()
    reverse_image.use_store(store)
    photos = {PHOTO: encode(product_photo(41), "JPEG", quality=70), LOGO: encode(product_photo(42).resize((160, 40)))}
    fetched: list[str] = []

    async def fetch(url: str, **_: Any) -> bytes:
        fetched.append(url)
        return photos[url]

    monkeypatch.setattr(reverse_image, "fetch_public", fetch)
    graph = build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False, record_page=True)
    prov = observations.for_request("198.51.100.7", None, fetched=True, tier="quick")
    with observations.observed_as(prov):
        result = await graph.ainvoke({"page_content": shopify_page(), "url": URL, "flags": [], "explain": False})
    await recording.drain()
    await reverse_image.drain()

    assert result["page_type"] == "storefront"
    assert fetched == [PHOTO]  # the product photo, not the header's logo
    assert [domain for domain, _ in store.rows] == ["pinewalk.example"]
    assert list(store.kept.values()) == [Kept(FETCHED | QUICK, 1)]
