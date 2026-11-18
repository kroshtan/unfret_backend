"""
Free checks feed the stores, safely: provenance, corroboration, write limits, and the quick tier's background record.

A forged page (any shop's address, any price, any photo, sent as client content) alone must never make another shop
look like it reuses a photo, sells far below market, never charged its "was" price or copied its reviews.
"""

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_image_index import StubEmbedder, encode, product_photo
from unfret import image_index, market_prices, observations, price_store, recording, review_store, scoring, shop_store
from unfret.config import settings
from unfret.facts import PageFacts, Review
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import reverse_image
from unfret.graph.skills.market_price import MarketPriceSkill
from unfret.graph.skills.pricing import PricingSkill
from unfret.graph.skills.reverse_image import ReverseImageSkill
from unfret.graph.skills.review_copies import ReviewCopiesSkill
from unfret.image_store import MemoryImageStore
from unfret.observations import CLIENT, DEEP, FETCHED, LEGACY, QUICK, Kept, Provenance, merge
from unfret.x402.verdict import skills_for, to_verdict

_LOAD_MODEL = scoring.load_model  # the real one, captured before the suite's autouse fixture hides it
GTIN = "4006381333931"
PHOTO = "https://cdn.glowlamp.shop/lamp.jpg"
URL = "https://glowlamp.shop/products/lamp"
REVIEW_TEXTS = [
    f"Review number {i}: this lamp arrived quickly, the light is warm and bright, I really love it here."
    for i in range(4)
]
LD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Glow Lamp",
    "brand": {"@type": "Brand", "name": "Lumi"},
    "image": PHOTO,
    "gtin13": GTIN,
    "offers": {
        "@type": "Offer",
        "price": "39.00",
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    },
    "review": [
        {
            "@type": "Review",
            "reviewBody": t,
            "author": {"@type": "Person", "name": "A"},
            "reviewRating": {"ratingValue": 5},
        }
        for t in REVIEW_TEXTS
    ],
}
PAGE = (
    f'<html><head><title>Glow Lamp</title><script type="application/ld+json">{json.dumps(LD)}</script></head>'
    f"<body><h1>Glow Lamp</h1><p>€39.00</p><button>Add to cart</button><p>{'A warm lamp for your desk. ' * 30}</p>"
    "</body></html>"
)


def client(network: str, tier: observations.Tier = "deep", account: str | None = None) -> Provenance:
    """A check of content the caller supplied, from this network."""
    return observations.for_request(network, account, fetched=False, tier=tier)


def fetched(network: str = "198.51.100.1", tier: observations.Tier = "deep") -> Provenance:
    return observations.for_request(network, None, fetched=True, tier=tier)


def state(facts: PageFacts, url: str = URL, page_type: str = "storefront") -> Any:
    return {"page_content": "", "url": url, "flags": [], "facts": facts, "cleaned_content": "", "page_type": page_type}


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    price_store.use_store(price_store.MemoryPriceStore())
    market_prices.use_store(market_prices.MemoryMarketStore())
    review_store.use_store(review_store.MemoryReviewStore())
    shop_store.use_store(shop_store.MemoryShopStore())
    monkeypatch.setattr(observations, "witnesses", observations.Witnesses())
    monkeypatch.setattr(observations, "writes", observations.WriteLimit(observations.witnesses))
    monkeypatch.setattr(recording, "stats", recording.stats.__class__())
    monkeypatch.setattr(settings, "observation_min_sources", 3)
    monkeypatch.setattr(settings, "observation_pages_per_network_per_day", 300)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> MemoryImageStore:
    """The photo index in memory, with a stand-in for the embedding model."""
    monkeypatch.setattr(image_index, "_embedder", StubEmbedder())
    monkeypatch.setattr(image_index, "embedder", lambda: image_index._embedder)
    monkeypatch.setattr(settings, "fingerprints", True)
    store = MemoryImageStore()
    reverse_image.use_store(store)
    return store


# ── Provenance and the merge rules ───────────────────────────────────────────────────────────────────────────


def test_provenance_bits_say_where_and_which_tier() -> None:
    assert fetched().bits == FETCHED | DEEP
    assert client("203.0.113.9", "quick").bits == CLIENT | QUICK
    assert observations.current() == observations.TRUSTED  # outside a check: as every observation was before
    assert observations.counts(LEGACY, 0) and observations.counts(FETCHED | QUICK, 0)
    assert not observations.counts(CLIENT | DEEP, 2) and observations.counts(CLIENT | DEEP, 3)


@pytest.mark.parametrize(
    ("old", "bits", "same", "newer_wins", "action", "kept"),
    [
        (None, CLIENT, False, True, "insert", Kept(CLIENT, 1)),
        (Kept(CLIENT, 1), CLIENT | QUICK, True, True, "vouch", Kept(CLIENT | QUICK, 2)),
        # A different value that doesn't count never replaces one that does, photo or price.
        (Kept(FETCHED, 0), CLIENT, False, True, "keep", Kept(FETCHED, 0)),
        (Kept(CLIENT, 3), CLIENT, False, False, "keep", Kept(CLIENT, 3)),
        # A fetched page replaces what doesn't count yet; for prices the first fetched price of the day stays.
        (Kept(CLIENT, 1), FETCHED, False, False, "replace", Kept(FETCHED, 1)),
        (Kept(FETCHED, 0), FETCHED, False, False, "keep", Kept(FETCHED, 0)),
        (Kept(FETCHED, 0), FETCHED, False, True, "replace", Kept(FETCHED, 1)),  # a listing's new photo
        (Kept(CLIENT, 1), CLIENT, False, True, "replace", Kept(CLIENT, 1)),
        (Kept(CLIENT, 1), CLIENT, False, False, "keep", Kept(CLIENT, 1)),
    ],
)
def test_merge(*, old: Kept | None, bits: int, same: bool, newer_wins: bool, action: str, kept: Kept) -> None:
    assert merge(old, bits, 1, same=same, newer_wins=newer_wins) == (action, kept)


# ── Counting sources without linking a pass to a page ───────────────────────────────────────────────────────


def test_a_source_counts_once_per_row_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    w = observations.witnesses
    a, b = client("203.0.113.1", account="key:a"), client("203.0.113.2", account="key:b")
    assert (w.vouch(a, b"row"), w.vouch(a, b"row"), w.vouch(b, b"row"), w.vouch(a, b"other")) == (1, 0, 1, 1)
    # The same pass from another network, or another pass from the same network, is no new source.
    assert w.vouch(client("203.0.113.3", account="key:a"), b"row") == 0
    assert w.vouch(client("203.0.113.1", account="key:c"), b"row") == 0
    assert w.vouch(observations.TRUSTED, b"row") == 0  # no sources: counts nothing
    # Tomorrow the salt and the marks are new: a source counts again, and today's marks can't be checked any more.
    salt = w._salt
    monkeypatch.setattr(observations, "_today", lambda: date.today() + timedelta(days=1))
    assert w.vouch(client("203.0.113.1", account="key:a"), b"row") == 1
    assert w._salt != salt and len(w._marks) == 2


def test_the_marks_are_keyed_hashes_and_bounded() -> None:
    w = observations.Witnesses(cap=4)
    p = client("203.0.113.1", account="key:secret-pass")
    assert all(b"203.0.113" not in s and b"secret" not in s for s in p.sources)
    assert w.vouch(p, b"r1") == 1 and w.vouch(client("203.0.113.2"), b"r1") == 1
    assert w.vouch(client("203.0.113.4"), b"r2") == 1  # the cap: 4 marks
    assert w.vouch(client("203.0.113.5"), b"r2") == 0  # past it nothing counts until tomorrow
    assert all(isinstance(m, int) for m in w._marks)


def test_write_limit_per_network_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "observation_pages_per_network_per_day", 2)
    assert [client("203.0.113.7", "quick").record for _ in range(3)] == [True, True, False]
    assert client("2001:db8::1", "quick").record and client("2001:db8::2").record  # one /64
    assert not client("2001:db8::3", "quick").record
    assert client("203.0.113.8").record  # another network
    # A deep check of a page Unfret fetched records as before, without a limit; a quick one counts.
    assert all(fetched("203.0.113.7").record for _ in range(5))
    assert not fetched("203.0.113.7", "quick").record
    monkeypatch.setattr(observations, "_today", lambda: date.today() + timedelta(days=1))
    assert client("203.0.113.7", "quick").record


# ── A forged page alone can't make another shop look bad ────────────────────────────────────────────────────


async def _forge(prov: Provenance, coro_fn: Any) -> None:
    with observations.observed_as(prov):
        await coro_fn()


async def test_forged_prices_dont_make_a_shop_far_below_market() -> None:
    facts = PageFacts(price=39.0, currency="EUR", product_ids={"gtin13": GTIN})
    high = facts.model_copy(update={"price": 129.0})
    one_network = client("203.0.113.66")
    for i in range(5):  # five made-up shops, one network
        await _forge(one_network, lambda i=i: market_prices.observe(f"https://fake{i}.shop/p", high))
    out = await MarketPriceSkill().run(state(facts, "https://victim.shop/p"))
    assert out["features"]["market_price.other_shops"] == 0 and "market_price.far_below" not in out["features"]

    # Corroborated by three independent sources, or seen on pages Unfret fetched, the same prices count.
    for network in ("203.0.113.67", "203.0.113.68"):
        for i in range(3):
            await _forge(client(network), lambda i=i: market_prices.observe(f"https://fake{i}.shop/p", high))
    out = await MarketPriceSkill().run(state(facts, "https://victim.shop/p"))
    assert out["features"]["market_price.other_shops"] == 3 and out["features"]["market_price.far_below"] == 1.0


async def test_a_forged_price_doesnt_overwrite_a_fetched_one() -> None:
    facts = PageFacts(price=129.0, currency="EUR", product_ids={"gtin13": GTIN})
    for i in range(3):
        await _forge(fetched(), lambda i=i: market_prices.observe(f"https://shop{i}.example/p", facts))
        for network in ("203.0.113.1", "203.0.113.2", "203.0.113.3"):
            cheap = facts.model_copy(update={"price": 1.0})
            await _forge(client(network), lambda i=i, c=cheap: market_prices.observe(f"https://shop{i}.example/p", c))
    store: market_prices.MemoryMarketStore = market_prices._store  # type: ignore[assignment]
    assert {price for price, _ in store.rows.values()} == {129.0}
    assert set(store.kept.values()) == {Kept(FETCHED | DEEP, 1)}  # the forged ones left no trace


async def test_a_forged_low_price_doesnt_make_a_was_price_look_never_charged() -> None:
    url = "https://www.amazon.nl/dp/B0ABC12345"
    facts = PageFacts(platform="amazon", price=39.95, list_price=79.95, currency="EUR")
    key = price_store.product_key(url, facts)
    assert key is not None
    store: price_store.MemoryPriceStore = price_store._store  # type: ignore[assignment]
    today = price_store.today()
    await store.record(key, price_store.Observation(today - timedelta(days=40), 79.95, "EUR", None))
    for days in (35, 20, 10):  # forged history: the product "was" cheap
        obs = price_store.Observation(today - timedelta(days=days), 9.95, "EUR", None)
        await store.record(key, obs, bits=CLIENT | QUICK, vouch=1)
    out = await PricingSkill().run(state(facts, url, "aggregate_listing") | {"cleaned_content": ""})
    assert "pricing.was_above_30d_low" not in out["features"]
    assert (await store.history(key, today, "EUR")).lowest is None  # only the fetched row, outside the window


async def test_forged_reviews_dont_make_a_shops_reviews_look_copied() -> None:
    reviews = PageFacts(reviews=[Review(body=t) for t in REVIEW_TEXTS])
    for i in range(3):
        await _forge(client("203.0.113.66"), lambda i=i: review_store.observe(f"https://fake{i}.shop/p", reviews))
    out = await ReviewCopiesSkill().run(state(reviews, "https://victim.shop/p"))
    assert out["features"]["review_copies.copied"] == 0
    for network in ("203.0.113.67", "203.0.113.68"):
        await _forge(client(network), lambda: review_store.observe("https://fake0.shop/p", reviews))
    out = await ReviewCopiesSkill().run(state(reviews, "https://victim.shop/p"))
    assert (out["features"]["review_copies.copied"], out["features"]["review_copies.other_shops"]) == (4, 1)


def _serve(monkeypatch: pytest.MonkeyPatch, photos: dict[str, bytes]) -> list[str]:
    fetched_urls: list[str] = []

    async def fetch(url: str, **_: Any) -> bytes:
        fetched_urls.append(url)
        return photos[url]

    monkeypatch.setattr(reverse_image, "fetch_public", fetch)
    return fetched_urls


async def test_forged_pages_dont_make_a_shops_photo_look_reused(
    stub: MemoryImageStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {PHOTO: encode(product_photo(21), "JPEG", quality=70)})
    facts = PageFacts(image_urls=[PHOTO])
    for i in range(60):  # the victim's photo "on" 60 made-up shops: neither reused nor a stock photo
        await _forge(client("203.0.113.66"), lambda i=i: ReverseImageSkill().run(state(facts, f"https://f{i}.shop/p")))
    await reverse_image.drain()
    assert len(stub.rows) == 60
    out = await ReverseImageSkill().run(state(facts))
    assert (out["features"]["reverse_image.searched"], out["features"]["reverse_image.other_shops"]) == (1.0, 0.0)
    assert out["findings"] == []

    # The same photo on a page Unfret fetched itself counts at once.
    await _forge(fetched(), lambda: ReverseImageSkill().run(state(facts, "https://real.shop/p")))
    await reverse_image.drain()
    out = await ReverseImageSkill().run(state(facts))
    assert out["features"]["reverse_image.other_shops"] == 1.0


async def test_a_forged_photo_doesnt_replace_a_listing_photo_that_counts(
    stub: MemoryImageStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "https://evil.example/other.jpg"
    _serve(monkeypatch, {PHOTO: encode(product_photo(22), "JPEG", quality=70), other: encode(product_photo(23))})
    await _forge(fetched(), lambda: ReverseImageSkill().run(state(PageFacts(image_urls=[PHOTO]))))
    await reverse_image.drain()
    ((row, image_url, *_),) = stub.rows.values()
    await _forge(client("203.0.113.66"), lambda: ReverseImageSkill().run(state(PageFacts(image_urls=[other]))))
    await reverse_image.drain()
    ((after, after_url, *_),) = stub.rows.values()
    assert after.print == row.print and after_url == image_url == PHOTO


# ── Quick checks record, after the answer, without changing it ──────────────────────────────────────────────


def _quick_graph(*, record: bool) -> Any:
    return build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False, record_page=record)


def _answer(result: dict[str, Any]) -> dict[str, Any]:
    verdict = to_verdict(result, policy="balanced", tier="quick").model_dump(mode="json")
    verdict.pop("checked_at")
    kept = {k: result.get(k) for k in ("score", "explanation", "page_type", "assessment", "verdict", "features")}
    return {"verdict": verdict, **kept, "findings": [f.model_dump() for f in result["findings"]]}


async def _quick(graph: Any, prov: Provenance, url: str = URL) -> dict[str, Any]:
    with observations.observed_as(prov):
        result: dict[str, Any] = await graph.ainvoke({"page_content": PAGE, "url": url, "flags": [], "explain": False})
    await recording.drain()
    await reverse_image.drain()
    return result


@pytest.fixture
def quick_model(monkeypatch: pytest.MonkeyPatch) -> None:
    quick = _LOAD_MODEL.__wrapped__(scoring.MODEL_QUICK_PATH)
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: quick)


@pytest.mark.usefixtures("quick_model")
async def test_a_quick_check_records_what_a_deep_check_would_and_answers_the_same(
    stub: MemoryImageStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched_urls = _serve(monkeypatch, {PHOTO: encode(product_photo(24), "JPEG", quality=70)})
    before = await _quick(_quick_graph(record=False), client("203.0.113.5", "quick"))
    assert not stub.rows and not market_prices._store.rows  # type: ignore[attr-defined]
    after = await _quick(_quick_graph(record=True), client("203.0.113.5", "quick"))
    assert _answer(after) == _answer(before)  # the same answer, tier, scorer and features

    assert fetched_urls == [PHOTO]  # the photo, after the answer; nothing else
    assert [domain for domain, _ in stub.rows] == ["glowlamp.shop"]
    assert list(stub.kept.values()) == [Kept(CLIENT | QUICK, 1)]
    market: market_prices.MemoryMarketStore = market_prices._store  # type: ignore[assignment]
    assert list(market.rows.values()) == [(39.0, "EUR")] and list(market.kept.values()) == [Kept(CLIENT | QUICK, 1)]
    reviews: review_store.MemoryReviewStore = review_store._store  # type: ignore[assignment]
    assert len(reviews.rows) == 4 and set(reviews.kept.values()) == {Kept(CLIENT | QUICK, 1)}
    assert recording.stats["pages"] == 1 and recording.stats["photos"] == 1

    # The same page from a page Unfret fetched, quick: counts at once.
    await _quick(_quick_graph(record=True), fetched("203.0.113.9", "quick"))
    assert market.kept[next(iter(market.kept))] == Kept(CLIENT | QUICK | FETCHED, 2)


@pytest.mark.usefixtures("quick_model")
async def test_quick_recording_is_bounded_and_skips_the_test_shop(
    stub: MemoryImageStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {PHOTO: encode(product_photo(25), "JPEG", quality=70)})
    graph = _quick_graph(record=True)
    monkeypatch.setattr(settings, "quick_record_queue", 0)
    await _quick(graph, client("203.0.113.5", "quick"))
    assert recording.stats["queue_full"] == 1 and not stub.rows
    monkeypatch.setattr(settings, "quick_record_queue", 16)
    monkeypatch.setattr(settings, "observation_pages_per_network_per_day", 0)
    await _quick(graph, client("203.0.113.6", "quick"))
    assert recording.stats["over_limit"] == 1 and not stub.rows
    monkeypatch.setattr(settings, "observation_pages_per_network_per_day", 300)
    await _quick(graph, fetched("203.0.113.6", "quick"), url="https://unfret.net/test-shop/lamp")
    assert not stub.rows and not market_prices._store.rows  # type: ignore[attr-defined]


async def test_a_slow_photo_is_cut_off(monkeypatch: pytest.MonkeyPatch, stub: MemoryImageStore) -> None:
    async def slow(url: str, **_: Any) -> bytes:
        await asyncio.sleep(10)
        return b""

    monkeypatch.setattr(reverse_image, "fetch_public", slow)
    monkeypatch.setattr(recording, "TIMEOUT_S", 0.05)
    facts = PageFacts(image_urls=[PHOTO], price=39.0, currency="EUR", product_ids={"gtin13": GTIN})
    await recording.record(state(facts))
    assert recording.stats["timed_out"] == 1 and not stub.rows


async def test_the_provenance_reaches_a_streamed_analysis() -> None:
    seen: list[Provenance] = []

    async def stream() -> Any:
        for i in range(2):
            seen.append(observations.current())
            yield i

    prov = client("203.0.113.5", "quick")
    assert [i async for i in observations.stream_as(prov, stream())] == [0, 1]
    assert seen == [prov, prov] and observations.current() == observations.TRUSTED


def test_the_privacy_statement_says_what_is_kept_of_free_checks() -> None:
    text = " ".join((Path(__file__).parents[2] / "src" / "unfret" / "static" / "privacy.html").read_text().split())
    assert "A quick check records these too, after it has answered; it doesn't look them up." in text
    assert "how many independent checks reported the same (a number)" in text
    assert "under a key it replaces every day and never stores" in text
