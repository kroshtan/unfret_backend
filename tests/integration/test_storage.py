"""Migrations, the Postgres fact cache, price and shop stores against a real database."""

from datetime import date, timedelta

import asyncpg
import pytest
from pydantic import SecretStr

from veridex import fact_cache, shop_store
from veridex.config import settings
from veridex.db import PostgresFactStore
from veridex.facts import PageFacts
from veridex.migrations import MIGRATIONS, migrate
from veridex.price_store import History, Observation, PostgresPriceStore
from veridex.shop_store import MAX_SHARE, PostgresShopStore, ShopRecord


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
