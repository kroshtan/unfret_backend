"""The product-page upgrades' stores against a real database: market prices, review fingerprints, hashed addresses."""

import hashlib
from datetime import date, timedelta

import asyncpg
import pytest

from unfret.market_prices import PostgresMarketStore
from unfret.review_store import PostgresReviewStore
from unfret.shop_store import PostgresShopStore, ShopRecord

TODAY = date(2026, 11, 15)


def key(name: str) -> bytes:
    return hashlib.sha256(name.encode()).digest()


async def test_market_prices_per_other_shop(pool: asyncpg.Pool) -> None:
    store = PostgresMarketStore(pool)
    product, mine = key("gtin:1"), key("shop:mine.shop")
    await store.record(product, mine, TODAY, 10.0, "EUR")
    await store.record(product, key("shop:a.shop"), TODAY - timedelta(days=2), 100.0, "EUR")
    await store.record(product, key("shop:a.shop"), TODAY - timedelta(days=1), 120.0, "EUR")
    await store.record(product, key("shop:a.shop"), TODAY - timedelta(days=1), 999.0, "EUR")  # the day's second
    await store.record(product, key("shop:b.shop"), TODAY, 130.0, "EUR")
    await store.record(product, key("shop:c.shop"), TODAY, 140.0, "USD")
    await store.record(product, key("shop:d.shop"), TODAY - timedelta(days=100), 50.0, "EUR")
    prices = await store.others(product, mine, TODAY - timedelta(days=90), "EUR")
    assert sorted(prices) == [110.0, 130.0]  # a.shop's median, b.shop; not mine, USD or too old
    await store.purge(TODAY - timedelta(days=90))
    assert await pool.fetchval("SELECT COUNT(*) FROM market_prices") == 5


async def test_review_fingerprints_count_other_shops(pool: asyncpg.Pool) -> None:
    store = PostgresReviewStore(pool)
    reviews = {key(f"review:{i}") for i in range(4)}
    await store.record(key("shop:a.shop"), set(list(reviews)[:2]), TODAY - timedelta(days=100))
    await store.record(key("shop:b.shop"), reviews, TODAY)
    assert await store.matches(key("shop:new.shop"), reviews) == (4, 2)
    assert await store.matches(key("shop:b.shop"), reviews) == (2, 1)
    await store.purge(TODAY - timedelta(days=90))
    assert await store.matches(key("shop:new.shop"), reviews) == (4, 1)
    columns = {r["column_name"] for r in await pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'review_fingerprints'"
    )}  # fmt: skip
    # provenance and sources: where it was seen and how many sources reported it, never which (unfret.observations)
    assert columns == {"review_hash", "shop_key", "first_seen", "last_seen", "provenance", "sources"}
    with pytest.raises(asyncpg.CheckViolationError):
        await store.record(b"short", reviews, TODAY)


async def test_addresses_are_hashed_counted_and_never_link(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    address = "a" * 64
    for i in range(3):
        await store.record(ShopRecord(f"s{i}.shop", TODAY, links={"address": {address}}))
    await store.record(ShopRecord("s9.shop", TODAY, links={"address": {address}, "ga": {"G-LINKED01"}}))
    await store.record(ShopRecord("me.shop", TODAY, links={"address": {address}, "ga": {"G-LINKED01"}}))
    assert [x.domain for x in await store.linked("me.shop", {"address": {address}})] == ["s9.shop"]
    assert await store.address_shops("me.shop", {address}) == {"s0.shop", "s1.shop", "s2.shop", "s9.shop"}
    with pytest.raises(asyncpg.CheckViolationError):
        await store.record(ShopRecord("z.shop", TODAY, links={"address": {"Herengracht 182"}}))
    await store.purge(TODAY + timedelta(days=181))
    assert await pool.fetchval("SELECT COUNT(*) FROM shop_links WHERE kind = 'address'") == 0
