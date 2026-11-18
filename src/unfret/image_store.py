"""
Where the image index lives, and how a photo is looked up in it.

Each row is one listing's main photo: the shop (its registrable domain, or the marketplace for Amazon and bol.com
listings), a hash of the listing (the shop and its public product ID, or the page path), the photo's fingerprints
(:mod:`unfret.image_index`), the photo's own address, for thumbnails, and the brand the listing names (``label``:
the page's brand for analysis rows, as :func:`unfret.brands.label` writes it, none when the page names none; the
catalogue's brand for seed rows). Rows come from analyses (``seen``) and from the public ABO catalogue (``abo``, see
:mod:`unfret.image_seed`). As in the shop store: days not times, and no caller, page address, score or verdict.

A lookup finds rows whose pHash is close (in SQL), keeps those whose dHash, embedding and colours agree as well, and
drops the photo's own shop (a shop reusing its photos is normal) and the same listing (a listing that changes is an
update, not a duplicate). A photo found on more than :data:`STOP_DOMAINS` shops is a stock image, logo or
placeholder, not evidence: it matches nothing. Matches are direct (photo to photo), never chained.

Each analysis row also keeps where it was seen and how many independent sources reported that photo for that listing
(:mod:`unfret.observations`). Lookups see only rows seen on a page Unfret fetched itself, or corroborated (and the
catalogue): a forged page that puts another shop's photo on made-up shops neither matches it nor makes it a stock
photo, and never replaces a listing's photo that counts. The brand follows the photo: whoever may replace the photo
sets it, and a report that doesn't count can't change a brand a fetched page gave (two reports that disagree leave
the brand unknown, never a third name).

On a marketplace (one shop family, see :data:`_FAMILIES`) the lookup also returns the same photo on the family's
other listings that name a brand (:attr:`Lookup.siblings`): the same photo under another brand on Amazon says
nothing of the shop, but a lot of the product (:mod:`unfret.graph.skills.reverse_image`).
"""

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import asyncpg

from unfret import brands, observations
from unfret.facts import PageFacts
from unfret.image_index import THRESHOLDS, ImagePrint, Thresholds, hamming, same_photo
from unfret.net import hostname
from unfret.observations import Kept
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
_MARKETPLACES = frozenset(_FAMILIES.values())


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
    label: str | None = None  # the brand the listing names (seed rows: the catalogue's), when known


@dataclass(frozen=True)
class Lookup:
    """What the index knows of a photo."""

    matches: list[StoredPrint]  # other shops' and listings' copies, one per shop, seed rows last
    stop: bool = False  # the photo is everywhere: a stock image, not evidence
    # The same photo on other listings of the page's own marketplace that name a brand (one per brand), for
    # comparing brands only: never counted as another shop.
    siblings: list[StoredPrint] = field(default_factory=list)


def label_after(
    action: observations.Action, old: str | None, new: str | None, *, old_bits: int, bits: int
) -> str | None:
    """
    The brand a row keeps after an observation (:func:`unfret.observations.merge` decided what it does to the photo).

    A new or replaced photo takes the observation's brand; the same photo seen on a page Unfret fetched takes it too
    (or keeps the row's when the page names none). A report that doesn't count never changes the brand of a row a
    fetched page gave (nor gives it one); on a row that doesn't count either it fills in a missing brand, and when it
    names another brand the brand becomes unknown.

    :param action: What the observation does to the row.
    :param old: The row's brand.
    :param new: The observation's brand.
    :param old_bits: The row's provenance before the observation.
    :param bits: The observation's provenance.
    :return: The brand to keep.
    """
    if action in {"insert", "replace"}:
        return new
    if action == "keep":
        return old
    if bits & observations.TRUSTED_BITS:
        return new or old
    if new is None or brands.same_brand(old, new) or old_bits & observations.TRUSTED_BITS:
        return old
    return new if old is None else None


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
        :return: Up to a few hundred rows (catalogue rows, and analysis rows that count).
        """


class ImageStore(Candidates, Protocol):
    """Where the index lives: Postgres in production, memory in tests."""

    async def upsert(
        self,
        where: Listing,
        fp: ImagePrint,
        *,
        image_url: str,
        day: date,
        source: Source,
        label: str | None = None,
        bits: int = observations.DEFAULT_BITS,
        vouch: int = 0,
    ) -> int | None:
        """
        Store a listing's main photo, replacing the listing's previous one as :func:`unfret.observations.merge` says.

        The same photo adds the observation's provenance and source. A different photo replaces the old one, unless
        the old one counts and the new one doesn't (then nothing changes).

        :param where: The listing.
        :param fp: The photo's fingerprints.
        :param image_url: The photo's address.
        :param day: Today.
        :param source: ``seen`` or ``abo``.
        :param label: The brand the listing names (:func:`unfret.brands.label`; seed rows: the catalogue's).
        :param bits: The observation's provenance bits (:mod:`unfret.observations`).
        :param vouch: 1 when a new source reports it.
        :return: The row's ID (for its thumbnail), or ``None`` when the row kept another photo.
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
        self.kept: dict[tuple[str, bytes], Kept] = {}  # each row's provenance (rows without one count)
        self._next = 1

    def _counts(self, row: StoredPrint) -> bool:
        kept = self.kept.get((row.domain, row.listing_key))
        return row.source != "seen" or kept is None or observations.counts(kept.bits, kept.sources)

    async def candidates(self, phash: int, max_distance: int) -> list[StoredPrint]:
        """
        Linear scan.

        :param phash: The query hash.
        :param max_distance: The largest distance.
        :return: The rows within it.
        """
        rows = [r for r, *_ in self.rows.values() if hamming(r.print.phash, phash) <= max_distance and self._counts(r)]
        return rows[:_CANDIDATES]

    async def upsert(
        self,
        where: Listing,
        fp: ImagePrint,
        *,
        image_url: str,
        day: date,
        source: Source,
        label: str | None = None,
        bits: int = observations.DEFAULT_BITS,
        vouch: int = 0,
    ) -> int | None:
        """
        Store or replace, merged as the Postgres store does.

        :param where: The listing.
        :param fp: The fingerprints.
        :param image_url: The address.
        :param day: Today.
        :param source: The source.
        :param label: The brand the listing names.
        :param bits: The provenance bits.
        :param vouch: 1 for a new source.
        :return: The row's ID, or ``None`` when it kept another photo.
        """
        key = (where.domain, where.listing_key)
        old = self.rows.get(key)
        before = (self.kept.get(key) or Kept(observations.LEGACY, 0)) if old else None
        same = old is not None and same_photo(fp, old[0].print)
        action, kept = observations.merge(before, bits, vouch, same=same, newer_wins=True)
        self.kept[key] = kept
        if old and action == "keep":
            return None
        brand = label_after(
            action, old[0].label if old else None, label, old_bits=before.bits if before else 0, bits=bits
        )
        if old and action == "vouch" and not bits & observations.TRUSTED_BITS:  # the same photo: keep the row's
            self.rows[key] = (replace(old[0], label=brand), old[1], old[2], day)
            return old[0].id
        row_id = old[0].id if old else self._next
        self._next += 0 if old else 1
        stored = StoredPrint(row_id, source, where.domain, where.listing_key, fp, brand)
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
        self.kept = {k: v for k, v in self.kept.items() if k in self.rows}


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
            "WHERE bit_count((phash # $1)::bit(64)) <= $2"
            " AND (source <> 'seen' OR provenance & $4 <> 0 OR sources >= $5) LIMIT $3",
            phash,
            max_distance,
            _CANDIDATES,
            *observations.trusted_params(),
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
        self,
        where: Listing,
        fp: ImagePrint,
        *,
        image_url: str,
        day: date,
        source: Source,
        label: str | None = None,
        bits: int = observations.DEFAULT_BITS,
        vouch: int = 0,
    ) -> int | None:
        """
        Insert, or merge into the listing's row (:func:`unfret.observations.merge`) and refresh its last-seen day.

        :param where: The listing.
        :param fp: The fingerprints.
        :param image_url: The address.
        :param day: Today.
        :param source: The source.
        :param label: The brand the listing names.
        :param bits: The provenance bits.
        :param vouch: 1 for a new source.
        :return: The row's ID, or ``None`` when it kept another photo.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row_id: int | None = await conn.fetchval(
                "INSERT INTO image_prints (source, domain, listing_key, phash, dhash, embedding, colour, image_url,"
                " label, first_seen, last_seen, provenance, sources)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $10, $11, $12) "
                "ON CONFLICT (domain, listing_key) DO NOTHING RETURNING id",
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
                bits,
                vouch,
            )
            if row_id is not None:
                return row_id
            old: Any = await conn.fetchrow(
                "SELECT id, phash, dhash, embedding, colour, provenance, sources, label FROM image_prints "
                "WHERE domain = $1 AND listing_key = $2 FOR UPDATE",
                where.domain,
                where.listing_key,
            )
            print_ = ImagePrint(old["phash"], old["dhash"], bytes(old["embedding"]), bytes(old["colour"]))
            action, kept = observations.merge(
                Kept(old["provenance"], old["sources"]), bits, vouch, same=same_photo(fp, print_), newer_wins=True
            )
            if action == "keep":
                return None
            brand = label_after(action, old["label"], label, old_bits=old["provenance"], bits=bits)
            if action == "vouch" and not bits & observations.TRUSTED_BITS:  # the same photo: keep the row's
                await conn.execute(
                    "UPDATE image_prints SET provenance = $2, sources = $3, last_seen = GREATEST(last_seen, $4),"
                    " label = $5 WHERE id = $1",
                    old["id"],
                    kept.bits,
                    kept.sources,
                    day,
                    brand,
                )
                return int(old["id"])
            await conn.execute(
                "UPDATE image_prints SET phash = $2, dhash = $3, embedding = $4, colour = $5, image_url = $6,"
                " last_seen = $7, provenance = $8, sources = $9, label = $10 WHERE id = $1",
                old["id"],
                fp.phash,
                fp.dhash,
                fp.embedding,
                fp.colour,
                image_url[:1000],
                day,
                kept.bits,
                kept.sources,
                brand,
            )
            return int(old["id"])

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
    siblings: dict[str, StoredPrint] = {}
    if where is not None:
        if where.domain in _MARKETPLACES:  # the same photo on the marketplace's other listings, one per brand named
            for match in sorted(same, key=lambda m: (m.source != "seen", m.id)):
                key = brands.brand_key(match.label)
                if match.domain == where.domain and match.listing_key != where.listing_key and key:
                    siblings.setdefault(key, match)
        same = [c for c in same if c.domain != where.domain and c.listing_key != where.listing_key]
    per_domain: dict[str, StoredPrint] = {}
    for match in sorted(same, key=lambda m: (m.source != "seen", m.id)):
        per_domain.setdefault(match.domain, match)
    if len(per_domain) > STOP_DOMAINS or len(siblings) > STOP_DOMAINS:
        return Lookup([], stop=True)
    return Lookup(list(per_domain.values()), siblings=list(siblings.values()))


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
