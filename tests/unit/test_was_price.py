"""The "was" price against the page's own price history: invented reference prices and discounts that never end."""

from datetime import date, timedelta
from typing import Any

import pytest

from unfret import levels, observations, price_store, shopper, was_price
from unfret.facts import PageFacts
from unfret.graph.skills.pricing import PricingSkill
from unfret.price_store import MemoryPriceStore, Observation

TODAY = date(2026, 11, 26)
SHOP = "https://glow-lamps.example/products/desk-lamp"


@pytest.fixture(autouse=True)
def store() -> MemoryPriceStore:
    memory = MemoryPriceStore()
    price_store.use_store(memory)
    return memory


def days(prices: list[tuple[int, float, float | None]]) -> list[Observation]:
    """Observations ``days_ago`` before TODAY, oldest first."""
    return sorted(
        (Observation(TODAY - timedelta(days=ago), price, "USD", was) for ago, price, was in prices),
        key=lambda o: o.day,
    )


def state(facts: PageFacts, url: str = SHOP, page_type: str = "storefront") -> Any:
    return {"facts": facts, "url": url, "cleaned_content": "", "page_type": page_type, "flags": []}


# --- the page key --------------------------------------------------------------------------------------------------


def test_a_product_page_without_a_product_id_is_keyed_by_its_page() -> None:
    facts = PageFacts(price=39.99)
    key = price_store.listing_key(SHOP, facts, "storefront")
    assert key is not None and len(key) == 32
    assert b"glow" not in key and b"lamp" not in key  # a hash, never the address
    # Tracking parameters, the fragment, the subdomain, case and a trailing slash don't make another page.
    same = "https://www.glow-lamps.example/Products/desk-lamp/?utm_source=x&gclid=abc&ref=y#reviews"
    assert price_store.listing_key(same, facts, "storefront") == key
    # A variant has its own price, so its own key.
    variant = price_store.listing_key(SHOP + "?variant=4242", facts, "storefront")
    assert variant not in {None, key}
    assert price_store.listing_key(SHOP + "?utm_source=x&variant=4242", facts, "storefront") == variant


def test_a_page_key_only_for_product_pages() -> None:
    facts = PageFacts(price=39.99)
    for page_type in (None, "article", "non_product", "service_site", "p2p_listing"):
        assert price_store.listing_key(SHOP, facts, page_type) is None
    assert price_store.listing_key("https://glow-lamps.example/", facts, "storefront") is None  # no path: no page


def test_a_public_product_id_keeps_its_product_key() -> None:
    facts = PageFacts(json_ld=[{"@type": "Product", "offers": {"gtin13": "8712345678906"}}], price=39.99)
    assert price_store.listing_key(SHOP, facts, "storefront") == price_store.product_key(SHOP, facts)


# --- the rules -----------------------------------------------------------------------------------------------------


def test_a_was_price_never_charged_over_weeks_is_a_finding() -> None:
    seen = days([(ago, 39.99 if ago % 2 else 44.99, 79.99) for ago in range(40, 0, -5)])
    [found] = [f for f in was_price.assess(seen, TODAY, 39.99, 79.99) if f.type == "was_price_unseen"]
    assert found.evidence.startswith("Seen on 8 days over 40 days: price 39.99 to 44.99; the 'was' price 79.99")
    assert "on 0 of them" in found.evidence and "50% off" in found.evidence


def test_a_was_price_charged_on_barely_a_day_still_counts() -> None:
    # One day at the "was" price, to make it look real, among twelve.
    seen = days([(ago, 79.99 if ago == 36 else 39.99, 79.99) for ago in range(36, 0, -3)])
    [found] = [f for f in was_price.assess(seen, TODAY, 39.99, 79.99) if f.type == "was_price_unseen"]
    assert "on 1 of them" in found.evidence


def test_an_honest_sale_is_no_finding() -> None:
    # Sold at 79.99 for weeks, then reduced: the "was" price was the price.
    seen = days([(ago, 79.99, None) for ago in range(60, 10, -5)] + [(ago, 59.99, 79.99) for ago in (8, 4, 1)])
    assert was_price.assess(seen, TODAY, 59.99, 79.99) == []


@pytest.mark.parametrize(
    "prices",
    [
        [(30, 39.99, 79.99), (20, 39.99, 79.99), (10, 39.99, 79.99)],  # three days: too few to say
        [(ago, 39.99, 79.99) for ago in range(12, 0, -2)],  # six days, but only over 12 days: one sale, maybe
        [],
    ],
)
def test_a_short_history_says_nothing(prices: list[tuple[int, float, float | None]]) -> None:
    seen = days(prices)
    assert was_price.assess(seen, TODAY, 39.99, 79.99) == []
    summary = was_price.summarise(seen, TODAY, 39.99, 79.99)
    assert summary.charged_share is None


def test_no_discount_today_says_nothing() -> None:
    seen = days([(ago, 39.99, 79.99) for ago in range(60, 0, -5)])
    assert was_price.assess(seen, TODAY, 39.99, None) == []
    assert was_price.assess(seen, TODAY, 39.99, 40.50) == []  # a rounding difference is no discount


def test_a_discount_shown_on_every_day_for_weeks_never_ends() -> None:
    seen = days([(ago, 49.99, 99.99) for ago in range(60, 0, -4)])
    found = {f.type: f.evidence for f in was_price.assess(seen, TODAY, 49.99, 99.99)}
    assert found["discount_always_on"] == (
        "A 'was' price above the price on 15 of 15 days seen over 60 days, and today: 50% off."
    )


def test_a_discount_on_part_of_the_days_is_not_endless() -> None:
    seen = days([(ago, 79.99, None) for ago in range(60, 20, -4)] + [(ago, 49.99, 79.99) for ago in (16, 8, 1)])
    assert "discount_always_on" not in {f.type for f in was_price.assess(seen, TODAY, 49.99, 79.99)}


def test_a_month_of_discounts_is_not_yet_endless() -> None:
    seen = days([(ago, 49.99, 99.99) for ago in range(32, 0, -4)])
    assert "discount_always_on" not in {f.type for f in was_price.assess(seen, TODAY, 49.99, 99.99)}


# --- the skill, the card, the levels -------------------------------------------------------------------------------


async def test_the_skill_finds_it_on_a_web_shops_own_page(store: MemoryPriceStore) -> None:
    facts = PageFacts(price=39.99, list_price=79.99, currency="USD")
    key = price_store.listing_key(SHOP, facts, "storefront")
    assert key is not None
    today = price_store.today()
    for ago in range(50, 0, -5):
        await store.record(key, Observation(today - timedelta(days=ago), 39.99, "USD", 79.99))
    await store.record(key, Observation(today - timedelta(days=20), 35.0, "EUR", None))  # another currency: ignored

    out = await PricingSkill().run(state(facts))

    f = out["features"]
    assert f["pricing.was_price_unseen"] == 1.0 and f["pricing.discount_always_on"] == 1.0
    assert f["pricing.was_charged_share"] == 0.0 and f["pricing.discount_share"] == 1.0
    types = [o.type for o in out["findings"]]
    assert "was_price_unseen" in types and "discount_always_on" in types

    verdict = shopper.build(70, features=f, findings=out["findings"], facts=facts, page_type="storefront")
    reasons = {r.type: r for r in verdict.reasons}
    assert "was_price_unseen" in reasons
    detail = reasons["was_price_unseen"].detail
    assert "10 days" in detail and "$79.99" in detail and "on none of them" in detail
    assert "scam" not in detail.lower()


async def test_the_skill_is_silent_on_a_new_page(store: MemoryPriceStore) -> None:
    facts = PageFacts(price=39.99, list_price=79.99, currency="USD")
    out = await PricingSkill().run(state(facts))
    assert not {"was_price_unseen", "discount_always_on"} & {o.type for o in out["findings"]}
    assert "pricing.was_charged_share" not in out["features"]
    assert len(store.rows) == 1  # today is recorded for next time


def test_both_are_manipulation_findings_a_shopper_can_mark() -> None:
    for t in ("was_price_unseen", "discount_always_on"):
        assert levels.level_of(t) == 1 and t in levels.MARKABLE
        assert levels.title_of(t) and "scam" not in levels.title_of(t).lower()


# --- privacy -------------------------------------------------------------------------------------------------------


async def test_a_page_row_keeps_nothing_about_the_page_address_or_who_checked(store: MemoryPriceStore) -> None:
    facts = PageFacts(price=39.99, list_price=79.99, currency="USD")
    with observations.observed_as(observations.for_request("203.0.113.7", "pass-123", fetched=False, tier="deep")):
        await price_store.observe(SHOP + "?utm_source=newsletter&email=someone@example.com", facts, "storefront")
    with observations.observed_as(observations.for_request("198.51.100.9", "pass-456", fetched=False, tier="deep")):
        await price_store.observe(SHOP, facts, "storefront")

    [(key, day)] = store.rows  # one row per page per day, whoever checked it
    assert set(vars(store.rows[(key, day)])) == {"day", "price", "currency", "was_price"}
    assert set(vars(store.kept[(key, day)])) == {"bits", "sources"}
    stored = repr(store.rows) + repr(store.kept)
    for private in ("203.0.113", "198.51.100", "pass-", "someone", "utm", "glow-lamps", "desk-lamp"):
        assert private not in stored
