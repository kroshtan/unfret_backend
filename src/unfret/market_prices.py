"""
What other shops charge for the same product: anonymous price observations keyed by barcode, across shops.

The price store (:mod:`unfret.price_store`) keeps one shop's price history of one product; this store compares
shops. A product far below what every other shop asks is a classic sign of a fake shop (counterfeits, or nothing
delivered at all), and a barcode (GTIN/EAN) names the same product on every shop.

What is stored, and nothing else: a SHA-256 of the barcode, a SHA-256 of the shop's registrable domain (marketplaces
as one shop each), the day, the price and its currency. One row per product per shop per day. No URL, caller,
wallet, IP address or time of day. Rows older than 90 days are deleted. Pages without a valid barcode aren't
recorded. Off in evaluation runs (archived pages aren't today's prices), with the price store.

Each row also keeps where it was seen and how many independent sources reported that price
(:mod:`unfret.observations`). Only rows seen on a page Unfret fetched itself, or corroborated, make the market: a
forged page (any shop's address, any price) can't make another shop look far below it.
"""

import hashlib
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import asyncpg
import structlog

from unfret import observations
from unfret.facts import PageFacts
from unfret.observations import Kept
from unfret.price_store import gtins, registrable_domain, valid_gtin
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

RETENTION_DAYS = 90
WINDOW_DAYS = 90  # observations of other shops this recent count
_MARKETPLACES = frozenset({"amazon", "bol.com", "walmart", "ebay"})


def barcode(facts: PageFacts) -> str | None:
    """
    The product's barcode: a valid GTIN from the page's product IDs or its Product JSON-LD, as 14 digits.

    :param facts: The page facts.
    :return: The GTIN, or ``None`` when the page shows none with a correct check digit.
    """
    for key in ("gtin14", "gtin13", "gtin", "gtin12", "gtin8"):
        value = facts.product_ids.get(key, "").strip().replace(" ", "")
        if valid_gtin(value):
            return value.zfill(14)
    found = gtins(facts.json_ld)
    return found[0] if found else None


def product_key(gtin: str) -> bytes:
    """
    The stored key of a product: SHA-256 of its barcode.

    :param gtin: 14 digits.
    :return: 32 bytes.
    """
    return hashlib.sha256(f"gtin:{gtin}".encode()).digest()


def shop_key(url: str, facts: PageFacts) -> bytes | None:
    """
    The stored key of a shop: SHA-256 of its registrable domain, or of the marketplace's name.

    :param url: The page URL.
    :param facts: The page facts (the platform).
    :return: 32 bytes, or ``None`` without a host.
    """
    if facts.platform in _MARKETPLACES:
        return hashlib.sha256(f"platform:{facts.platform}".encode()).digest()
    host = urlsplit(url).hostname or ""
    if not host or "." not in host:
        return None
    return hashlib.sha256(f"shop:{registrable_domain(host)}".encode()).digest()


@dataclass(frozen=True)
class Market:
    """What other shops charged for a product in the window (today included), in one currency."""

    shops: int  # other shops with a price
    median: float | None  # the median over those shops of each shop's median price


class MarketStore(Protocol):
    """Where observations live: Postgres in production, memory in tests."""

    async def record(
        self,
        product: bytes,
        shop: bytes,
        day: date,
        price: float,
        currency: str,
        *,
        bits: int = observations.DEFAULT_BITS,
        vouch: int = 0,
    ) -> None:
        """
        Keep a shop's first price of a product that day, merged by :func:`unfret.observations.merge`.

        :param product: From :func:`product_key`.
        :param shop: From :func:`shop_key`.
        :param day: The day.
        :param price: The price.
        :param currency: Its currency.
        :param bits: Its provenance bits (:mod:`unfret.observations`).
        :param vouch: 1 when a new source reports it.
        """

    async def others(self, product: bytes, shop: bytes, since: date, currency: str) -> list[float]:
        """
        Each other shop's median price of the product since a day.

        :param product: The product key.
        :param shop: This shop (left out).
        :param since: The first day that counts.
        :param currency: Only prices in this currency.
        :return: One price per other shop (rows that count only, :func:`unfret.observations.counts`).
        """

    async def purge(self, before: date) -> None:
        """
        Delete observations from before a day.

        :param before: The first day to keep.
        """


class MemoryMarketStore:
    """In-process store, for tests and runs without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[tuple[bytes, bytes, date], tuple[float, str]] = {}
        self.kept: dict[tuple[bytes, bytes, date], Kept] = {}  # each row's provenance (rows without one count)

    async def record(
        self,
        product: bytes,
        shop: bytes,
        day: date,
        price: float,
        currency: str,
        *,
        bits: int = observations.DEFAULT_BITS,
        vouch: int = 0,
    ) -> None:
        """
        Keep the day's first price, merged as the Postgres store does.

        :param product: The product key.
        :param shop: The shop key.
        :param day: The day.
        :param price: The price.
        :param currency: The currency.
        :param bits: Its provenance bits.
        :param vouch: 1 for a new source.
        """
        row = (product, shop, day)
        old = self.rows.get(row)
        before = (self.kept.get(row) or Kept(observations.LEGACY, 0)) if old is not None else None
        action, kept = observations.merge(before, bits, vouch, same=old == (price, currency), newer_wins=False)
        if action in {"insert", "replace"}:
            self.rows[row] = (price, currency)
        self.kept[row] = kept

    def _counts(self, row: tuple[bytes, bytes, date]) -> bool:
        kept = self.kept.get(row)
        return kept is None or observations.counts(kept.bits, kept.sources)

    async def others(self, product: bytes, shop: bytes, since: date, currency: str) -> list[float]:
        """
        Other shops' median prices.

        :param product: The product key.
        :param shop: This shop.
        :param since: The first day.
        :param currency: The currency.
        :return: One price per other shop.
        """
        per_shop: dict[bytes, list[float]] = {}
        for (p, s, day), (price, cur) in self.rows.items():
            if p == product and s != shop and day >= since and cur == currency and self._counts((p, s, day)):
                per_shop.setdefault(s, []).append(price)
        return [statistics.median(prices) for prices in per_shop.values()]

    async def purge(self, before: date) -> None:
        """
        Drop old observations.

        :param before: The first day to keep.
        """
        self.rows = {k: v for k, v in self.rows.items() if k[2] >= before}
        self.kept = {k: v for k, v in self.kept.items() if k in self.rows}


class PostgresMarketStore:
    """Observations in the ``market_prices`` table (migration 18)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def record(
        self,
        product: bytes,
        shop: bytes,
        day: date,
        price: float,
        currency: str,
        *,
        bits: int = observations.DEFAULT_BITS,
        vouch: int = 0,
    ) -> None:
        """
        Insert the day's first price; a later one that day is merged (:func:`unfret.observations.merge`).

        :param product: The product key.
        :param shop: The shop key.
        :param day: The day.
        :param price: The price.
        :param currency: The currency.
        :param bits: Its provenance bits.
        :param vouch: 1 for a new source.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchval(
                "INSERT INTO market_prices (product_key, shop_key, day, price, currency, provenance, sources) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (product_key, shop_key, day) DO NOTHING "
                "RETURNING TRUE",
                product,
                shop,
                day,
                price,
                currency,
                bits,
                vouch,
            )
            if inserted:
                return
            row: Any = await conn.fetchrow(
                "SELECT price, currency, provenance, sources FROM market_prices "
                "WHERE product_key = $1 AND shop_key = $2 AND day = $3 FOR UPDATE",
                product,
                shop,
                day,
            )
            same = (float(row["price"]), row["currency"]) == (price, currency)
            action, kept = observations.merge(
                Kept(row["provenance"], row["sources"]), bits, vouch, same=same, newer_wins=False
            )
            await conn.execute(
                "UPDATE market_prices SET provenance = $4, sources = $5,"
                " price = CASE WHEN $6 THEN $7 ELSE price END, currency = CASE WHEN $6 THEN $8 ELSE currency END"
                " WHERE product_key = $1 AND shop_key = $2 AND day = $3",
                product,
                shop,
                day,
                kept.bits,
                kept.sources,
                action == "replace",
                price,
                currency,
            )

    async def others(self, product: bytes, shop: bytes, since: date, currency: str) -> list[float]:
        """
        Other shops' median prices.

        :param product: The product key.
        :param shop: This shop.
        :param since: The first day.
        :param currency: The currency.
        :return: One price per other shop.
        """
        rows: list[Any] = await self.pool.fetch(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price) AS price FROM market_prices "
            "WHERE product_key = $1 AND shop_key <> $2 AND day >= $3 AND currency = $4"
            " AND (provenance & $5 <> 0 OR sources >= $6) GROUP BY shop_key",
            product,
            shop,
            since,
            currency,
            *observations.trusted_params(),
        )
        return [float(r["price"]) for r in rows]

    async def purge(self, before: date) -> None:
        """
        Delete old observations.

        :param before: The first day to keep.
        """
        await self.pool.execute("DELETE FROM market_prices WHERE day < $1", before)


_store: MarketStore = MemoryMarketStore()
_purged_on: date | None = None


def use_store(store: MarketStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresMarketStore` at startup).

    :param store: The store.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


# The shop key of a peer-to-peer ad: its own price is never recorded, so it excludes nobody.
_AD_SHOP = hashlib.sha256(b"ad:peer-to-peer").digest()


async def for_ad(facts: PageFacts) -> Market | None:
    """
    What shops charge for the product a peer-to-peer ad names by its barcode, without recording the ad. Never raises.

    :param facts: The ad's facts.
    :return: The market (every shop counts), or ``None`` without a barcode or price, or when the store failed.
    """
    gtin = barcode(facts)
    if gtin is None or not facts.price:
        return None
    since = today() - timedelta(days=WINDOW_DAYS)
    try:
        prices = await _store.others(product_key(gtin), _AD_SHOP, since, (facts.currency or "").upper()[:3])
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("market_store_failed", error=type(exc).__name__)
        return None
    return Market(len(prices), statistics.median(prices) if prices else None)


def today() -> date:
    """
    Today in UTC, the only time stored.

    :return: The date.
    """
    return datetime.now(tz=UTC).date()


async def observe(url: str, facts: PageFacts) -> Market | None:
    """
    Compare the page's price with other shops', then record it (as :func:`record`). Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: The market, or ``None`` for a page without a price, a barcode or a host (or a failing store), and for
        the test shop (:mod:`unfret.test_shop`), which is never recorded.
    """
    gtin, shop = barcode(facts), shop_key(url, facts)
    if gtin is None or shop is None or not facts.price or is_test_shop(url):
        return None
    product, day, currency = product_key(gtin), today(), (facts.currency or "").upper()[:3]
    try:
        prices = await _store.others(product, shop, day - timedelta(days=WINDOW_DAYS), currency)
        await _record(product, shop, day, round(facts.price, 2), currency)
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("market_store_failed", error=type(exc).__name__)
        return None
    return Market(len(prices), statistics.median(prices) if prices else None)


async def _record(product: bytes, shop: bytes, day: date, price: float, currency: str) -> None:
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    provenance = observations.current()
    if not provenance.record:
        return
    vouch = observations.vouch(b"market|" + product + shop + day.isoformat().encode())
    await _store.record(product, shop, day, price, currency, bits=provenance.bits, vouch=vouch)
    if _purged_on != day:
        _purged_on = day
        await _store.purge(day - timedelta(days=RETENTION_DAYS))


async def record(url: str, facts: PageFacts) -> bool:
    """
    Record the page's price for the market without looking anything up (a quick check, after its answer).

    The same row :func:`observe` writes, with the running analysis's provenance. Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: Whether a price was recorded (not without a price, a barcode or a host, nor for the test shop).
    """
    gtin, shop = barcode(facts), shop_key(url, facts)
    if gtin is None or shop is None or not facts.price or is_test_shop(url):
        return False
    try:
        await _record(product_key(gtin), shop, today(), round(facts.price, 2), (facts.currency or "").upper()[:3])
    except Exception as exc:  # noqa: BLE001 - optional, after the answer
        logger.warning("market_store_failed", error=type(exc).__name__)
        return False
    return True
