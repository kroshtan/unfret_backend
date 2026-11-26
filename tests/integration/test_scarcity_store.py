"""The stock-claim store against a real database: what a row holds, what counts, and the purge."""

from datetime import date, timedelta

import asyncpg

from unfret import observations
from unfret.facts import PageFacts
from unfret.scarcity_store import Claim, PostgresScarcityStore, assess, product_key

DAY = date(2026, 11, 26)
URL = "https://lamps.example/products/glow-lamp?variant=123"


async def test_a_row_holds_a_hash_a_day_and_a_count_only(pool: asyncpg.Pool) -> None:
    columns = {
        r["column_name"]: r["data_type"]
        for r in await pool.fetch(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'scarcity_claims'"
        )
    }
    assert columns == {
        "product_key": "bytea",
        "day": "date",
        "count": "smallint",
        "provenance": "smallint",
        "sources": "smallint",
    }


async def test_claims_count_once_trusted_or_corroborated_and_old_ones_go(pool: asyncpg.Pool) -> None:
    store = PostgresScarcityStore(pool)
    key = product_key(URL, PageFacts())
    assert key is not None
    for ago in (12, 6):
        await store.record(key, Claim(DAY - timedelta(days=ago), 3))  # pages Unfret fetched
    client = observations.Provenance(fetched=False, tier="quick").bits
    await store.record(key, Claim(DAY - timedelta(days=3), 9), bits=client, vouch=1)  # not corroborated
    await store.record(key, Claim(DAY - timedelta(days=12), 3), bits=client, vouch=1)  # adds to a trusted row

    earlier = await store.claims(key, DAY - timedelta(days=30))
    assert earlier == [Claim(DAY - timedelta(days=12), 3), Claim(DAY - timedelta(days=6), 3)]
    found = assess(earlier, Claim(DAY, 3))
    assert found is not None and found.pattern == "static"
    assert await pool.fetchval("SELECT COUNT(*) FROM scarcity_claims") == 3

    await store.purge(DAY - timedelta(days=7))
    assert await store.claims(key, DAY - timedelta(days=30)) == [Claim(DAY - timedelta(days=6), 3)]
    rows = "".join(r[0] for r in await pool.fetch("SELECT t::text FROM scarcity_claims t"))
    assert "lamps" not in rows and "glow" not in rows and b"lamps".hex() not in rows
