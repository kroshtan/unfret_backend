"""
The shop fingerprint store's provenance and corroboration against a real database (:mod:`unfret.shop_store`).

Identifiers kept from before (migration 32) and seen on pages Unfret fetched count at once; client-supplied ones only
once corroborated, and they never change a shop's facts. The shops here are made up.
"""

from datetime import date, timedelta

import asyncpg
import pytest

from tests.integration.conftest import DROP_ALL
from unfret import observations
from unfret.config import settings
from unfret.migrations import MIGRATIONS, migrate
from unfret.observations import CLIENT, DEEP, FETCHED, LEGACY, QUICK
from unfret.shop_store import PostgresShopStore, ShopRecord

DAY = date(2026, 11, 18)
PIXEL = {"meta_pixel": {"7712309876541"}}
CLIENT_DEEP = CLIENT | DEEP


@pytest.fixture(autouse=True)
def corroboration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "observation_min_sources", 3)


def rec(
    domain: str,
    bits: int = observations.DEFAULT_BITS,
    *,
    vouch: bool = False,
    links: dict[str, set[str]] | None = None,
) -> ShopRecord:
    links = links or {}
    vouched = frozenset((k, v) for k, values in links.items() for v in values) if vouch else frozenset()
    return ShopRecord(domain, DAY, platform="shopify", links=links, fingerprint=bytes(32), bits=bits, vouched=vouched)


async def domains(store: PostgresShopStore, domain: str, current: dict[str, set[str]]) -> list[str]:
    return [link.domain for link in await store.linked(domain, current)]


async def test_identifiers_kept_from_before_count_as_before(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 32])
    await pool.execute(
        "INSERT INTO shops (domain, first_seen, last_seen, platform) VALUES ('old.shop', $1, $1, 'x')", DAY
    )
    await pool.execute(
        "INSERT INTO shop_links (kind, value, domain, first_seen, last_seen) "
        "VALUES ('meta_pixel', '7712309876541', 'old.shop', $1, $1)",
        DAY,
    )
    assert 32 in await migrate(pool)
    assert tuple(await pool.fetchrow("SELECT provenance, sources FROM shop_links")) == (LEGACY, 0)
    assert await domains(PostgresShopStore(pool), "lantern-house.shop", PIXEL) == ["old.shop"]


async def test_client_identifiers_count_once_corroborated_or_fetched(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    await store.record(rec("young-scam.shop", CLIENT_DEEP, vouch=True, links=PIXEL))
    await store.record(rec("young-scam.shop", CLIENT | QUICK, vouch=False, links=PIXEL))  # a source seen today already
    assert await domains(store, "lantern-house.shop", PIXEL) == []
    await store.record(rec("young-scam.shop", CLIENT_DEEP, vouch=True, links=PIXEL))
    assert await domains(store, "lantern-house.shop", PIXEL) == []
    await store.record(rec("young-scam.shop", CLIENT_DEEP, vouch=True, links=PIXEL))
    row = await pool.fetchrow("SELECT provenance, sources FROM shop_links WHERE domain = 'young-scam.shop'")
    assert tuple(row) == (CLIENT_DEEP | QUICK, 3)
    assert await domains(store, "lantern-house.shop", PIXEL) == ["young-scam.shop"]

    await store.record(rec("sister.shop", FETCHED | QUICK, links=PIXEL))  # a page Unfret fetched: at once
    assert set(await domains(store, "lantern-house.shop", PIXEL)) == {"young-scam.shop", "sister.shop"}


async def test_a_planted_identifier_an_address_or_a_forged_agency_id_counts_for_nothing(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    other = {"meta_pixel": {"5550001112223"}}
    await store.record(rec("young-scam.shop", links=other))
    await store.record(rec("lantern-house.shop", CLIENT_DEEP, vouch=True, links=other))  # planted on lantern-house
    assert await domains(store, "lantern-house.shop", {}) == []

    address = {"a" * 64}
    for i in range(3):
        await store.record(rec(f"fake-{i}.shop", CLIENT_DEEP, vouch=True, links={"address": address}))
    assert await store.address_shops("lantern-house.shop", address) == set()
    await store.record(rec("real-neighbour.shop", links={"address": address}))
    assert await store.address_shops("lantern-house.shop", address) == {"real-neighbour.shop"}

    await store.record(rec("sister.shop", links=PIXEL))
    for i in range(30):  # more than MAX_SHARE forged holders don't turn the pixel into an agency's
        await store.record(rec(f"agency-{i}.shop", CLIENT_DEEP, vouch=True, links=PIXEL))
    assert await domains(store, "lantern-house.shop", PIXEL) == ["sister.shop"]


async def test_only_a_page_unfret_fetched_changes_a_shops_facts(pool: asyncpg.Pool) -> None:
    store = PostgresShopStore(pool)
    await store.record(rec("lantern-house.shop", links=PIXEL))
    forged = ShopRecord("lantern-house.shop", DAY, platform="woocommerce", apps=("x",), fingerprint=b"f" * 32, bits=2)
    await store.record(forged)
    assert await store.fingerprint("lantern-house.shop") == bytes(32)
    assert await pool.fetchval("SELECT platform FROM shops WHERE domain = 'lantern-house.shop'") == "shopify"
    await store.record(ShopRecord("new.shop", DAY, platform="woocommerce", fingerprint=b"f" * 32, bits=CLIENT_DEEP))
    row = await pool.fetchrow("SELECT platform, apps, fingerprint FROM shops WHERE domain = 'new.shop'")
    assert (row["platform"], row["apps"], row["fingerprint"]) == ("generic", [], None)


async def test_first_fetched_is_set_by_fetched_pages_only(pool: asyncpg.Pool) -> None:
    # Unfret's own "seen since" (domain_age): the first day a page Unfret fetched itself showed the shop.
    store = PostgresShopStore(pool)
    client = observations.Provenance(fetched=False, tier="quick").bits
    await store.record(ShopRecord("forged.shop", DAY, bits=client))
    assert await store.first_fetched("forged.shop") is None
    await store.record(ShopRecord("forged.shop", DAY + timedelta(days=5)))
    await store.record(ShopRecord("forged.shop", DAY + timedelta(days=9)))
    assert await store.first_fetched("forged.shop") == DAY + timedelta(days=5)
    # Past the write limit: the day only, for a new shop and for one already there.
    await store.seen("quiet.shop", DAY)
    await store.seen("forged.shop", DAY + timedelta(days=20))
    assert await store.first_fetched("quiet.shop") == DAY
    assert await store.first_fetched("forged.shop") == DAY + timedelta(days=5)
    row = await pool.fetchrow("SELECT last_seen, platform, fingerprint FROM shops WHERE domain = 'quiet.shop'")
    assert tuple(row) == (DAY, "generic", None)


async def test_first_fetched_starts_from_first_seen_for_fetched_shops(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 35])
    await pool.execute(
        "INSERT INTO shops (domain, first_seen, last_seen, platform) VALUES ('kept.shop', $1, $1, 'shopify'),"
        " ('client.shop', $1, $1, 'generic')",
        DAY,
    )
    assert 35 in await migrate(pool)
    store = PostgresShopStore(pool)
    assert await store.first_fetched("kept.shop") == DAY
    assert await store.first_fetched("client.shop") is None
