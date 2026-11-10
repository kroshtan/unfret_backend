"""
Anonymous price observations per product, to tell real discounts from invented "was" prices.

EU law (the Omnibus directive) says an announced "was" price must be the lowest price of the 30 days before the
reduction. A shop can't be asked for its price history, but every analysed product page shows today's price: kept
for 90 days, those observations show whether the "was" price was ever charged.

What is stored, and nothing else: a SHA-256 of the shop's registrable domain and the product's public identifier
(ASIN, bol.com ID or EAN/GTIN), the day, the price, its currency and the claimed "was" price. One row per product per
day. No URL, no caller, no wallet, no IP address, no time of day. Rows older than 90 days are deleted. Pages
without a public product identifier aren't recorded.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import asyncpg
import structlog

from unfret.facts import PageFacts

logger = structlog.get_logger("unfret")

RETENTION_DAYS = 90
HISTORY_WINDOW_DAYS = 30  # the Omnibus reference period

# Public suffixes with two labels that sell online a lot; without a full public-suffix list, these keep
# shop.example.co.uk and example.co.uk the same shop.
_TWO_LABEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "ne.jp",
        "com.br", "com.mx", "com.ar", "com.tr", "com.cn", "com.hk", "com.sg", "com.tw", "co.za", "co.in", "co.kr",
        "com.pl", "com.es", "com.pt", "co.il", "com.my", "com.ph", "co.id", "com.vn", "com.ua", "com.ru",
    }
)  # fmt: skip
_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
_BOL_RE = re.compile(r"/p/[^/]+/(\d{10,20})(?:/|$)")
_WALMART_RE = re.compile(r"/ip/(?:[^/]+/)?(\d{6,15})(?:[/?]|$)")
_GTIN_KEYS = ("gtin14", "gtin13", "gtin", "gtin12", "gtin8", "ean")


def registrable_domain(host: str) -> str:
    """
    The registrable domain of a hostname: ``www.shop.example.co.uk`` → ``example.co.uk``.

    :param host: A hostname.
    :return: The registrable domain (the host itself when it has two labels or fewer).
    """
    labels = host.lower().rstrip(".").removeprefix("www.").split(".")
    take = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-take:])


def valid_gtin(digits: str) -> bool:
    """
    Whether a string is a GTIN-8/12/13/14 with a correct check digit.

    :param digits: The candidate.
    :return: ``True`` for a valid GTIN.
    """
    if not digits.isdigit() or len(digits) not in {8, 12, 13, 14}:
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _gtins(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key in _GTIN_KEYS:
            value = str(node.get(key) or "").strip().replace(" ", "")
            if valid_gtin(value):
                found.append(value.zfill(14))
        found += _gtins(node.get("offers"))
    elif isinstance(node, list):
        for item in node[:10]:
            found += _gtins(item)
    return found


def product_id(url: str, facts: PageFacts) -> str | None:
    """
    The product's public identifier: the ASIN on Amazon, the product ID on bol.com or Walmart, else a GTIN/EAN.

    :param url: The page URL.
    :param facts: The page facts (their Product JSON-LD carries the GTIN).
    :return: ``asin:…``, ``bol:…``, ``walmart:…`` or ``gtin:<14 digits>``, or ``None`` when the page shows none.
    """
    path = urlsplit(url).path
    if facts.platform == "amazon" and (m := _ASIN_RE.search(path)):
        return f"asin:{m.group(1).upper()}"
    if facts.platform == "bol.com" and (m := _BOL_RE.search(path)):
        return f"bol:{m.group(1)}"
    if facts.platform == "walmart" and (m := _WALMART_RE.search(path)):
        return f"walmart:{m.group(1)}"
    gtins = _gtins(facts.json_ld)
    return f"gtin:{gtins[0]}" if gtins else None


def product_key(url: str, facts: PageFacts) -> bytes | None:
    """
    The stored key for a product page: SHA-256 of the registrable domain and the public product ID.

    :param url: The page URL.
    :param facts: The page facts.
    :return: 32 bytes, or ``None`` without a host or a public product ID.
    """
    host = urlsplit(url).hostname or ""
    pid = product_id(url, facts)
    if not host or pid is None:
        return None
    return hashlib.sha256(f"{registrable_domain(host)}|{pid}".encode()).digest()


@dataclass(frozen=True)
class Observation:
    """One product's price on one day."""

    day: date
    price: float
    currency: str
    was_price: float | None


@dataclass(frozen=True)
class History:
    """What the store saw for a product over the reference period (today excluded)."""

    first_seen: date | None
    days: int  # distinct days observed in the reference period
    lowest: float | None  # lowest price in the reference period (same currency as today's)

    def span_days(self, today: date) -> int:
        """
        How long the product has been observed.

        :param today: Today.
        :return: Days since the first observation (0 without one).
        """
        return (today - self.first_seen).days if self.first_seen else 0


class PriceStore(Protocol):
    """Where observations live: Postgres in production, memory in tests."""

    async def record(self, key: bytes, observation: Observation) -> None:
        """
        Keep the day's first observation of a product (later ones that day are dropped).

        :param key: From :func:`product_key`.
        :param observation: The observation.
        """

    async def history(self, key: bytes, today: date, currency: str) -> History:
        """
        The product's observations in the reference period before ``today``.

        :param key: From :func:`product_key`.
        :param today: Today (excluded).
        :param currency: Only prices in this currency count.
        :return: The history.
        """

    async def purge(self, before: date) -> None:
        """
        Delete observations from before a day.

        :param before: The first day to keep.
        """


class MemoryPriceStore:
    """In-process store, for tests and runs without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[tuple[bytes, date], Observation] = {}

    async def record(self, key: bytes, observation: Observation) -> None:
        """
        Keep the day's first observation.

        :param key: The product key.
        :param observation: The observation.
        """
        self.rows.setdefault((key, observation.day), observation)

    async def history(self, key: bytes, today: date, currency: str) -> History:
        """
        The observations before today.

        :param key: The product key.
        :param today: Today (excluded).
        :param currency: The currency to compare in.
        :return: The history.
        """
        start = today - timedelta(days=HISTORY_WINDOW_DAYS)
        mine = [o for (k, _), o in self.rows.items() if k == key and o.day < today]
        window = [o for o in mine if o.day >= start and o.currency == currency]
        return History(
            first_seen=min((o.day for o in mine), default=None),
            days=len({o.day for o in window}),
            lowest=min((o.price for o in window), default=None),
        )

    async def purge(self, before: date) -> None:
        """
        Drop old observations.

        :param before: The first day to keep.
        """
        self.rows = {k: o for k, o in self.rows.items() if o.day >= before}


class PostgresPriceStore:
    """Observations in the ``price_observations`` table (migration 6)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def record(self, key: bytes, observation: Observation) -> None:
        """
        Insert the day's first observation; later ones that day are dropped.

        :param key: The product key.
        :param observation: The observation.
        """
        await self.pool.execute(
            "INSERT INTO price_observations (product_key, day, price, currency, was_price) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (product_key, day) DO NOTHING",
            key,
            observation.day,
            observation.price,
            observation.currency,
            observation.was_price,
        )

    async def history(self, key: bytes, today: date, currency: str) -> History:
        """
        The observations before today.

        :param key: The product key.
        :param today: Today (excluded).
        :param currency: The currency to compare in.
        :return: The history.
        """
        row: Any = await self.pool.fetchrow(
            "SELECT MIN(day) AS first_seen, "
            "COUNT(*) FILTER (WHERE day >= $3 AND currency = $4) AS days, "
            "MIN(price) FILTER (WHERE day >= $3 AND currency = $4) AS lowest "
            "FROM price_observations WHERE product_key = $1 AND day < $2",
            key,
            today,
            today - timedelta(days=HISTORY_WINDOW_DAYS),
            currency,
        )
        return History(
            first_seen=row["first_seen"],
            days=int(row["days"] or 0),
            lowest=float(row["lowest"]) if row["lowest"] is not None else None,
        )

    async def purge(self, before: date) -> None:
        """
        Delete old observations.

        :param before: The first day to keep.
        """
        await self.pool.execute("DELETE FROM price_observations WHERE day < $1", before)


_store: PriceStore = MemoryPriceStore()
_purged_on: date | None = None


def use_store(store: PriceStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresPriceStore` at startup).

    :param store: The store.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


def today() -> date:
    """
    Today in UTC, the only time stored with an observation.

    :return: The date.
    """
    return datetime.now(tz=UTC).date()


async def observe(url: str, facts: PageFacts) -> tuple[bytes, History] | None:
    """
    Record today's price of a product page and return its history (before today).

    Purges expired rows at most once a day per process. Never raises: a failing store costs the price history,
    not the analysis.

    :param url: The page URL.
    :param facts: The page facts.
    :return: The product key and its history, or ``None`` for pages without a price or a public product ID.
    """
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    key = product_key(url, facts)
    if key is None or facts.price is None:
        return None
    day = today()
    currency = (facts.currency or "").upper()[:3]
    try:
        history = await _store.history(key, day, currency)
        await _store.record(key, Observation(day, round(facts.price, 2), currency, facts.list_price))
        if _purged_on != day:
            _purged_on = day
            await _store.purge(day - timedelta(days=RETENTION_DAYS))
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("price_store_failed", error=type(exc).__name__)
        return None
    return key, history
