"""
Provenance and corroboration in the Postgres stores, the routes that set the provenance, and a quick check that
records its page (and keeps nothing that names it) against a real database.
"""

import hashlib
from datetime import date
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.conftest import FakeGraph
from tests.unit.test_image_index import StubEmbedder, encode, product_photo
from tests.unit.test_observations import GTIN, PAGE, PHOTO, REVIEW_TEXTS
from unfret import (
    image_index,
    market_prices,
    observations,
    page_fetch,
    price_store,
    recording,
    review_store,
    scoring,
    shop_store,
)
from unfret.config import settings
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import reverse_image
from unfret.image_index import ImagePrint
from unfret.image_store import Listing, PostgresImageStore, lookup
from unfret.main import app, use_postgres_stores
from unfret.market_prices import PostgresMarketStore
from unfret.observations import CLIENT, DEEP, FETCHED, LEGACY, QUICK, Provenance
from unfret.price_store import Observation, PostgresPriceStore
from unfret.review_store import PostgresReviewStore
from unfret.routes import analyze
from unfret.x402.verdict import skills_for

DAY = date(2026, 11, 18)
_LOAD_MODEL = scoring.load_model


def key(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(observations, "witnesses", observations.Witnesses())
    monkeypatch.setattr(observations, "writes", observations.WriteLimit(observations.witnesses))
    monkeypatch.setattr(settings, "observation_min_sources", 3)


async def test_existing_rows_count_as_before(pool: asyncpg.Pool) -> None:
    for table in ("image_prints", "price_observations", "market_prices", "review_fingerprints"):
        default = await pool.fetchval(
            "SELECT column_default FROM information_schema.columns WHERE table_name = $1 AND column_name = $2",
            table,
            "provenance",
        )
        assert default == str(FETCHED | DEEP)  # rows the migration found were marked 16 (LEGACY): they count
    assert observations.counts(LEGACY, 0)


async def test_market_prices_count_once_corroborated_or_fetched(pool: asyncpg.Pool) -> None:
    store = PostgresMarketStore(pool)
    product = key("gtin")
    await store.record(product, key("fake.shop"), DAY, 129.0, "EUR", bits=CLIENT | QUICK, vouch=1)
    await store.record(product, key("fake.shop"), DAY, 129.0, "EUR", bits=CLIENT | DEEP, vouch=1)
    assert await store.others(product, key("me"), DAY, "EUR") == []
    await store.record(product, key("fake.shop"), DAY, 129.0, "EUR", bits=CLIENT | QUICK, vouch=1)
    assert await store.others(product, key("me"), DAY, "EUR") == [129.0]  # three sources
    row = await pool.fetchrow("SELECT provenance, sources FROM market_prices")
    assert (row["provenance"], row["sources"]) == (CLIENT | QUICK | DEEP, 3)

    # A forged price never replaces one that counts; a fetched one replaces one that doesn't yet.
    await store.record(product, key("real.shop"), DAY, 99.0, "EUR")
    await store.record(product, key("real.shop"), DAY, 1.0, "EUR", bits=CLIENT | QUICK, vouch=1)
    await store.record(product, key("other.shop"), DAY, 1.0, "EUR", bits=CLIENT | QUICK, vouch=1)
    await store.record(product, key("other.shop"), DAY, 89.0, "EUR", bits=FETCHED | QUICK, vouch=1)
    assert sorted(await store.others(product, key("me"), DAY, "EUR")) == [89.0, 99.0, 129.0]


async def test_price_history_counts_only_rows_that_count(pool: asyncpg.Pool) -> None:
    store = PostgresPriceStore(pool)
    product = key("asin")
    await store.record(product, Observation(date(2026, 10, 30), 9.95, "EUR", None), bits=CLIENT | DEEP, vouch=1)
    await store.record(product, Observation(date(2026, 11, 1), 79.95, "EUR", None))
    history = await store.history(product, DAY, "EUR")
    assert (history.first_seen, history.days, history.lowest) == (date(2026, 11, 1), 1, 79.95)
    assert (await store.latest(product)) == Observation(date(2026, 11, 1), 79.95, "EUR", None)
    # A fetched price replaces a forged one the same day that doesn't count; the reverse leaves it.
    await store.record(product, Observation(date(2026, 10, 30), 74.95, "EUR", 99.0))
    await store.record(product, Observation(date(2026, 11, 1), 1.0, "EUR", None), bits=CLIENT | QUICK, vouch=1)
    rows = await pool.fetch("SELECT day, price, was_price, provenance FROM price_observations ORDER BY day")
    assert [(float(r["price"]), r["was_price"] and float(r["was_price"]), r["provenance"]) for r in rows] == [
        (74.95, 99.0, FETCHED | DEEP),
        (79.95, None, FETCHED | DEEP),
    ]


async def test_review_fingerprints_count_once_corroborated(pool: asyncpg.Pool) -> None:
    store = PostgresReviewStore(pool)
    reviews = {key("r1"), key("r2")}
    for _ in range(2):
        await store.record(key("fake.shop"), reviews, DAY, bits=CLIENT | QUICK, vouch=1)
    assert await store.matches(key("me"), reviews) == (0, 0)
    await store.record(key("fake.shop"), reviews, DAY, bits=CLIENT | DEEP, vouch=1)
    assert await store.matches(key("me"), reviews) == (2, 1)
    await store.record(key("real.shop"), {key("r1")}, DAY)
    assert await store.matches(key("me"), reviews) == (2, 2)


def _print(seed: int) -> ImagePrint:
    model = StubEmbedder()
    return image_index.fingerprint(encode(product_photo(seed), "JPEG", quality=70), model)


async def test_image_index_counts_once_corroborated_and_keeps_a_photo_that_counts(pool: asyncpg.Pool) -> None:
    store = PostgresImageStore(pool)
    photo, other = _print(31), _print(32)
    fake = Listing("fake.shop", key("fake"))
    for _ in range(2):
        assert await store.upsert(
            fake, photo, image_url="https://x/1.jpg", day=DAY, source="seen", bits=CLIENT, vouch=1
        )
    assert (await lookup(store, photo, None)).matches == []
    await store.upsert(fake, photo, image_url="https://x/2.jpg", day=DAY, source="seen", bits=CLIENT | QUICK, vouch=1)
    (match,) = (await lookup(store, photo, None)).matches
    assert match.domain == "fake.shop"
    row = await pool.fetchrow("SELECT image_url, provenance, sources FROM image_prints")
    assert tuple(row) == ("https://x/1.jpg", CLIENT | QUICK, 3)  # the same photo: the row's address stays

    real = Listing("real.shop", key("real"))
    real_id = await store.upsert(real, photo, image_url="https://real/1.jpg", day=DAY, source="seen")
    assert await store.upsert(real, other, image_url="https://evil/1.jpg", day=DAY, source="seen", bits=CLIENT) is None
    assert await store.image_url(real_id or 0) == "https://real/1.jpg"
    # A listing's new photo from a page Unfret fetched replaces the old one, as before.
    assert await store.upsert(real, other, image_url="https://real/2.jpg", day=DAY, source="seen") == real_id
    assert await store.image_url(real_id or 0) == "https://real/2.jpg"


# ── The routes say where the page came from ─────────────────────────────────────────────────────────────────


class WitnessGraph(FakeGraph):
    """A fake graph that notes the provenance the analysis runs with."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[Provenance] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.seen.append(observations.current())
        return await super().ainvoke(state, config)

    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        self.seen.append(observations.current())
        async for item in super().astream(*args, **kwargs):
            self.seen.append(observations.current())
            yield item


@pytest.fixture
def witness(monkeypatch: pytest.MonkeyPatch) -> WitnessGraph:
    graph = WitnessGraph()
    monkeypatch.setattr(app, "graph", graph)
    monkeypatch.setattr(app, "quick_graph", graph)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(analyze, "_fetch_rate", analyze.RateLimit(1000))

    async def fetch(url: str, **_: Any) -> bytes:
        return PAGE.encode()

    monkeypatch.setattr(page_fetch, "fetch_public", fetch)
    return graph


async def test_analyses_run_with_their_provenance(
    client: httpx.AsyncClient, witness: WitnessGraph, wallet: Any
) -> None:
    _, headers = await wallet(settings.analysis_price_usd_micros)
    page = {"page_content": PAGE, "url": "https://glowlamp.shop/p/1"}
    assert (await client.post("/v1/analyze", json=page, headers=headers)).status_code == 200
    quick = {"url": "https://glowlamp.shop/p/2", "tier": "quick"}
    assert (await client.post("/v1/analyze", json=quick)).status_code == 200
    stream = {**page, "url": "https://glowlamp.shop/p/3", "tier": "quick"}
    assert (await client.post("/v1/analyze/stream", json=stream)).status_code == 200

    deep, url_only, *streamed = witness.seen
    assert (deep.fetched, deep.tier, len(deep.sources), deep.record) == (False, "deep", 2, True)  # network + pass
    assert (url_only.fetched, url_only.tier, len(url_only.sources)) == (True, "quick", 1)  # no account: the network
    assert len(streamed) > 1 and all(p.fetched is False and p.tier == "quick" for p in streamed)
    assert deep.sources[0] == url_only.sources[0]  # one network, today
    assert observations.current() == observations.TRUSTED


# ── A quick check records its page, after the answer, and nothing names the page ─────────────────────────────


async def test_a_quick_check_records_its_page_and_keeps_nothing_that_names_it(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://glowlamp.shop/products/secret-lamp-91c?ref=share-7f3a"
    use_postgres_stores(pool)
    for module in (price_store, market_prices, review_store, shop_store, reverse_image):
        monkeypatch.setattr(module, "_store", module._store)
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(settings, "price_observations", True)
    monkeypatch.setattr(scoring, "load_model", _LOAD_MODEL)
    monkeypatch.setattr(image_index, "_embedder", StubEmbedder())
    monkeypatch.setattr(image_index, "embedder", lambda: image_index._embedder)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(analyze, "_fetch_rate", analyze.RateLimit(1000))
    graph = build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False, record_page=True)
    monkeypatch.setattr(app, "quick_graph", graph)
    photo = encode(product_photo(33), "JPEG", quality=70)

    async def fetch(target: str, **_: Any) -> bytes:
        return {url: PAGE.encode(), PHOTO: photo}[target]

    monkeypatch.setattr(page_fetch, "fetch_public", fetch)
    monkeypatch.setattr(reverse_image, "fetch_public", fetch)

    response = await client.post("/v1/analyze", json={"url": url, "tier": "quick"})
    await recording.drain()
    await reverse_image.drain()
    await shop_store.drain()

    assert response.status_code == 200 and response.json()["tier"] == "quick"
    fetched = FETCHED | QUICK
    assert await pool.fetchval("SELECT provenance FROM image_prints WHERE domain = 'glowlamp.shop'") == fetched
    assert await pool.fetchval("SELECT provenance FROM market_prices") == fetched
    assert {r[0] for r in await pool.fetch("SELECT provenance FROM review_fingerprints")} == {fetched}
    assert await pool.fetchval("SELECT COUNT(*) FROM review_fingerprints") == len(REVIEW_TEXTS)
    tables = [r[0] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    for table in tables:
        text = "\n".join(r[0] for r in await pool.fetch(f'SELECT t::text FROM "{table}" t'))  # noqa: S608
        for needle in ("secret-lamp-91c", "share-7f3a", "testclient", "127.0.0.1"):
            assert needle not in text and needle.encode().hex() not in text, (table, needle)
    assert GTIN not in str(await pool.fetch("SELECT * FROM market_prices"))  # hashed

    # The operator sees counts only.
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    seen = await client.get("/v1/admin/observations", headers={"Authorization": "Bearer operator-token"})
    assert seen.status_code == 200
    body = seen.json()
    assert body["stores"]["image_prints"] == {"rows": 1, "counting": 1, "quick": 1, "client": 0, "fetched": 1}
    assert body["stores"]["review_fingerprints"]["counting"] == len(REVIEW_TEXTS)
    assert body["quick_recording"]["pages"] >= 1 and body["min_sources"] == 3
    assert "glowlamp" not in seen.text
