"""Migrations, the Postgres fact cache and the usage budget against a real database."""

from datetime import date, timedelta

import asyncpg

from veridex import fact_cache
from veridex.db import PostgresBudget, PostgresFactStore
from veridex.migrations import MIGRATIONS, migrate
from veridex.price_store import History, Observation, PostgresPriceStore


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


async def test_postgres_budget_caps_per_period(pool: asyncpg.Pool) -> None:
    budget = PostgresBudget(pool, "vision")
    assert [await budget.reserve("2026-11", 2) for _ in range(3)] == [True, True, False]
    assert await budget.reserve("2026-12", 2) is True
    assert await pool.fetchval("SELECT count FROM usage_counters WHERE name = 'vision' AND period = '2026-11'") == 2


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
