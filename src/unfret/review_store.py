"""
Review fingerprints: the same review texts on different shops, without keeping the reviews.

Fake shops fill their pages with reviews copied from elsewhere (another shop, a marketplace, the supplier's listing),
so the same words turn up under different names. Every review long enough to be distinctive is normalised (lower
case, letters and digits only, single spaces) and kept only as a SHA-256 of that text, with a SHA-256 of the shop's
registrable domain (a marketplace counts as one shop) and the first and last day it was seen.

What is stored, and nothing else: those two hashes and two days. No review text, reviewer name, rating, URL, caller,
wallet, IP address or time of day. Rows not seen for 90 days are deleted. Off in evaluation runs, with the shop
fingerprints.

Each row also keeps where it was seen and how many independent sources reported it (:mod:`unfret.observations`).
Only rows seen on a page Unfret fetched itself, or corroborated, are matched: a forged page can't make another shop's
reviews look copied.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import asyncpg
import structlog

from unfret import observations
from unfret.facts import PageFacts
from unfret.market_prices import shop_key
from unfret.observations import Kept
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

RETENTION_DAYS = 90
MIN_CHARS = 60  # shorter reviews ("Great product, fast delivery!") repeat naturally
MIN_WORDS = 10
MAX_REVIEWS = 100
_NON_WORD_RE = re.compile(r"[\W_]+")


def normalise(text: str) -> str:
    """
    A review's comparable form: accents folded, lower case, letters and digits only, single spaces.

    :param text: The review title and body.
    :return: The normalised text.
    """
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _NON_WORD_RE.sub(" ", folded.lower()).strip()


def review_hashes(facts: PageFacts) -> set[bytes]:
    """
    The fingerprints of the page's distinctive reviews.

    :param facts: The page facts (``reviews``).
    :return: SHA-256 digests of the normalised review bodies long enough to be distinctive.
    """
    found: set[bytes] = set()
    for review in facts.reviews[:MAX_REVIEWS]:
        text = normalise(review.body)
        if len(text) >= MIN_CHARS and len(text.split()) >= MIN_WORDS:
            found.add(hashlib.sha256(f"review:{text}".encode()).digest())
    return found


@dataclass(frozen=True)
class Copies:
    """How many of a page's reviews other shops show too."""

    checked: int  # distinctive reviews on the page
    copied: int  # of those, seen on another shop
    other_shops: int  # distinct other shops showing at least one of them

    @property
    def share(self) -> float:
        """
        The share of distinctive reviews seen elsewhere.

        :return: 0 to 1.
        """
        return self.copied / self.checked if self.checked else 0.0


class ReviewStore(Protocol):
    """Where fingerprints live: Postgres in production, memory in tests."""

    async def matches(self, shop: bytes, hashes: set[bytes]) -> tuple[int, int]:
        """
        How many of the fingerprints other shops have, and how many other shops.

        :param shop: This shop (left out).
        :param hashes: The page's fingerprints.
        :return: ``(matched fingerprints, other shops)``, rows that count only (:func:`unfret.observations.counts`).
        """
        ...

    async def record(
        self, shop: bytes, hashes: set[bytes], day: date, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0
    ) -> None:
        """
        Add or refresh the shop's fingerprints, adding the observation's provenance and source.

        :param shop: The shop key.
        :param hashes: The fingerprints.
        :param day: Today.
        :param bits: Its provenance bits (:mod:`unfret.observations`).
        :param vouch: 1 when a new source reports them.
        """

    async def purge(self, before: date) -> None:
        """
        Delete fingerprints not seen since a day.

        :param before: The first day to keep.
        """


class MemoryReviewStore:
    """In-process store, for tests and runs without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[tuple[bytes, bytes], tuple[date, date]] = {}  # (review, shop) → first, last seen
        self.kept: dict[tuple[bytes, bytes], Kept] = {}  # each row's provenance (rows without one count)

    def _counts(self, row: tuple[bytes, bytes]) -> bool:
        kept = self.kept.get(row)
        return kept is None or observations.counts(kept.bits, kept.sources)

    async def matches(self, shop: bytes, hashes: set[bytes]) -> tuple[int, int]:
        """
        Fingerprints other shops have.

        :param shop: This shop.
        :param hashes: The fingerprints.
        :return: ``(matched, other shops)``.
        """
        hits = [(r, s) for (r, s) in self.rows if r in hashes and s != shop and self._counts((r, s))]
        return len({r for r, _ in hits}), len({s for _, s in hits})

    async def record(
        self, shop: bytes, hashes: set[bytes], day: date, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0
    ) -> None:
        """
        Add or refresh fingerprints.

        :param shop: The shop.
        :param hashes: The fingerprints.
        :param day: Today.
        :param bits: Their provenance bits.
        :param vouch: 1 for a new source.
        """
        for review in hashes:
            row = (review, shop)
            before = (self.kept.get(row) or Kept(observations.LEGACY, 0)) if row in self.rows else None
            first, _ = self.rows.get(row, (day, day))
            self.rows[row] = (first, day)
            self.kept[row] = observations.merge(before, bits, vouch, same=True, newer_wins=False)[1]

    async def purge(self, before: date) -> None:
        """
        Drop old fingerprints.

        :param before: The first day to keep.
        """
        self.rows = {k: v for k, v in self.rows.items() if v[1] >= before}
        self.kept = {k: v for k, v in self.kept.items() if k in self.rows}


class PostgresReviewStore:
    """Fingerprints in the ``review_fingerprints`` table (migration 18)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def matches(self, shop: bytes, hashes: set[bytes]) -> tuple[int, int]:
        """
        Fingerprints other shops have.

        :param shop: This shop.
        :param hashes: The fingerprints.
        :return: ``(matched, other shops)``.
        """
        row: Any = await self.pool.fetchrow(
            "SELECT COUNT(DISTINCT review_hash) AS matched, COUNT(DISTINCT shop_key) AS shops "
            "FROM review_fingerprints WHERE review_hash = ANY($1::bytea[]) AND shop_key <> $2"
            " AND (provenance & $3 <> 0 OR sources >= $4)",
            sorted(hashes),
            shop,
            *observations.trusted_params(),
        )
        return int(row["matched"]), int(row["shops"])

    async def record(
        self, shop: bytes, hashes: set[bytes], day: date, *, bits: int = observations.DEFAULT_BITS, vouch: int = 0
    ) -> None:
        """
        Upsert the fingerprints: a known one adds the provenance bits and the source (merged as the same value).

        :param shop: The shop.
        :param hashes: The fingerprints.
        :param day: Today.
        :param bits: Their provenance bits.
        :param vouch: 1 for a new source.
        """
        await self.pool.executemany(
            "INSERT INTO review_fingerprints (review_hash, shop_key, first_seen, last_seen, provenance, sources) "
            "VALUES ($1, $2, $3, $3, $4, $5) ON CONFLICT (review_hash, shop_key) DO UPDATE SET "
            "last_seen = EXCLUDED.last_seen, provenance = review_fingerprints.provenance | EXCLUDED.provenance, "
            "sources = LEAST(review_fingerprints.sources + EXCLUDED.sources, 32767)",  # observations.MAX_SOURCES
            [(review, shop, day, bits, vouch) for review in sorted(hashes)],
        )

    async def purge(self, before: date) -> None:
        """
        Delete old fingerprints.

        :param before: The first day to keep.
        """
        await self.pool.execute("DELETE FROM review_fingerprints WHERE last_seen < $1", before)


_store: ReviewStore = MemoryReviewStore()
_purged_on: date | None = None


def use_store(store: ReviewStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresReviewStore` at startup).

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


async def observe(url: str, facts: PageFacts) -> Copies | None:
    """
    Look the page's reviews up on other shops, then record them (as :func:`record`). Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: The copies, or ``None`` without distinctive reviews or a shop (or with a failing store), and for the
        test shop (:mod:`unfret.test_shop`), whose invented reviews are never recorded.
    """
    hashes, shop = review_hashes(facts), shop_key(url, facts)
    if not hashes or shop is None or is_test_shop(url):
        return None
    try:
        copied, shops = await _store.matches(shop, hashes)
        await _record(shop, hashes)
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("review_store_failed", error=type(exc).__name__)
        return None
    return Copies(len(hashes), copied, shops)


async def _record(shop: bytes, hashes: set[bytes]) -> None:
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    provenance = observations.current()
    if not provenance.record:
        return
    day = today()
    # One source vouches for a shop's reviews once a day, whichever of them it saw.
    vouch = observations.vouch(b"reviews|" + shop + day.isoformat().encode())
    await _store.record(shop, hashes, day, bits=provenance.bits, vouch=vouch)
    if _purged_on != day:
        _purged_on = day
        await _store.purge(day - timedelta(days=RETENTION_DAYS))


async def record(url: str, facts: PageFacts) -> bool:
    """
    Record the page's review fingerprints without looking anything up (a quick check, after its answer).

    The same rows :func:`observe` writes, with the running analysis's provenance. Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: Whether any were recorded (not without distinctive reviews or a shop, nor for the test shop).
    """
    hashes, shop = review_hashes(facts), shop_key(url, facts)
    if not hashes or shop is None or is_test_shop(url):
        return False
    try:
        await _record(shop, hashes)
    except Exception as exc:  # noqa: BLE001 - optional, after the answer
        logger.warning("review_store_failed", error=type(exc).__name__)
        return False
    return True
