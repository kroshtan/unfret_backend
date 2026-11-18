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
AD_RETENTION_DAYS = 90  # for photos of peer-to-peer ads (a private seller's photos: as short as the ad store)
STOP_DOMAINS = 50
_CANDIDATES = 500
# Marketplaces are one shop family across their country domains (amazon.de and amazon.nl list the same photos).
_FAMILIES = {"amazon": "amazon", "bol.com": "bol.com"}
# Peer-to-peer platforms are one family each too (vinted.nl and vinted.de), apart from the shops: an ad's photos are
# matched against other ads and shops (:func:`lookup_ad`), a shop's never against ads.
AD_FAMILIES: dict[str, str] = {p: p for p in ("ebay", "marktplaats", "2dehands", "vinted", "craigslist", "facebook")}
_AD_DOMAINS = tuple(AD_FAMILIES.values())


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
    domain = _FAMILIES.get(facts.platform) or AD_FAMILIES.get(facts.platform) or registrable_domain(host)
    # An ad is its own listing even when it names a product's barcode (two people can sell the same phone).
    pid = None if facts.platform in AD_FAMILIES else product_id(url, facts)
    pid = pid or f"path:{urlsplit(url).path.rstrip('/').lower()}"
    return Listing(domain, hashlib.sha256(f"{domain}|{pid}".encode()).digest())


class Candidates(Protocol):
    """What a lookup needs of an index: the real one, or the test shop's (:mod:`unfret.test_shop_photos`)."""

    async def candidates(self, phash: int, max_distance: int) -> list[StoredPrint]:
        """
        Rows whose pHash is within ``max_distance`` bits.

        :param phash: The query hash.
        :param max_distance: The largest Hamming distance.
        :return: Up to a few hundred rows.
        """


class ImageStore(Candidates, Protocol):
    """Where the index lives: Postgres in production, memory in tests."""

    async def upsert(
        self, where: Listing, fp: ImagePrint, *, image_url: str, day: date, source: Source, label: str | None = None
    ) -> int:
        """
        Store a listing's main photo, replacing the listing's previous one.

        :param where: The listing.
        :param fp: The photo's fingerprints.
        :param image_url: The photo's address.
        :param day: Today.
        :param source: ``seen`` or ``abo``.
        :param label: The catalogue brand (seed rows).
        :return: The row's ID (for its thumbnail).
        """

    async def image_url(self, print_id: int) -> str | None:
        """
        A stored photo's address.

        :param print_id: The row.
        :return: The URL, or ``None``.
        """

    async def purge(self, today: date) -> None:
        """
        Delete photos seen in analyses not seen for :data:`RETENTION_DAYS` (ads: :data:`AD_RETENTION_DAYS`).

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
    ) -> int:
        """
        Store or replace.

        :param where: The listing.
        :param fp: The fingerprints.
        :param image_url: The address.
        :param day: Today.
        :param source: The source.
        :param label: The brand.
        :return: The row's ID.
        """
        key = (where.domain, where.listing_key)
        old = self.rows.get(key)
        row_id = old[0].id if old else self._next
        self._next += 0 if old else 1
        stored = StoredPrint(row_id, source, where.domain, where.listing_key, fp, label)
        self.rows[key] = (stored, image_url, old[2] if old else day, day)
        return row_id

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
        ad_cutoff = today - timedelta(days=AD_RETENTION_DAYS)
        self.rows = {
            k: v
            for k, v in self.rows.items()
            if v[0].source != "seen" or v[3] >= (ad_cutoff if v[0].domain in _AD_DOMAINS else cutoff)
        }


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
    ) -> int:
        """
        Insert, or replace the listing's photo and refresh its last-seen day.

        :param where: The listing.
        :param fp: The fingerprints.
        :param image_url: The address.
        :param day: Today.
        :param source: The source.
        :param label: The brand.
        :return: The row's ID.
        """
        row_id: int = await self.pool.fetchval(
            "INSERT INTO image_prints (source, domain, listing_key, phash, dhash, embedding, colour, image_url,"
            " label, first_seen, last_seen) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10) "
            "ON CONFLICT (domain, listing_key) DO UPDATE SET phash = EXCLUDED.phash, dhash = EXCLUDED.dhash,"
            " embedding = EXCLUDED.embedding, colour = EXCLUDED.colour, image_url = EXCLUDED.image_url,"
            " last_seen = EXCLUDED.last_seen RETURNING id",
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
        return row_id

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
            "DELETE FROM image_prints WHERE source = 'seen' "
            "AND (last_seen < $1 OR (domain = ANY($2) AND last_seen < $3))",
            today - timedelta(days=RETENTION_DAYS),
            list(_AD_DOMAINS),
            today - timedelta(days=AD_RETENTION_DAYS),
        )


async def lookup(
    store: Candidates, fp: ImagePrint, where: Listing | None, thresholds: Thresholds = THRESHOLDS
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
    same = [c for c in same if c.domain not in _AD_DOMAINS]  # a shop's photo in someone's ad says nothing of the shop
    if where is not None:
        same = [c for c in same if c.domain != where.domain and c.listing_key != where.listing_key]
    per_domain: dict[str, StoredPrint] = {}
    for match in sorted(same, key=lambda m: (m.source != "seen", m.id)):
        per_domain.setdefault(match.domain, match)
    if len(per_domain) > STOP_DOMAINS:
        return Lookup([], stop=True)
    return Lookup(list(per_domain.values()))


async def lookup_ad(
    store: Candidates, fp: ImagePrint, where: Listing | None, thresholds: Thresholds = THRESHOLDS
) -> Lookup:
    """
    The other ads and shops showing the same photo as a peer-to-peer ad.

    Unlike a shop's photo, an ad's is compared with the other ads on its own platform (and on the others): the same
    photo in another ad is a copied ad, or a photo taken from someone else's. One match per other ad (ads first),
    then one per shop.

    :param store: The index.
    :param fp: The photo's fingerprints.
    :param where: The ad (it doesn't match itself).
    :param thresholds: When two photos are the same.
    :return: The matches; ``stop`` when the photo is on too many shops or ads to mean anything (a stock photo).
    """
    same = [c for c in await store.candidates(fp.phash, thresholds.phash) if same_photo(fp, c.print, thresholds)]
    if where is not None:
        same = [c for c in same if c.listing_key != where.listing_key]
    found: dict[tuple[str, bytes], StoredPrint] = {}
    for match in sorted(same, key=lambda m: (m.domain not in _AD_DOMAINS, m.source != "seen", m.id)):
        found.setdefault((match.domain, match.listing_key if is_ad(match) else b""), match)
    if len(found) > STOP_DOMAINS:
        return Lookup([], stop=True)
    return Lookup(list(found.values()))


def is_ad(match: StoredPrint) -> bool:
    """
    Whether a match is another peer-to-peer ad (rather than a shop or the catalogue).

    :param match: The match.
    :return: ``True`` for a photo seen on a peer-to-peer platform.
    """
    return match.domain in _AD_DOMAINS
