"""
Anonymous low-stock claims per product page, to tell real stock from an "only 3 left" that never moves.

A shop's "Only 3 left" can't be checked on one visit. Real stock sells down, runs out or is restocked now and then;
a claim made up by the page stays the same for weeks, or goes back up again and again. Every analysed shop page that
makes such a claim records its count for the day, and the next check of that page compares.

What is stored, and nothing else: a SHA-256 of the shop's registrable domain and the product (its public identifier,
as in :mod:`unfret.price_store`, else the product page's path, lowercased, without the query except a ``variant``
number), the day and the count. One row per product, day and count. No URL, no caller, no pass, no IP address, no
time of day. Rows older than 90 days are deleted. Each row keeps where it was seen and how many independent sources
reported it (:mod:`unfret.observations`): only rows that count are compared, so a forged page can't make another
shop's stock claim look stuck.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, urlsplit

import asyncpg
import structlog

from unfret import observations
from unfret.facts import PageFacts
from unfret.observations import Kept
from unfret.price_store import product_id, registrable_domain
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

RETENTION_DAYS = 90
WINDOW_DAYS = 30  # the claims compared
MAX_COUNT = 99  # a higher count isn't a scarcity claim
# "The same count": seen on at least this many days, the first at least this many days before today.
STATIC_MIN_DAYS = 3
STATIC_MIN_SPAN_DAYS = 7
# "Goes back up": the count rose from one day seen to the next at least this many times.
RESETS_MIN = 2

Pattern = Literal["static", "resets"]


def product_key(url: str, facts: PageFacts) -> bytes | None:
    """
    The stored key for a product page: SHA-256 of the registrable domain and the product.

    The product is its public identifier when the page shows one, else the page's path (lowercased, the part from
    ``/products/`` on for a shop whose collections repeat it), plus the ``variant`` number when the URL names one:
    each variant has its own stock.

    :param url: The page URL.
    :param facts: The page facts.
    :return: 32 bytes, or ``None`` without a host or a path.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        return None
    product = product_id(url, facts)
    if product is None:
        path = parts.path.lower().rstrip("/")
        if (at := path.find("/products/")) >= 0:
            path = path[at:]
        if not path:
            return None
        product = f"path:{path}"
    variant = (parse_qs(parts.query).get("variant") or [""])[0]
    if variant.isdigit():
        product += f"|variant:{variant}"
    return hashlib.sha256(f"{registrable_domain(host)}|{product}".encode()).digest()


@dataclass(frozen=True, order=True)
class Claim:
    """A count claimed on one day."""

    day: date
    count: int


@dataclass(frozen=True)
class Finding:
    """What the claims over time show."""

    pattern: Pattern
    count: int  # the count claimed today
    days: int  # distinct days the claim was seen (today included)
    span_days: int  # days from the first claim to today
    resets: int  # times the count went back up


class ScarcityStore(Protocol):
    """Where claims live: Postgres in production, memory in tests."""

    async def record(self, key: bytes, claim: Claim, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0) -> None:
        """
        Keep a claim (the same count again that day only adds its provenance).

        :param key: From :func:`product_key`.
        :param claim: The claim.
        :param bits: Its provenance bits.
        :param vouch: 1 when a new source reports it.
        """

    async def claims(self, key: bytes, since: date) -> list[Claim]:
        """
        The product's claims that count, from a day on.

        :param key: From :func:`product_key`.
        :param since: The first day.
        :return: The claims, by day.
        """

    async def purge(self, before: date) -> None:
        """
        Delete claims from before a day.

        :param before: The first day to keep.
        """


class MemoryScarcityStore:
    """In-process store, for tests and runs without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[tuple[bytes, date, int], Kept] = {}

    async def record(self, key: bytes, claim: Claim, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0) -> None:
        """
        Keep a claim.

        :param key: The product key.
        :param claim: The claim.
        :param bits: Its provenance bits.
        :param vouch: 1 for a new source.
        """
        row = (key, claim.day, claim.count)
        _, kept = observations.merge(self.rows.get(row), bits, vouch, same=True, newer_wins=False)
        self.rows[row] = kept

    async def claims(self, key: bytes, since: date) -> list[Claim]:
        """
        The claims that count.

        :param key: The product key.
        :param since: The first day.
        :return: The claims.
        """
        return sorted(
            Claim(d, c)
            for (k, d, c), kept in self.rows.items()
            if k == key and d >= since and observations.counts(kept.bits, kept.sources)
        )

    async def purge(self, before: date) -> None:
        """
        Drop old claims.

        :param before: The first day to keep.
        """
        self.rows = {r: k for r, k in self.rows.items() if r[1] >= before}


class PostgresScarcityStore:
    """Claims in the ``scarcity_claims`` table (migration 47)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def record(self, key: bytes, claim: Claim, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0) -> None:
        """
        Insert a claim, or add this observation's provenance to it.

        :param key: The product key.
        :param claim: The claim.
        :param bits: Its provenance bits.
        :param vouch: 1 for a new source.
        """
        await self.pool.execute(
            "INSERT INTO scarcity_claims (product_key, day, count, provenance, sources) VALUES ($1, $2, $3, $4, $5) "
            "ON CONFLICT (product_key, day, count) DO UPDATE SET provenance = scarcity_claims.provenance | $4, "
            "sources = LEAST(scarcity_claims.sources + $5, $6)",
            key,
            claim.day,
            claim.count,
            bits,
            vouch,
            observations.MAX_SOURCES,
        )

    async def claims(self, key: bytes, since: date) -> list[Claim]:
        """
        The claims that count.

        :param key: The product key.
        :param since: The first day.
        :return: The claims.
        """
        rows: Any = await self.pool.fetch(
            "SELECT day, count FROM scarcity_claims WHERE product_key = $1 AND day >= $2"
            " AND (provenance & $3 <> 0 OR sources >= $4) ORDER BY day, count",
            key,
            since,
            *observations.trusted_params(),
        )
        return [Claim(r["day"], int(r["count"])) for r in rows]

    async def purge(self, before: date) -> None:
        """
        Delete old claims.

        :param before: The first day to keep.
        """
        await self.pool.execute("DELETE FROM scarcity_claims WHERE day < $1", before)


_store: ScarcityStore = MemoryScarcityStore()
_purged_on: date | None = None


def use_store(store: ScarcityStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresScarcityStore` at startup).

    :param store: The store.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


def today() -> date:
    """
    Today in UTC, the only time stored with a claim.

    :return: The date.
    """
    return datetime.now(tz=UTC).date()


def assess(claims: Iterable[Claim], now: Claim) -> Finding | None:
    """
    Whether a product's stock claims over time look like real stock.

    Real stock sells down, runs out or is restocked now and then. Two patterns it rarely shows: the same low count on
    many days over a week or more (``static``), and a count that keeps going back up (``resets``: it rose from one
    day seen to the next at least :data:`RESETS_MIN` times). This is a finding, not a verdict: the level model weighs
    it.

    :param claims: Earlier claims that count (today's earlier ones may be among them).
    :param now: Today's claim.
    :return: The finding, or ``None``.
    """
    by_day: dict[date, set[int]] = {}
    for c in (*claims, now):
        by_day.setdefault(c.day, set()).add(c.count)
    days = sorted(by_day)
    span = (now.day - days[0]).days
    counts = set().union(*by_day.values())
    rises = sum(1 for prev, nxt in zip(days, days[1:], strict=False) if max(by_day[nxt]) > min(by_day[prev]))
    if rises >= RESETS_MIN:
        return Finding("resets", now.count, len(days), span, rises)
    if counts == {now.count} and len(days) >= STATIC_MIN_DAYS and span >= STATIC_MIN_SPAN_DAYS:
        return Finding("static", now.count, len(days), span, 0)
    return None


def _claim(url: str, facts: PageFacts) -> tuple[bytes, Claim] | None:
    count = facts.stock_left
    if count is None or not 1 <= count <= MAX_COUNT or is_test_shop(url):
        return None
    key = product_key(url, facts)
    return (key, Claim(today(), count)) if key is not None else None


async def _record(key: bytes, claim: Claim) -> None:
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    provenance = observations.current()
    if not provenance.record:
        return
    vouch = observations.vouch(b"scarcity|" + key + f"{claim.day.isoformat()}|{claim.count}".encode())
    await _store.record(key, claim, bits=provenance.bits, vouch=vouch)
    if _purged_on != claim.day:
        _purged_on = claim.day
        await _store.purge(claim.day - timedelta(days=RETENTION_DAYS))


async def observe(url: str, facts: PageFacts) -> Finding | None:
    """
    Compare today's stock claim with the page's earlier ones, then record it.

    Recorded with the running analysis's provenance (:func:`unfret.observations.current`). Never raises: a failing
    store costs this finding, not the analysis.

    :param url: The page URL.
    :param facts: The page facts (``stock_left``).
    :return: The finding, or ``None`` (no claim, nothing unusual, or the test shop, which is never recorded).
    """
    found = _claim(url, facts)
    if found is None:
        return None
    key, claim = found
    try:
        earlier = await _store.claims(key, claim.day - timedelta(days=WINDOW_DAYS))
        await _record(key, claim)
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("scarcity_store_failed", error=type(exc).__name__)
        return None
    return assess(earlier, claim)


async def record(url: str, facts: PageFacts) -> bool:
    """
    Record today's stock claim without comparing (a quick check, after its answer). Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: Whether a claim was recorded.
    """
    found = _claim(url, facts)
    if found is None:
        return False
    try:
        await _record(*found)
    except Exception as exc:  # noqa: BLE001 - optional, after the answer
        logger.warning("scarcity_store_failed", error=type(exc).__name__)
        return False
    return True
