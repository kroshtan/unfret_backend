"""
What other shops charge for the same product: anonymous price observations keyed by barcode, across shops.

The price store (:mod:`unfret.price_store`) keeps one shop's price history of one product; this store compares
shops. A product far below what every other shop asks is a classic sign of a fake shop (counterfeits, or nothing
delivered at all), and a barcode (GTIN/EAN) names the same product on every shop.

What is stored, and nothing else: a SHA-256 of the barcode, a SHA-256 of the shop's registrable domain (marketplaces
as one shop each), the day, the price and its currency. One row per product per shop per day. No URL, caller,
wallet, IP address or time of day. Rows older than 90 days are deleted. Pages without a valid barcode aren't
recorded. Off in evaluation runs (archived pages aren't today's prices), with the price store.
"""

import hashlib
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import asyncpg
import structlog

from unfret.facts import PageFacts
from unfret.price_store import gtins, registrable_domain, valid_gtin

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

    async def record(self, product: bytes, shop: bytes, day: date, price: float, currency: str) -> None:
        """
        Keep a shop's first price of a product that day.

        :param product: From :func:`product_key`.
        :param shop: From :func:`shop_key`.
        :param day: The day.
        :param price: The price.
        :param currency: Its currency.
        """

    async def others(self, product: bytes, shop: bytes, since: date, currency: str) -> list[float]:
        """
        Each other shop's median price of the product since a day.

        :param product: The product key.
        :param shop: This shop (left out).
        :param since: The first day that counts.
        :param currency: Only prices in this currency.
        :return: One price per other shop.
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

    async def record(self, product: bytes, shop: bytes, day: date, price: float, currency: str) -> None:
        """
        Keep the day's first price.

        :param product: The product key.
        :param shop: The shop key.
        :param day: The day.
        :param price: The price.
        :param currency: The currency.
        """
        self.rows.setdefault((product, shop, day), (price, currency))

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
            if p == product and s != shop and day >= since and cur == currency:
                per_shop.setdefault(s, []).append(price)
        return [statistics.median(prices) for prices in per_shop.values()]

    async def purge(self, before: date) -> None:
        """
        Drop old observations.

        :param before: The first day to keep.
        """
        self.rows = {k: v for k, v in self.rows.items() if k[2] >= before}


class PostgresMarketStore:
    """Observations in the ``market_prices`` table (migration 18)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def record(self, product: bytes, shop: bytes, day: date, price: float, currency: str) -> None:
        """
        Insert the day's first price; later ones that day are dropped.

        :param product: The product key.
        :param shop: The shop key.
        :param day: The day.
        :param price: The price.
        :param currency: The currency.
        """
        await self.pool.execute(
            "INSERT INTO market_prices (product_key, shop_key, day, price, currency) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (product_key, shop_key, day) DO NOTHING",
            product,
            shop,
            day,
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
            "WHERE product_key = $1 AND shop_key <> $2 AND day >= $3 AND currency = $4 GROUP BY shop_key",
            product,
            shop,
            since,
            currency,
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


def today() -> date:
    """
    Today in UTC, the only time stored.

    :return: The date.
    """
    return datetime.now(tz=UTC).date()


async def observe(url: str, facts: PageFacts) -> Market | None:
    """
    Compare the page's price with other shops', then record it. Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: The market, or ``None`` for a page without a price, a barcode or a host (or a failing store).
    """
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    gtin, shop = barcode(facts), shop_key(url, facts)
    if gtin is None or shop is None or not facts.price:
        return None
    product, day, currency = product_key(gtin), today(), (facts.currency or "").upper()[:3]
    try:
        prices = await _store.others(product, shop, day - timedelta(days=WINDOW_DAYS), currency)
        await _store.record(product, shop, day, round(facts.price, 2), currency)
        if _purged_on != day:
            _purged_on = day
            await _store.purge(day - timedelta(days=RETENTION_DAYS))
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("market_store_failed", error=type(exc).__name__)
        return None
    return Market(len(prices), statistics.median(prices) if prices else None)
