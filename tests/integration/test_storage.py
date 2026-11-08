"""Migrations, the Postgres fact cache and the usage budget against a real database."""

import asyncpg

from veridex import fact_cache
from veridex.db import PostgresBudget, PostgresFactStore
from veridex.migrations import MIGRATIONS, migrate


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
    budget = PostgresBudget(pool, "tineye")
    assert [await budget.reserve("2026-11", 2) for _ in range(3)] == [True, True, False]
    assert await budget.reserve("2026-12", 2) is True
    assert await pool.fetchval("SELECT count FROM usage_counters WHERE name = 'tineye' AND period = '2026-11'") == 2
