"""
Reviews and reference prices read from the page itself, on every layout the server or a client sees.

Each fixture in ``tests/fixtures/pages`` reproduces a real page's structure (README there); the browser extension
holds the same pages with these facts as its parity expectations, so the app, the extension and the server read
them alike.
"""

from datetime import date
from pathlib import Path

import pytest

from unfret.facts import parse_page
from unfret.graph.skills.pricing import PricingSkill

PAGES = Path(__file__).resolve().parent.parent / "fixtures" / "pages"


def _facts(page: str, url: str):  # type: ignore[no-untyped-def]
    return parse_page((PAGES / page).read_text(), url)


@pytest.mark.parametrize(
    ("page", "url", "price", "was", "currency"),
    [
        # The mobile page (a phone's, the Android app's WebView): an empty offscreen price, a sponsored price first,
        # a price per unit in the price box.
        ("amazon-nl-mobile-reference.html", "https://www.amazon.nl/dp/B0MOBREF01", 20.99, 29.99, "EUR"),
        ("amazon-de-mobile-uvp.html", "https://www.amazon.de/dp/B0MOBUVP01", 26.0, 39.99, "EUR"),
        ("amazon-com-typical-price-reviews.html", "https://www.amazon.com/dp/B0TYPICAL1", 14.99, 19.99, "USD"),
        ("bolcom-reviews-partner.html", "https://www.bol.com/nl/nl/p/bouwset/9300000000001/", 21.99, 29.99, "EUR"),
        ("woocommerce-sale-reviews.html", "https://gadgetwinkel.example/product/pistool/", 49.95, 129.95, "EUR"),
        ("generic-jsonld-strikethrough.html", "https://bergzeit-outdoor.example/rucksack-30", 59.9, 89.9, "EUR"),
        ("ebay-was-price.html", "https://www.ebay.com/itm/100000000001", 24.99, 129.99, "USD"),
    ],
)
def test_price_and_reference_price(page: str, url: str, price: float, was: float, currency: str) -> None:
    facts, _ = _facts(page, url)

    assert (facts.price, facts.list_price, facts.currency) == (price, was, currency)


def test_amazon_mobile_page_reads_rating_seller_and_reviews() -> None:
    facts, _ = _facts("amazon-nl-mobile-reference.html", "https://www.amazon.nl/dp/B0MOBREF01")

    assert (facts.rating, facts.rating_count, facts.seller, facts.ships_from) == (4.8, 1204, "Amazon", "Amazon")
    assert [(r.rating, r.posted, r.title, r.verified) for r in facts.reviews] == [
        (4.0, date(2026, 8, 30), "Precies wat we zochten", True),
        (5.0, date(2026, 11, 6), "Mooie kleuren", False),
    ]
    assert facts.reviews[0].body == "Onze dochter bouwt er elke dag mee."
    assert facts.rating_histogram == {}  # the mobile page has no star histogram


def test_amazon_current_review_hooks_give_titles_and_texts() -> None:
    facts, _ = _facts("amazon-com-typical-price-reviews.html", "https://www.amazon.com/dp/B0TYPICAL1")

    assert [(r.title, r.body) for r in facts.reviews] == [
        ("Keeps the kids busy", "Great quality, the case closes well."),
        ("Fewer pieces than shown", "The photo shows more colours than came in the box."),
    ]
    assert facts.rating_histogram == {5: 0.81, 4: 0.11, 3: 0.04, 2: 0.01, 1: 0.03}


def test_bolcom_reviews_histogram_and_partner() -> None:
    facts, _ = _facts("bolcom-reviews-partner.html", "https://www.bol.com/nl/nl/p/bouwset/9300000000001/")

    assert (facts.seller, facts.seller_rating, facts.rating, facts.rating_count) == ("Speelgoedpartner", 87.0, 4.7, 20)
    assert facts.rating_histogram == {1: 0.05, 2: 0.0, 3: 0.0, 4: 0.15, 5: 0.8}
    assert [(r.rating, r.posted, r.title, r.verified) for r in facts.reviews] == [
        (5.0, date(2026, 11, 19), "Drijft echt", True),
        (1.0, date(2026, 2, 14), "", False),
    ]


def test_woocommerce_rating_and_reviews_without_structured_data() -> None:
    facts, _ = _facts("woocommerce-sale-reviews.html", "https://gadgetwinkel.example/product/pistool/")

    assert (facts.rating, facts.rating_count) == (4.5, 12)
    assert [(r.rating, r.posted, r.verified) for r in facts.reviews] == [
        (5.0, date(2026, 10, 2), True),
        (2.0, date(2026, 9, 20), False),
    ]


def test_review_apps_and_microdata() -> None:
    judgeme, _ = _facts("shopify-judgeme-reviews.html", "https://lumora.example/products/glow-desk-lamp")
    microdata, _ = _facts("generic-microdata-rating.html", "https://maison-the.example/theiere-fonte")
    jsonld, _ = _facts("generic-jsonld-strikethrough.html", "https://bergzeit-outdoor.example/rucksack-30")

    assert (judgeme.rating, judgeme.rating_count) == (4.62, 58)  # the review widget's, before the Loox badge's
    assert judgeme.rating_histogram[5] == pytest.approx(44 / 58)
    assert [(r.rating, r.title, r.verified) for r in judgeme.reviews] == [
        (5.0, "Lovely warm light", True),
        (3.0, "OK", False),
    ]
    assert (microdata.rating, microdata.rating_count) == (4.3, 126)  # the microdata, before the Stamped badge
    assert (jsonld.rating, jsonld.rating_count) == (4.4, 37)  # an AggregateRating block about the product
    assert jsonld.image_urls[0] == "https://bergzeit-outdoor.example/media/rucksack-30.jpg"


async def test_the_mobile_amazon_page_gives_the_reference_price_reason() -> None:
    # Floris's case: the app read amazon.nl's mobile page without a price, so the "Adviesprijs" discount went unseen.
    facts, text = _facts("amazon-nl-mobile-reference.html", "https://www.amazon.nl/dp/B0MOBREF01")

    out = await PricingSkill(history=False).run({"facts": facts, "cleaned_content": text, "url": ""})  # type: ignore[typeddict-item]

    assert out["features"].get("pricing.reference_anchor") == 1.0


async def test_bolcom_own_reference_labels_say_nothing_by_themselves() -> None:
    facts, text = _facts("bolcom-reviews-partner.html", "https://www.bol.com/nl/nl/p/bouwset/9300000000001/")

    out = await PricingSkill(history=False).run({"facts": facts, "cleaned_content": text, "url": ""})  # type: ignore[typeddict-item]

    assert "pricing.reference_anchor" not in out["features"]
