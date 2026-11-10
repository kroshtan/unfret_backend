"""The anonymous price store and the fake-discount skill."""

from datetime import date, timedelta
from typing import Any

import pytest

from unfret import price_store
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.facts.schema import Platform
from unfret.graph.skills.pricing import PricingSkill
from unfret.price_store import MemoryPriceStore, Observation


@pytest.fixture(autouse=True)
def store() -> MemoryPriceStore:
    memory = MemoryPriceStore()
    price_store.use_store(memory)
    return memory


def state(facts: PageFacts, url: str = "", text: str = "", page_type: str = "storefront") -> Any:
    return {"facts": facts, "url": url, "cleaned_content": text, "page_type": page_type, "flags": []}


def test_product_ids_are_public_identifiers_only() -> None:
    amazon = PageFacts(platform="amazon")
    assert price_store.product_id("https://www.amazon.nl/Some-Lamp/dp/B0ABC12345?th=1", amazon) == "asin:B0ABC12345"
    bol = PageFacts(platform="bol.com")
    assert (
        price_store.product_id("https://www.bol.com/nl/nl/p/glow-lamp/9300000123456789/", bol)
        == "bol:9300000123456789"
    )
    shop = PageFacts(json_ld=[{"@type": "Product", "offers": {"gtin13": "8712345678906"}}])
    assert price_store.product_id("https://glow.example/products/lamp", shop) == "gtin:08712345678906"
    assert price_store.product_id("https://glow.example/products/lamp", PageFacts()) is None
    bad_check_digit = PageFacts(json_ld=[{"@type": "Product", "gtin13": "8712345678907"}])
    assert price_store.product_id("https://glow.example/p", bad_check_digit) is None


def test_the_key_hides_the_url_and_ignores_subdomains() -> None:
    facts = PageFacts(platform="amazon")
    a = price_store.product_key("https://www.amazon.co.uk/x/dp/B0ABC12345", facts)
    b = price_store.product_key("https://smile.amazon.co.uk/dp/B0ABC12345?ref=x", facts)
    assert a == b and a is not None and len(a) == 32
    assert b"amazon" not in a
    assert price_store.registrable_domain("shop.example.co.uk") == "example.co.uk"


async def test_one_row_per_product_per_day_without_url_or_caller(store: MemoryPriceStore) -> None:
    facts = PageFacts(platform="amazon", price=19.95, list_price=79.95, currency="eur")
    url = "https://www.amazon.nl/dp/B0ABC12345"
    await price_store.observe(url, facts)
    await price_store.observe(url, facts.model_copy(update={"price": 9.0}))

    [(key, day)] = store.rows
    assert store.rows[(key, day)] == Observation(day, 19.95, "EUR", 79.95)  # the day's first observation
    assert set(vars(store.rows[(key, day)])) == {"day", "price", "currency", "was_price"}


async def test_old_observations_are_purged(store: MemoryPriceStore) -> None:
    key = b"k" * 32
    today = price_store.today()
    await store.record(key, Observation(today - timedelta(days=91), 10.0, "EUR", None))
    await store.record(key, Observation(today - timedelta(days=89), 10.0, "EUR", None))
    await store.purge(today - timedelta(days=price_store.RETENTION_DAYS))
    assert [d for _, d in store.rows] == [today - timedelta(days=89)]


async def test_fake_discount_wording() -> None:
    facts = PageFacts(price=19.95, list_price=99.95)
    text = "Adviesprijs: €99,95 Nu €19,95. Opheffingsuitverkoop, alles moet weg!"

    out = await PricingSkill().run(state(facts, text=text))

    f = out["features"]
    assert (f["pricing.implausible_discount"], f["pricing.reference_anchor"], f["pricing.permanent_sale"]) == (1, 1, 1)
    assert {o.type for o in out["findings"]} == {"implausible_discount", "reference_anchor", "permanent_sale"}


@pytest.mark.parametrize(
    ("platform", "text"),
    [
        # Wording from archived amazon.com and Walmart listings (US-market fixtures), and bol.com's own label.
        ("amazon", "Paperback $10.90 Print List Price: $14.39 $14.39"),
        ("amazon", "-25% $10.90 Typical price: $14.39"),
        ("walmart", "Current price is USD Now $89.00 You save $40.99 was $129.99 $129.99"),
        ("bol.com", "Adviesprijs € 14,39 € 10,90"),
    ],
)
async def test_a_marketplaces_own_reference_price_is_not_an_anchor(platform: Platform, text: str) -> None:
    facts = PageFacts(platform=platform, price=10.9, list_price=14.39)
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert out["features"] == {}


async def test_a_sellers_own_anchor_still_counts_on_a_marketplace() -> None:
    facts = PageFacts(platform="amazon", price=10.9, list_price=14.39)
    text = "List Price: $14.39. Retail value: $49.99, yours for $10.90!"
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}
    assert [o.evidence for o in out["findings"]] == ['"Retail value"']


async def test_a_list_price_label_still_counts_on_a_shop() -> None:
    out = await PricingSkill().run(state(PageFacts(price=10.9, list_price=14.39), text="List price: $14.39"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}


async def test_an_honest_was_price_is_not_an_anchor() -> None:
    out = await PricingSkill().run(state(PageFacts(price=40.0, list_price=50.0), text="Was €50, now €40."))
    assert out["features"] == {}


async def test_was_never_charged_needs_a_month_of_observations(store: MemoryPriceStore) -> None:
    url = "https://www.amazon.nl/dp/B0ABC12345"
    facts = PageFacts(platform="amazon", price=39.95, list_price=79.95, currency="EUR")
    key = price_store.product_key(url, facts)
    assert key is not None
    today = price_store.today()
    await store.record(key, Observation(today - timedelta(days=10), 34.95, "EUR", None))

    young = await PricingSkill().run(state(facts, url=url, page_type="aggregate_listing"))
    assert "pricing.was_above_30d_low" not in young["features"]  # gated: only 10 days of data

    await store.record(
        key, Observation(today - timedelta(days=settings.price_history_min_days + 5), 39.0, "EUR", None)
    )
    store.rows.pop((key, today))  # observed again "tomorrow"
    old = await PricingSkill().run(state(facts, url=url, page_type="aggregate_listing"))
    assert old["features"]["pricing.was_above_30d_low"] == 1.0
    assert "34.95" in old["findings"][0].evidence


async def test_the_store_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, store: MemoryPriceStore) -> None:
    monkeypatch.setattr(settings, "price_observations", False)
    facts = PageFacts(platform="amazon", price=19.95, currency="EUR")
    await PricingSkill().run(state(facts, url="https://www.amazon.nl/dp/B0ABC12345"))
    assert store.rows == {}


def test_gtin_check_digit() -> None:
    assert price_store.valid_gtin("4006381333931")
    assert not price_store.valid_gtin("4006381333932")
    assert not price_store.valid_gtin(str(date.today().year))
