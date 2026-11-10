"""Migrations, the Postgres fact cache, price and shop stores against a real database."""

import time
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import numpy as np
import pytest
from pydantic import SecretStr

from unfret import fact_cache, image_seed, shop_store, verdict_store
from unfret.config import settings
from unfret.db import PostgresFactStore
from unfret.facts import PageFacts
from unfret.image_index import ImagePrint
from unfret.image_store import Listing, PostgresImageStore, lookup
from unfret.migrations import MIGRATIONS, migrate
from unfret.price_store import History, Observation, PostgresPriceStore
from unfret.shop_store import MAX_SHARE, PostgresShopStore, ShopRecord
from unfret.verdict_store import PostgresVerdictStore, Stored, listing_key
from unfret.x402.verdict import Verdict


async def test_migrations_are_recorded_and_idempotent(pool: asyncpg.Pool) -> None:
    versions = [r["version"] for r in await pool.fetch("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [v for v, _, _ in MIGRATIONS]
    assert await migrate(pool) == []


async def test_a_new_migration_applies_once(pool: asyncpg.Pool) -> None:
    extra = [*MIGRATIONS, (999, "test table", "CREATE TABLE migration_probe (id INT)")]
    try:
        assert await migrate(pool, extra) == [999]
        assert await migrate(pool, extra) == []
    finally:
        await pool.execute("DROP TABLE IF EXISTS migration_probe")
        await pool.execute("DELETE FROM schema_migrations WHERE version = 999")


async def test_postgres_fact_store(pool: asyncpg.Pool) -> None:
    store = PostgresFactStore(pool)
    assert await store.get("rdap", "shop.test") == (False, None)

    await store.put("rdap", "shop.test", {"registered": "2026-09-01"}, ttl_s=60)
    assert await store.get("rdap", "shop.test") == (True, {"registered": "2026-09-01"})

    await store.put("rdap", "gone.test", None, ttl_s=60)
    assert await store.get("rdap", "gone.test") == (True, None)

    await store.put("rdap", "old.test", {"x": 1}, ttl_s=-1)
    assert await store.get("rdap", "old.test") == (False, None)


async def test_cached_fact_uses_postgres(pool: asyncpg.Pool) -> None:
    calls: list[str] = []

    async def load() -> dict[str, str]:
        calls.append("loaded")
        return {"first_cert": "2026-09-03"}

    fact_cache.use_store(PostgresFactStore(pool))
    try:
        assert await fact_cache.cached_fact("crtsh", "shop.test", load) == {"first_cert": "2026-09-03"}
        assert await fact_cache.cached_fact("crtsh", "shop.test", load) == {"first_cert": "2026-09-03"}
    finally:
        fact_cache.use_store(fact_cache.MemoryStore())
    assert calls == ["loaded"]


async def test_postgres_price_store_keeps_one_anonymous_row_per_day(pool: asyncpg.Pool) -> None:
    store = PostgresPriceStore(pool)
    key, today = b"\x01" * 32, date(2026, 11, 8)
    await store.record(key, Observation(today - timedelta(days=95), 5.0, "EUR", None))
    await store.record(key, Observation(today - timedelta(days=40), 30.0, "EUR", 60.0))
    await store.record(key, Observation(today - timedelta(days=3), 24.95, "EUR", 59.95))
    await store.record(key, Observation(today - timedelta(days=3), 1.0, "EUR", None))  # same day: dropped
    await store.record(key, Observation(today - timedelta(days=2), 9.0, "USD", None))

    await store.purge(today - timedelta(days=90))
    history = await store.history(key, today, "EUR")

    assert history == History(first_seen=today - timedelta(days=40), days=1, lowest=24.95)
    columns = {
        r["column_name"]
        for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'price_observations'"
        )
    }
    assert columns == {"product_key", "day", "price", "currency", "was_price"}  # no URL, caller or time


TODAY = date(2026, 11, 9)


async def test_postgres_shop_store_links_one_hop_and_keeps_first_seen(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    await store.record(ShopRecord("a.shop", TODAY - timedelta(days=3), links={"meta_pixel": {"111"}}))
    await store.record(ShopRecord("a.shop", TODAY, registered=date(2026, 10, 1), links={"meta_pixel": {"111"}}))
    await store.record(ShopRecord("b.shop", TODAY, links={"meta_pixel": {"111"}, "ga": {"G-BBBBBBBB1"}}))
    await store.record(ShopRecord("c.shop", TODAY, links={"ga": {"G-BBBBBBBB1"}}))
    (link,) = await store.linked("a.shop", {})
    assert (link.domain, link.kinds, link.shared) == ("b.shop", ("meta_pixel",), ("111",))  # c.shop isn't linked
    (back,) = [x for x in await store.linked("c.shop", {}) if x.domain == "b.shop"]
    assert back.kinds == ("ga",)
    row = await pool.fetchrow("SELECT first_seen, last_seen, registered FROM shops WHERE domain = 'a.shop'")
    assert tuple(row) == (TODAY - timedelta(days=3), TODAY, date(2026, 10, 1))
    assert [x.domain for x in await store.linked("new.shop", {"ga": {"G-BBBBBBBB1"}})] == ["b.shop", "c.shop"]


async def test_postgres_shop_store_ignores_overshared_ids_and_forgets(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    for i in range(MAX_SHARE + 1):
        await store.record(ShopRecord(f"s{i}.shop", TODAY, links={"gtm": {"GTM-AGENCY1"}}))
    assert await store.linked("s0.shop", {}) == []
    await store.record(ShopRecord("x.shop", TODAY - timedelta(days=200), links={"email": {"e" * 64}, "ga": {"G-X1"}}))
    await store.record(ShopRecord("y.shop", TODAY, links={"email": {"e" * 64}}))
    await store.purge(TODAY)
    kinds = {r["kind"] for r in await pool.fetch("SELECT kind FROM shop_links WHERE domain = 'x.shop'")}
    assert kinds == {"ga"}  # the contact went after 180 days, the shop stays 400
    assert await store.forget_contact("email", "e" * 64) == 1
    assert await store.forget_shop("x.shop") is True
    assert await pool.fetchval("SELECT COUNT(*) FROM shop_links WHERE domain = 'x.shop'") == 0
    await store.purge(TODAY + timedelta(days=401))
    assert await pool.fetchval("SELECT COUNT(*) FROM shops") == 0


async def test_contacts_can_only_be_stored_hashed(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await store.record(ShopRecord("z.shop", TODAY, links={"email": {"owner@z.shop"}}))
    assert await pool.fetchval("SELECT COUNT(*) FROM shops WHERE domain = 'z.shop'") == 0  # one transaction


async def test_fingerprint_tables_hold_no_people_pages_times_or_verdicts(pool: asyncpg.Pool) -> None:
    columns = await pool.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_name IN ('shops', 'shop_links')"
    )
    names = {c["column_name"] for c in columns}
    forbidden = (
        "url",
        "path",
        "ip",
        "wallet",
        "pubkey",
        "payer",
        "caller",
        "user",
        "score",
        "verdict",
        "risk",
        "flag",
    )
    assert not {n for n in names if any(word in n for word in forbidden)}
    assert {c["data_type"] for c in columns if "seen" in c["column_name"]} == {"date"}
    assert not [c for c in columns if "time" in c["data_type"]]


async def test_recording_a_real_page_leaves_no_page_or_contact_in_any_row(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr("k"))
    shop_store.use_store(PostgresShopStore(pool))
    try:
        await shop_store.observe(
            {
                "url": "https://www.glowlamp.shop/products/secret-lamp?utm_source=friend&ref=wallet9xyz",
                "page_type": "storefront",
                "facts": PageFacts(platform="shopify", title="Secret Lamp"),
                "page_content": "<script>fbq('init', '1234509876543')</script>"
                '<a href="mailto:owner@glowlamp.shop">mail</a> <a href="tel:+31612345678">call</a>',
            }
        )
    finally:
        shop_store.use_store(shop_store.MemoryShopStore())
    rows = [r["row"] for r in await pool.fetch("SELECT row_to_json(t)::text AS row FROM shops t")]
    rows += [r["row"] for r in await pool.fetch("SELECT row_to_json(t)::text AS row FROM shop_links t")]
    dump = " ".join(rows).lower()
    assert "glowlamp.shop" in dump and "1234509876543" in dump  # the shop and its pixel are what is kept
    for leaked in ("secret-lamp", "products", "utm_source", "wallet9xyz", "owner@", "612345678", "secret lamp"):
        assert leaked not in dump, leaked


def _print(phash: int, axis: int = 0) -> ImagePrint:
    # A unit embedding along one axis: the same axis is the same photo to the embedding, another axis isn't.
    embedding = bytearray(384)
    embedding[axis] = 127
    return ImagePrint(phash, phash, bytes(embedding), bytes(48))


async def test_postgres_image_store_gates_by_hamming_distance_in_sql(pool: asyncpg.Pool) -> None:
    store = PostgresImageStore(pool)
    base = 0x0F0F_0F0F_0F0F_0F0F
    await store.upsert(
        Listing("a.shop", b"a" * 32), _print(base), image_url="https://a/1.jpg", day=TODAY, source="seen"
    )
    await store.upsert(Listing("b.shop", b"b" * 32), _print(base ^ 0b111), image_url="u", day=TODAY, source="seen")
    await store.upsert(Listing("c.shop", b"c" * 32), _print(~base), image_url="u", day=TODAY, source="seen")
    signed = -0x7000_0000_0000_0001  # the top bit set: hashes are stored signed
    await store.upsert(Listing("d.shop", b"d" * 32), _print(signed), image_url="u", day=TODAY, source="seen")
    assert {c.domain for c in await store.candidates(base, 3)} == {"a.shop", "b.shop"}
    assert {c.domain for c in await store.candidates(signed ^ 1, 1)} == {"d.shop"}
    found = await lookup(store, _print(base), Listing("a.shop", b"z" * 32))
    assert [m.domain for m in found.matches] == ["b.shop"]  # its own shop doesn't count
    assert await store.image_url(found.matches[0].id - 1) == "https://a/1.jpg"


async def test_postgres_image_store_updates_a_listing_and_keeps_the_seed(pool: asyncpg.Pool) -> None:
    store = PostgresImageStore(pool)
    where = Listing("a.shop", b"a" * 32)
    old = TODAY - timedelta(days=500)
    await store.upsert(where, _print(1), image_url="u1", day=old, source="seen")
    await store.upsert(where, _print(2), image_url="u2", day=old + timedelta(days=1), source="seen")
    row = await pool.fetchrow("SELECT phash, image_url, first_seen, last_seen FROM image_prints")
    assert tuple(row) == (2, "u2", old, old + timedelta(days=1))  # one row: an update, not a duplicate
    await store.upsert(Listing("amazon", b"s" * 32), _print(3), image_url="abo", day=old, source="abo", label="X")
    await store.purge(TODAY)
    assert [r["source"] for r in await pool.fetch("SELECT source FROM image_prints")] == ["abo"]


async def test_image_index_holds_no_page_addresses_people_or_times(pool: asyncpg.Pool) -> None:
    columns = await pool.fetch(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'image_prints'"
    )
    names = {c["column_name"] for c in columns}
    assert names == {
        "id", "source", "domain", "listing_key", "phash", "dhash", "embedding", "colour", "image_url", "label",
        "first_seen", "last_seen",
    }  # fmt: skip
    assert not [c for c in columns if "time" in c["data_type"]]


async def test_fast_path_lookup_is_well_under_200_ms(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    shops, verdicts = PostgresShopStore(pool), PostgresVerdictStore(pool)
    shop_store.use_store(shops)
    verdict_store.use_store(verdicts)
    monkeypatch.setattr(shop_store, "today", lambda: TODAY)
    try:
        fingerprint = b"f" * 32
        await shops.record(ShopRecord("glowlamp.shop", TODAY, fingerprint=fingerprint))
        verdict = Verdict(action="allow", risk_score=0.2, confidence=0.8, flags=[], policy_applied="balanced",
                          tier="quick", checked_at="2026-11-09T09:00:00Z")  # type: ignore[arg-type]  # fmt: skip
        key = listing_key("https://glowlamp.shop/products/lamp")
        assert key is not None
        await verdicts.put(key, "quick", Stored(TODAY, fingerprint, verdict.model_dump(mode="json")))
        timings = []
        for _ in range(50):
            start = time.perf_counter()
            served = await verdict_store.recall("https://glowlamp.shop/products/lamp", "quick", "strict")
            timings.append(time.perf_counter() - start)
            assert served is not None and served.source == "fingerprint"
        p50 = sorted(timings)[25]
        print(f"fast path p50 {p50 * 1000:.1f} ms")  # noqa: T201 - shown in the CI log
        assert p50 < 0.2
        row = await pool.fetchrow("SELECT * FROM listing_verdicts")
        assert set(row.keys()) == {"listing_key", "tier", "day", "shop_fingerprint", "verdict"}
        assert "glowlamp" not in row["verdict"]  # the answer holds no page address
    finally:
        shop_store.use_store(shop_store.MemoryShopStore())
        verdict_store.use_store(verdict_store.MemoryVerdictStore())


async def test_the_seed_imports_idempotently_and_can_be_removed(pool: asyncpg.Pool, tmp_path: Path) -> None:
    n = 500
    seed = tmp_path / "seed.npz"
    rng = np.random.default_rng(1)
    np.savez_compressed(
        seed,
        asin=np.array([f"B{i:09d}" for i in range(n)]),
        path=np.array([f"{i % 100:02x}/{i:08x}.jpg" for i in range(n)]),
        brand=np.array(["LumiCo" if i % 2 else "" for i in range(n)]),
        phash=rng.integers(-(2**63), 2**63 - 1, n, dtype=np.int64),
        dhash=rng.integers(-(2**63), 2**63 - 1, n, dtype=np.int64),
        emb=rng.integers(-127, 127, (n, 384), dtype=np.int8),
        colour=rng.integers(-127, 127, (n, 48), dtype=np.int8),
    )
    pinned = image_seed.sha256_of(seed)
    seen = PostgresImageStore(pool)
    await seen.upsert(Listing("a.shop", b"a" * 32), _print(1), image_url="u", day=TODAY, source="seen")

    assert await image_seed.import_seed(pool, seed, "abo", expected_sha256=pinned) == n
    assert await image_seed.import_seed(pool, seed, "abo") == n  # run again: nothing new
    row = await pool.fetchrow("SELECT source, domain, image_url, label FROM image_prints WHERE listing_key = $1",
                              image_seed.listing_key("B000000001"))  # fmt: skip
    assert tuple(row) == ("abo", "amazon", image_seed.SOURCES["abo"] + "01/00000001.jpg", "LumiCo")

    assert await image_seed.remove_seed(pool, "abo") == n
    assert [r["source"] for r in await pool.fetch("SELECT source FROM image_prints")] == ["seen"]  # analyses stay
