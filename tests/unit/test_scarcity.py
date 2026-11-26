"""Low-stock claims across visits: the claim, the store, the finding and what is (not) stored."""

from datetime import date, timedelta
from typing import Any

import pytest

from unfret import levels, observations, scarcity_store
from unfret.facts import PageFacts
from unfret.facts.extract import parse_page, stock_left
from unfret.graph.skills.scarcity import ScarcitySkill
from unfret.scarcity_store import Claim, MemoryScarcityStore, assess

URL = "https://lamps.example/products/glow-lamp?variant=123&utm_source=mail"
DAY = date(2026, 11, 26)


@pytest.fixture
def store() -> MemoryScarcityStore:
    memory = MemoryScarcityStore()
    scarcity_store.use_store(memory)
    return memory


def state(facts: PageFacts, url: str = URL, page_type: str = "storefront") -> Any:
    return {"facts": facts, "url": url, "cleaned_content": "", "page_type": page_type, "flags": []}


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("Hurry! Only 3 left in stock.", 3),
        ("just 2 items remaining", 2),
        ("12 left in stock", 12),
        ("Only 7 in stock", 7),
        ("Nur noch 4 Stück auf Lager", 4),
        ("Nog maar 2 op voorraad", 2),
        ("Plus que 5 en stock", 5),
        ("3 left turns to the store", None),
        ("Only 0 left", None),
        ("In stock, ships today", None),
    ],
)
def test_the_claim_needs_only_or_in_stock(text: str, count: int | None) -> None:
    assert stock_left(text) == count


def test_the_claim_is_read_from_the_whole_page_including_the_cart_form() -> None:
    html = "<html><body><h1>Glow lamp</h1><form><p>Only 3 left!</p><button>Add</button></form></body></html>"
    facts, _ = parse_page(html, URL)
    assert facts.stock_left == 3


def test_the_key_hides_the_url_and_ignores_tracking_but_not_the_variant() -> None:
    facts = PageFacts()
    key = scarcity_store.product_key(URL, facts)
    assert key is not None and len(key) == 32
    assert key == scarcity_store.product_key(
        "https://www.lamps.example/collections/all/products/glow-lamp?variant=123", facts
    )
    assert key != scarcity_store.product_key("https://lamps.example/products/glow-lamp?variant=456", facts)
    assert key != scarcity_store.product_key("https://lamps.example/products/other-lamp?variant=123", facts)
    assert scarcity_store.product_key("not a url", facts) is None


def claims(*pairs: tuple[int, int]) -> list[Claim]:
    return [Claim(DAY - timedelta(days=ago), count) for ago, count in pairs]


def test_the_same_count_for_over_a_week_is_static() -> None:
    found = assess(claims((10, 3), (4, 3)), Claim(DAY, 3))
    assert found is not None and (found.pattern, found.count, found.days, found.span_days) == ("static", 3, 3, 10)


@pytest.mark.parametrize(
    "earlier",
    [
        claims((3, 3), (1, 3)),  # three days, but within a week
        claims((10, 3)),  # two days only
        claims((10, 5), (4, 4)),  # selling down
        claims((10, 4), (4, 3), (2, 10)),  # one restock
    ],
)
def test_real_stock_patterns_are_no_finding(earlier: list[Claim]) -> None:
    assert assess(earlier, Claim(DAY, 3)) is None


def test_a_count_that_keeps_going_back_up_resets() -> None:
    found = assess(claims((9, 5), (7, 2), (5, 5), (3, 2)), Claim(DAY, 5))
    assert found is not None and found.pattern == "resets" and found.resets == 2


def test_selling_down_within_a_day_is_not_a_reset() -> None:
    assert assess(claims((2, 5), (2, 4), (1, 4), (1, 3)), Claim(DAY, 2)) is None


async def test_the_skill_fires_on_a_shop_page_from_recorded_days(store: MemoryScarcityStore) -> None:
    key = scarcity_store.product_key(URL, PageFacts())
    assert key is not None
    today = scarcity_store.today()
    for ago in (12, 5):
        await store.record(key, Claim(today - timedelta(days=ago), 3))
    out = await ScarcitySkill().run(state(PageFacts(stock_left=3)))
    assert out["features"]["scarcity.static"] == 1.0
    [finding] = out["findings"]
    assert finding.type == "scarcity_static" and "never changed" in finding.evidence
    assert (key, today, 3) in store.rows  # today's claim is recorded too


async def test_no_claim_records_nothing(store: MemoryScarcityStore) -> None:
    out = await ScarcitySkill().run(state(PageFacts()))
    assert out["features"]["scarcity.claim"] == 0.0 and not out["findings"] and not store.rows


async def test_the_test_shop_is_never_recorded(store: MemoryScarcityStore) -> None:
    from unfret.test_shop import is_test_shop  # noqa: PLC0415

    url = next(
        u for u in ("https://unfret.net/test-shop/lamp", "https://dev.unfret.net/test-shop/lamp") if is_test_shop(u)
    )
    assert await scarcity_store.record(url, PageFacts(stock_left=3)) is False
    assert not store.rows


async def test_claims_from_supplied_pages_count_only_once_corroborated(store: MemoryScarcityStore) -> None:
    key = scarcity_store.product_key(URL, PageFacts())
    assert key is not None
    client = observations.Provenance(fetched=False, tier="quick").bits
    await store.record(key, Claim(DAY - timedelta(days=10), 3), bits=client, vouch=1)
    assert await store.claims(key, DAY - timedelta(days=30)) == []
    await store.record(key, Claim(DAY - timedelta(days=10), 3), bits=client, vouch=2)
    assert await store.claims(key, DAY - timedelta(days=30)) == [Claim(DAY - timedelta(days=10), 3)]


async def test_nothing_about_the_page_address_or_the_requester_is_stored(store: MemoryScarcityStore) -> None:
    ip, account = "203.0.113.7", "pass_abc123"
    provenance = observations.for_request(ip, account, fetched=True, tier="deep")
    with observations.observed_as(provenance):
        await scarcity_store.observe(URL, PageFacts(stock_left=3))
    [(key, day, count)] = store.rows
    kept = store.rows[(key, day, count)]
    assert set(vars(kept)) == {"bits", "sources"} and count == 3 and isinstance(day, date)
    stored = repr(store.rows).encode() + key
    for needle in ("lamps.example", "glow-lamp", "utm_source", ip, account):
        assert needle.encode() not in stored and needle.encode().hex().encode() not in stored


def test_the_findings_are_level_1_and_markable() -> None:
    for kind in ("scarcity_static", "scarcity_resets"):
        assert levels.level_of(kind) == 1 and kind in levels.MARKABLE


def test_the_card_names_the_count_and_its_source() -> None:
    from unfret import card, shopper  # noqa: PLC0415
    from unfret.graph.state import Finding  # noqa: PLC0415

    found = Finding(
        skill="scarcity",
        type="scarcity_static",
        evidence='"Only 3 left" on 4 days over 12 days: the count never changed.',
    )
    verdict = shopper.build(
        40, features={"scarcity.static": 1.0}, findings=[found], facts=PageFacts(), page_type="storefront"
    )
    [reason] = [r for r in verdict.reasons if r.type == "scarcity_static"]
    assert "only 3 left" in reason.detail and "scam" not in (reason.title + reason.detail).lower()
    assert card.REASON_SOURCES["scarcity_static"] == card.REASON_SOURCES["scarcity_resets"] == "stock_history"
