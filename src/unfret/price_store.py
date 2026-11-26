"""
Anonymous price observations per product, to tell real discounts from invented "was" prices.

EU law (the Omnibus directive) says an announced "was" price must be the lowest price of the 30 days before the
reduction. A shop can't be asked for its price history, but every analysed product page shows today's price: kept
for 90 days, those observations show whether the "was" price was ever charged.

What is stored, and nothing else: a SHA-256 of the shop's registrable domain and the product's public identifier
(ASIN, bol.com ID or EAN/GTIN), the day, the price, its currency and the claimed "was" price. One row per product per
day. No URL, no caller, no wallet, no IP address, no time of day. Rows older than 90 days are deleted. A product page
without a public product identifier is keyed by its page instead (:func:`listing_key`: the SHA-256 of the domain and
the page's path, the same way the image index keys a listing), so a web shop's own "was" prices have a history too;
other pages aren't recorded.

Each row also keeps where it was seen and how many independent sources reported that price
(:mod:`unfret.observations`): the history counts only rows seen on a page Unfret fetched itself, or corroborated, so
a forged page can't plant a low price under another shop's product (nor overwrite a price that counts).
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import asyncpg
import structlog

from unfret import observations
from unfret.facts import PageFacts
from unfret.observations import Kept
from unfret.test_shop import is_test_shop

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
# The product page classes (:data:`unfret.graph.state.PRODUCT_TYPES`; not imported: the graph imports this module).
_PRODUCT_PAGES = frozenset({"storefront", "aggregate_listing"})
# The one query parameter a page key keeps: a web shop's variant ("?variant=…"), which has its own price.
_VARIANT_RE = re.compile(r"(?:^|&)variant=(\d{1,20})(?:&|$)")


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


def gtins(node: object) -> list[str]:
    """
    The valid GTINs in Product JSON-LD (the product and its offers), as 14 digits.

    :param node: A JSON-LD node or list of nodes.
    :return: The GTINs, in document order.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key in _GTIN_KEYS:
            value = str(node.get(key) or "").strip().replace(" ", "")
            if valid_gtin(value):
                found.append(value.zfill(14))
        found += gtins(node.get("offers"))
    elif isinstance(node, list):
        for item in node[:10]:
            found += gtins(item)
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
    found = gtins(facts.json_ld)
    return f"gtin:{found[0]}" if found else None


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


def listing_key(url: str, facts: PageFacts, page_type: str | None = None) -> bytes | None:
    """
    The stored key for a product page: by its public product ID, or else by the page itself.

    The page key is the SHA-256 of the registrable domain and ``path:`` with the page's path (lower case, no trailing
    slash), as the image index keys a listing, plus the variant a web shop's address names. No other part of the
    address: no query, no fragment, no tracking parameters.

    :param url: The page URL.
    :param facts: The page facts.
    :param page_type: The page's class; only a product page (``storefront``, ``aggregate_listing``) gets a page key.
    :return: 32 bytes, or ``None`` without a host, or without a public product ID on a page that isn't a product page.
    """
    if (key := product_key(url, facts)) is not None or page_type not in _PRODUCT_PAGES:
        return key
    parts = urlsplit(url)
    host, path = parts.hostname or "", parts.path.rstrip("/").lower()
    if not host or "." not in host or not path:
        return None
    pid = f"path:{path}"
    if m := _VARIANT_RE.search(parts.query):
        pid += f"?variant={m.group(1)}"
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
    # Every day kept (the last 90, today excluded) in today's currency, oldest first: for the "was" price history.
    observed: tuple[Observation, ...] = ()

    def span_days(self, today: date) -> int:
        """
        How long the product has been observed.

        :param today: Today.
        :return: Days since the first observation (0 without one).
        """
        return (today - self.first_seen).days if self.first_seen else 0


class PriceStore(Protocol):
    """Where observations live: Postgres in production, memory in tests."""

    async def record(
        self, key: bytes, observation: Observation, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0
    ) -> None:
        """
        Keep the day's first observation of a product (later ones that day only add their provenance).

        A different price later that day replaces it only as :func:`unfret.observations.merge` says: one from a page
        Unfret fetched replaces one that doesn't count yet.

        :param key: From :func:`product_key`.
        :param observation: The observation.
        :param bits: Its provenance bits (:mod:`unfret.observations`).
        :param vouch: 1 when a new source reports it.
        """

    async def history(self, key: bytes, today: date, currency: str) -> History:
        """
        The product's observations in the reference period before ``today``.

        :param key: From :func:`product_key`.
        :param today: Today (excluded).
        :param currency: Only prices in this currency count.
        :return: The history (rows that count only, :func:`unfret.observations.counts`).
        """

    async def latest(self, key: bytes) -> Observation | None:
        """
        The product's most recent observation (today's included), in whatever currency it was.

        :param key: From :func:`product_key`.
        :return: The observation, or ``None`` when the product was never seen (or not in the last 90 days); rows
            that don't count are left out.
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
        self.kept: dict[tuple[bytes, date], Kept] = {}  # each row's provenance (rows without one count)

    def _counts(self, row: tuple[bytes, date]) -> bool:
        kept = self.kept.get(row)
        return kept is None or observations.counts(kept.bits, kept.sources)

    async def record(
        self, key: bytes, observation: Observation, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0
    ) -> None:
        """
        Keep the day's first observation, merged as the Postgres store does.

        :param key: The product key.
        :param observation: The observation.
        :param bits: Its provenance bits.
        :param vouch: 1 for a new source.
        """
        row = (key, observation.day)
        old = self.rows.get(row)
        before = (self.kept.get(row) or Kept(observations.LEGACY, 0)) if old is not None else None
        action, kept = observations.merge(before, bits, vouch, same=_same(old, observation), newer_wins=False)
        if action in {"insert", "replace"}:
            self.rows[row] = observation
        self.kept[row] = kept

    async def history(self, key: bytes, today: date, currency: str) -> History:
        """
        The observations before today.

        :param key: The product key.
        :param today: Today (excluded).
        :param currency: The currency to compare in.
        :return: The history.
        """
        start = today - timedelta(days=HISTORY_WINDOW_DAYS)
        mine = [o for (k, d), o in self.rows.items() if k == key and o.day < today and self._counts((k, d))]
        window = [o for o in mine if o.day >= start and o.currency == currency]
        return History(
            first_seen=min((o.day for o in mine), default=None),
            days=len({o.day for o in window}),
            lowest=min((o.price for o in window), default=None),
            observed=tuple(sorted((o for o in mine if o.currency == currency), key=lambda o: o.day)),
        )

    async def latest(self, key: bytes) -> Observation | None:
        """
        The most recent observation.

        :param key: The product key.
        :return: It, or ``None``.
        """
        mine = [o for (k, d), o in self.rows.items() if k == key and self._counts((k, d))]
        return max(mine, key=lambda o: o.day, default=None)

    async def purge(self, before: date) -> None:
        """
        Drop old observations.

        :param before: The first day to keep.
        """
        self.rows = {k: o for k, o in self.rows.items() if o.day >= before}
        self.kept = {k: v for k, v in self.kept.items() if k in self.rows}


def _same(old: Observation | None, new: Observation) -> bool:
    return old is not None and (old.price, old.currency) == (new.price, new.currency)


class PostgresPriceStore:
    """Observations in the ``price_observations`` table (migration 6)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def record(
        self, key: bytes, observation: Observation, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0
    ) -> None:
        """
        Insert the day's first observation; a later one that day is merged (:func:`unfret.observations.merge`).

        :param key: The product key.
        :param observation: The observation.
        :param bits: Its provenance bits.
        :param vouch: 1 for a new source.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            inserted = await conn.fetchval(
                "INSERT INTO price_observations (product_key, day, price, currency, was_price, provenance, sources) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (product_key, day) DO NOTHING RETURNING TRUE",
                key,
                observation.day,
                observation.price,
                observation.currency,
                observation.was_price,
                bits,
                vouch,
            )
            if inserted:
                return
            row: Any = await conn.fetchrow(
                "SELECT price, currency, provenance, sources FROM price_observations "
                "WHERE product_key = $1 AND day = $2 FOR UPDATE",
                key,
                observation.day,
            )
            old = Observation(observation.day, float(row["price"]), row["currency"], None)
            action, kept = observations.merge(
                Kept(row["provenance"], row["sources"]), bits, vouch, same=_same(old, observation), newer_wins=False
            )
            await conn.execute(
                "UPDATE price_observations SET provenance = $3, sources = $4,"
                " price = CASE WHEN $5 THEN $6 ELSE price END, currency = CASE WHEN $5 THEN $7 ELSE currency END,"
                " was_price = CASE WHEN $5 THEN $8 ELSE was_price END WHERE product_key = $1 AND day = $2",
                key,
                observation.day,
                kept.bits,
                kept.sources,
                action == "replace",
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
            "FROM price_observations WHERE product_key = $1 AND day < $2"
            " AND (provenance & $5 <> 0 OR sources >= $6)",
            key,
            today,
            today - timedelta(days=HISTORY_WINDOW_DAYS),
            currency,
            *observations.trusted_params(),
        )
        rows: Any = await self.pool.fetch(
            "SELECT day, price, currency, was_price FROM price_observations WHERE product_key = $1 AND day < $2"
            " AND currency = $3 AND (provenance & $4 <> 0 OR sources >= $5) ORDER BY day",
            key,
            today,
            currency,
            *observations.trusted_params(),
        )
        return History(
            first_seen=row["first_seen"],
            days=int(row["days"] or 0),
            lowest=float(row["lowest"]) if row["lowest"] is not None else None,
            observed=tuple(
                Observation(
                    r["day"],
                    float(r["price"]),
                    r["currency"],
                    float(r["was_price"]) if r["was_price"] is not None else None,
                )
                for r in rows
            ),
        )

    async def latest(self, key: bytes) -> Observation | None:
        """
        The most recent observation.

        :param key: The product key.
        :return: It, or ``None``.
        """
        row: Any = await self.pool.fetchrow(
            "SELECT day, price, currency, was_price FROM price_observations WHERE product_key = $1"
            " AND (provenance & $2 <> 0 OR sources >= $3) ORDER BY day DESC LIMIT 1",
            key,
            *observations.trusted_params(),
        )
        if row is None:
            return None
        was = row["was_price"]
        return Observation(row["day"], float(row["price"]), row["currency"], float(was) if was is not None else None)

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


async def observe(url: str, facts: PageFacts, page_type: str | None = None) -> tuple[bytes, History] | None:
    """
    Record today's price of a product page and return its history (before today).

    Recorded with the running analysis's provenance (:func:`unfret.observations.current`); past the network's daily
    write limit it is only looked up. Purges expired rows at most once a day per process. Never raises: a failing
    store costs the price history, not the analysis.

    :param url: The page URL.
    :param facts: The page facts.
    :param page_type: The page's class: a product page without a public product ID is keyed by its page
        (:func:`listing_key`).
    :return: The product key and its history, or ``None`` for pages without a price or a key, and for the test
        shop (:mod:`unfret.test_shop`), whose prices are made up and never recorded.
    """
    key = listing_key(url, facts, page_type)
    if key is None or facts.price is None or is_test_shop(url):
        return None
    day = today()
    currency = (facts.currency or "").upper()[:3]
    try:
        history = await _store.history(key, day, currency)
        await _record(key, Observation(day, round(facts.price, 2), currency, facts.list_price))
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("price_store_failed", error=type(exc).__name__)
        return None
    return key, history


async def _record(key: bytes, observation: Observation) -> None:
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    provenance = observations.current()
    if not provenance.record:
        return
    vouch = observations.vouch(b"price|" + key + observation.day.isoformat().encode())
    await _store.record(key, observation, bits=provenance.bits, vouch=vouch)
    if _purged_on != observation.day:
        _purged_on = observation.day
        await _store.purge(observation.day - timedelta(days=RETENTION_DAYS))


async def record(url: str, facts: PageFacts, page_type: str | None = None) -> bool:
    """
    Record today's price of a product page without looking anything up (a quick check, after its answer).

    The same row :func:`observe` writes, with the running analysis's provenance. Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :param page_type: The page's class (:func:`listing_key`).
    :return: Whether a price was recorded (not for pages without one or a key, nor the test shop).
    """
    key = listing_key(url, facts, page_type)
    if key is None or facts.price is None or is_test_shop(url):
        return False
    currency = (facts.currency or "").upper()[:3]
    try:
        await _record(key, Observation(today(), round(facts.price, 2), currency, facts.list_price))
    except Exception as exc:  # noqa: BLE001 - optional, after the answer
        logger.warning("price_store_failed", error=type(exc).__name__)
        return False
    return True


async def seen_price(key: bytes) -> Observation | None:
    """
    The last price Unfret saw for a listing (by its stored key), for "sold elsewhere for $X".

    The image index keys a shop's listing the same way as this store (registrable domain and public product ID, or
    the page's path; :func:`listing_key`), so
    a photo match on another shop can name that shop's price when Unfret has seen it. Never raises.

    :param key: The listing's key.
    :return: The latest observation, or ``None``.
    """
    try:
        return await _store.latest(key)
    except Exception as exc:  # noqa: BLE001 - optional
        logger.warning("price_store_failed", error=type(exc).__name__)
        return None
