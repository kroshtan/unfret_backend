"""
Review fingerprints: the same review texts on different shops, without keeping the reviews.

Fake shops fill their pages with reviews copied from elsewhere (another shop, a marketplace, the supplier's listing),
so the same words turn up under different names. Every review long enough to be distinctive is normalised (lower
case, letters and digits only, single spaces) and kept only as a SHA-256 of that text, with a SHA-256 of the shop's
registrable domain (a marketplace counts as one shop) and the first and last day it was seen.

What is stored, and nothing else: those two hashes and two days. No review text, reviewer name, rating, URL, caller,
wallet, IP address or time of day. Rows not seen for 90 days are deleted. Off in evaluation runs, with the shop
fingerprints.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

import asyncpg
import structlog

from unfret.facts import PageFacts
from unfret.market_prices import shop_key
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
        :return: ``(matched fingerprints, other shops)``.
        """
        ...

    async def record(self, shop: bytes, hashes: set[bytes], day: date) -> None:
        """
        Add or refresh the shop's fingerprints.

        :param shop: The shop key.
        :param hashes: The fingerprints.
        :param day: Today.
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

    async def matches(self, shop: bytes, hashes: set[bytes]) -> tuple[int, int]:
        """
        Fingerprints other shops have.

        :param shop: This shop.
        :param hashes: The fingerprints.
        :return: ``(matched, other shops)``.
        """
        hits = [(r, s) for (r, s) in self.rows if r in hashes and s != shop]
        return len({r for r, _ in hits}), len({s for _, s in hits})

    async def record(self, shop: bytes, hashes: set[bytes], day: date) -> None:
        """
        Add or refresh fingerprints.

        :param shop: The shop.
        :param hashes: The fingerprints.
        :param day: Today.
        """
        for review in hashes:
            first, _ = self.rows.get((review, shop), (day, day))
            self.rows[(review, shop)] = (first, day)

    async def purge(self, before: date) -> None:
        """
        Drop old fingerprints.

        :param before: The first day to keep.
        """
        self.rows = {k: v for k, v in self.rows.items() if v[1] >= before}


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
            "FROM review_fingerprints WHERE review_hash = ANY($1::bytea[]) AND shop_key <> $2",
            sorted(hashes),
            shop,
        )
        return int(row["matched"]), int(row["shops"])

    async def record(self, shop: bytes, hashes: set[bytes], day: date) -> None:
        """
        Upsert the fingerprints.

        :param shop: The shop.
        :param hashes: The fingerprints.
        :param day: Today.
        """
        await self.pool.executemany(
            "INSERT INTO review_fingerprints (review_hash, shop_key, first_seen, last_seen) VALUES ($1, $2, $3, $3) "
            "ON CONFLICT (review_hash, shop_key) DO UPDATE SET last_seen = EXCLUDED.last_seen",
            [(review, shop, day) for review in sorted(hashes)],
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
    Look the page's reviews up on other shops, then record them. Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :return: The copies, or ``None`` without distinctive reviews or a shop (or with a failing store), and for the
        test shop (:mod:`unfret.test_shop`), whose invented reviews are never recorded.
    """
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    hashes, shop = review_hashes(facts), shop_key(url, facts)
    if not hashes or shop is None or is_test_shop(url):
        return None
    day = today()
    try:
        copied, shops = await _store.matches(shop, hashes)
        await _store.record(shop, hashes, day)
        if _purged_on != day:
            _purged_on = day
            await _store.purge(day - timedelta(days=RETENTION_DAYS))
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("review_store_failed", error=type(exc).__name__)
        return None
    return Copies(len(hashes), copied, shops)
