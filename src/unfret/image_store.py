"""
Where the image index lives, and how a photo is looked up in it.

Each row is one listing's main photo: the shop (its registrable domain, or the marketplace for Amazon and bol.com
listings), a hash of the listing (the shop and its public product ID, or the page path), the photo's fingerprints
(:mod:`unfret.image_index`) and the photo's own address, for thumbnails. Rows come from analyses (``seen``) and
from the public ABO catalogue (``abo``, see :mod:`unfret.image_seed`). As in the shop store: days not times, and
no caller, page address, score or verdict.

A lookup finds rows whose pHash is close (in SQL), keeps those whose dHash, embedding and colours agree as well, and
drops the photo's own shop (a shop reusing its photos is normal) and the same listing (a listing that changes is an
update, not a duplicate). A photo found on more than :data:`STOP_DOMAINS` shops is a stock image, logo or
placeholder, not evidence: it matches nothing. Matches are direct (photo to photo), never chained.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import asyncpg

from unfret.facts import PageFacts
from unfret.image_index import THRESHOLDS, ImagePrint, Thresholds, hamming, same_photo
from unfret.net import hostname
from unfret.price_store import product_id, registrable_domain

Source = Literal["seen", "abo"]

RETENTION_DAYS = 400  # for photos seen in analyses; the public catalogue seed stays
STOP_DOMAINS = 50
_CANDIDATES = 500
# Marketplaces are one shop family across their country domains (amazon.de and amazon.nl list the same photos).
_FAMILIES = {"amazon": "amazon", "bol.com": "bol.com"}


@dataclass(frozen=True)
class Listing:
    """Where a photo was seen."""

    domain: str
    listing_key: bytes


@dataclass(frozen=True)
class StoredPrint:
    """A photo in the index."""

    id: int
    source: Source
    domain: str
    listing_key: bytes
    print: ImagePrint
    label: str | None = None  # the catalogue brand, for seed rows


@dataclass(frozen=True)
class Lookup:
    """What the index knows of a photo."""

    matches: list[StoredPrint]  # other shops' and listings' copies, one per shop, seed rows last
    stop: bool = False  # the photo is everywhere: a stock image, not evidence


def listing(url: str, facts: PageFacts) -> Listing | None:
    """
    The shop and listing a product page belongs to.

    :param url: The page URL.
    :param facts: The page facts.
    :return: The listing, or ``None`` without a host.
    """
    host = hostname(url)
    if not host or "." not in host:
        return None
    domain = _FAMILIES.get(facts.platform) or registrable_domain(host)
    pid = product_id(url, facts) or f"path:{urlsplit(url).path.rstrip('/').lower()}"
    return Listing(domain, hashlib.sha256(f"{domain}|{pid}".encode()).digest())


class ImageStore(Protocol):
    """Where the index lives: Postgres in production, memory in tests."""

    async def candidates(self, phash: int, max_distance: int) -> list[StoredPrint]:
        """
        Rows whose pHash is within ``max_distance`` bits.

        :param phash: The query hash.
        :param max_distance: The largest Hamming distance.
        :return: Up to a few hundred rows.
        """

    async def upsert(
        self, where: Listing, fp: ImagePrint, *, image_url: str, day: date, source: Source, label: str | None = None
    ) -> None:
        """
        Store a listing's main photo, replacing the listing's previous one.

        :param where: The listing.
        :param fp: The photo's fingerprints.
        :param image_url: The photo's address.
        :param day: Today.
        :param source: ``seen`` or ``abo``.
        :param label: The catalogue brand (seed rows).
        """

    async def image_url(self, print_id: int) -> str | None:
        """
        A stored photo's address.

        :param print_id: The row.
        :return: The URL, or ``None``.
        """

    async def purge(self, today: date) -> None:
        """
        Delete photos seen in analyses that weren't seen for :data:`RETENTION_DAYS`.

        :param today: Today.
        """


class MemoryImageStore:
    """In-process index, for tests."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[tuple[str, bytes], tuple[StoredPrint, str, date, date]] = {}
        self._next = 1

    async def candidates(self, phash: int, max_distance: int) -> list[StoredPrint]:
        """
        Linear scan.

        :param phash: The query hash.
        :param max_distance: The largest distance.
        :return: The rows within it.
        """
        return [r for r, *_ in self.rows.values() if hamming(r.print.phash, phash) <= max_distance][:_CANDIDATES]

    async def upsert(
        self, where: Listing, fp: ImagePrint, *, image_url: str, day: date, source: Source, label: str | None = None
    ) -> None:
        """
        Store or replace.

        :param where: The listing.
        :param fp: The fingerprints.
        :param image_url: The address.
        :param day: Today.
        :param source: The source.
        :param label: The brand.
        """
        key = (where.domain, where.listing_key)
        old = self.rows.get(key)
        row_id = old[0].id if old else self._next
        self._next += 0 if old else 1
        stored = StoredPrint(row_id, source, where.domain, where.listing_key, fp, label)
        self.rows[key] = (stored, image_url, old[2] if old else day, day)

    async def image_url(self, print_id: int) -> str | None:
        """
        Look a row up.

        :param print_id: The row.
        :return: Its URL.
        """
        return next((url for row, url, *_ in self.rows.values() if row.id == print_id), None)

    async def purge(self, today: date) -> None:
        """
        Apply the retention limit.

        :param today: Today.
        """
        cutoff = today - timedelta(days=RETENTION_DAYS)
        self.rows = {k: v for k, v in self.rows.items() if v[0].source != "seen" or v[3] >= cutoff}


class PostgresImageStore:
    """The index in the ``image_prints`` table (migration 11)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def candidates(self, phash: int, max_distance: int) -> list[StoredPrint]:
        """
        A scan with a popcount filter: tens of milliseconds for 100,000 rows.

        :param phash: The query hash.
        :param max_distance: The largest distance.
        :return: The rows within it.
        """
        rows: list[Any] = await self.pool.fetch(
            "SELECT id, source, domain, listing_key, phash, dhash, embedding, colour, label FROM image_prints "
            "WHERE bit_count((phash # $1)::bit(64)) <= $2 LIMIT $3",
            phash,
            max_distance,
            _CANDIDATES,
        )
        return [
            StoredPrint(
                r["id"],
                r["source"],
                r["domain"],
                bytes(r["listing_key"]),
                ImagePrint(r["phash"], r["dhash"], bytes(r["embedding"]), bytes(r["colour"])),
                r["label"],
            )  # fmt: skip
            for r in rows
        ]

    async def upsert(
        self, where: Listing, fp: ImagePrint, *, image_url: str, day: date, source: Source, label: str | None = None
    ) -> None:
        """
        Insert, or replace the listing's photo and refresh its last-seen day.

        :param where: The listing.
        :param fp: The fingerprints.
        :param image_url: The address.
        :param day: Today.
        :param source: The source.
        :param label: The brand.
        """
        await self.pool.execute(
            "INSERT INTO image_prints (source, domain, listing_key, phash, dhash, embedding, colour, image_url,"
            " label, first_seen, last_seen) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10) "
            "ON CONFLICT (domain, listing_key) DO UPDATE SET phash = EXCLUDED.phash, dhash = EXCLUDED.dhash,"
            " embedding = EXCLUDED.embedding, colour = EXCLUDED.colour, image_url = EXCLUDED.image_url,"
            " last_seen = EXCLUDED.last_seen",
            source,
            where.domain,
            where.listing_key,
            fp.phash,
            fp.dhash,
            fp.embedding,
            fp.colour,
            image_url[:1000],
            label,
            day,
        )

    async def image_url(self, print_id: int) -> str | None:
        """
        Look a row up.

        :param print_id: The row.
        :return: Its URL.
        """
        url: str | None = await self.pool.fetchval("SELECT image_url FROM image_prints WHERE id = $1", print_id)
        return url

    async def purge(self, today: date) -> None:
        """
        Apply the retention limit to photos seen in analyses.

        :param today: Today.
        """
        await self.pool.execute(
            "DELETE FROM image_prints WHERE source = 'seen' AND last_seen < $1", today - timedelta(days=RETENTION_DAYS)
        )


async def lookup(
    store: ImageStore, fp: ImagePrint, where: Listing | None, thresholds: Thresholds = THRESHOLDS
) -> Lookup:
    """
    The other shops and listings showing the same photo.

    :param store: The index.
    :param fp: The photo's fingerprints.
    :param where: The listing it was seen on (its own shop and listing don't count).
    :param thresholds: When two photos are the same.
    :return: One match per other shop; ``stop`` when the photo is on too many shops to mean anything.
    """
    same = [c for c in await store.candidates(fp.phash, thresholds.phash) if same_photo(fp, c.print, thresholds)]
    if where is not None:
        same = [c for c in same if c.domain != where.domain and c.listing_key != where.listing_key]
    per_domain: dict[str, StoredPrint] = {}
    for match in sorted(same, key=lambda m: (m.source != "seen", m.id)):
        per_domain.setdefault(match.domain, match)
    if len(per_domain) > STOP_DOMAINS:
        return Lookup([], stop=True)
    return Lookup(list(per_domain.values()))
