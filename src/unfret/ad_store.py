"""
The ad store: fingerprints of peer-to-peer ads, to spot the same ad text, or the same phone number, behind many ads.

Private sellers are people, so this keeps as little as works (the decided minimum):

- per ad: the platform, a one-way hash of the listing (the platform and the ad's path, as the image index keys a
  listing), a 64-bit simhash of the ad's normalised words (word shingles hashed and folded: similar texts give
  nearby hashes, and no text can be read back from one), and the last day it was seen;
- per contact detail written in an ad (a phone number or e-mail address): only its keyed hash (HMAC-SHA256 with a
  server secret, the same as the shop fingerprint store's, :func:`unfret.shop_store.contact_key`), with the
  listing's hash and the day, to link one number behind many ads. Never stored without the key.

No seller, name, account, location, price, text, URL, caller, score or verdict. Everything is deleted 90 days after
it was last seen (:data:`RETENTION_DAYS`), and a contact can be removed on request (:func:`forget_contact`).
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import asyncpg
import structlog

from unfret.fingerprint import normalise_phone

logger = structlog.get_logger("unfret")

RETENTION_DAYS = 90
SIMHASH_DISTANCE = 10  # bits of 64: a copy with a line changed is within it, unrelated ads differ by ~32
MIN_WORDS = 12  # shorter texts ("Zo goed als nieuw, ophalen") say nothing about a copied ad
_SHINGLE = 3
_WORD_RE = re.compile(r"[a-z]+")  # words only: prices, sizes and phone numbers change between copies
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MASK = (1 << 64) - 1


def comparable_phone(raw: str) -> str | None:
    """
    A phone number in the form its hash is stored in, so the same number matches however an ad writes it.

    Dutch and Belgian mobile numbers written nationally get their country code ("06-12345678" → "+31612345678").

    :param raw: The number as written.
    :return: ``+`` and digits, digits only for other national numbers, or ``None`` when it isn't one.
    """
    number = normalise_phone(raw)
    if number is None:
        return None
    if number.startswith("00"):
        return "+" + number[2:]
    if len(number) == 10 and number.startswith("06"):
        return "+31" + number[1:]
    if len(number) == 10 and number.startswith("04"):
        return "+32" + number[1:]
    return number


def listing_key(platform: str, url: str) -> bytes:
    """
    The stored key of an ad: SHA-256 of the platform and the ad's path (no host, query or fragment).

    :param platform: The platform (``marktplaats``, …).
    :param url: The ad's URL.
    :return: 32 bytes.
    """
    path = urlsplit(url).path.rstrip("/").lower()
    return hashlib.sha256(f"{platform}|{path}".encode()).digest()


def _words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    return _WORD_RE.findall(_URL_RE.sub(" ", folded))


def simhash(text: str) -> int | None:
    """
    A 64-bit simhash of a text's three-word shingles, as a signed integer (Postgres ``BIGINT``).

    :param text: The ad text.
    :return: The hash, or ``None`` for a text under :data:`MIN_WORDS` words.
    """
    words = _words(text)
    if len(words) < MIN_WORDS:
        return None
    weights = [0] * 64
    for i in range(len(words) - _SHINGLE + 1):
        digest = hashlib.blake2b(" ".join(words[i : i + _SHINGLE]).encode(), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(64):
            weights[bit] += 1 if value >> bit & 1 else -1
    folded = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return folded - (1 << 64) if folded >= 1 << 63 else folded


def distance(a: int, b: int) -> int:
    """
    The Hamming distance between two simhashes.

    :param a: One hash.
    :param b: The other.
    :return: Differing bits.
    """
    return ((a ^ b) & _MASK).bit_count()


@dataclass(frozen=True)
class AdPrint:
    """One ad as stored."""

    platform: str
    listing_key: bytes
    simhash: int | None
    contacts: tuple[bytes, ...] = ()  # keyed hashes


@dataclass(frozen=True)
class Seen:
    """What the store knows of an ad's text and contacts from other ads in the retention window."""

    text_copies: dict[str, int]  # platform → other ads with the same text
    contact_ads: int  # other ads with one of the same contact details

    @property
    def copies(self) -> int:
        """
        Other ads with the same text, on all platforms.

        :return: The count.
        """
        return sum(self.text_copies.values())


class AdStore(Protocol):
    """Where ad fingerprints live: Postgres in production, memory in tests."""

    async def lookup(self, ad: AdPrint, since: date) -> Seen:
        """
        Other ads (not this listing) seen since a day with a close text hash or a shared contact.

        :param ad: The ad.
        :param since: The first day that counts.
        :return: What was seen.
        """

    async def record(self, ad: AdPrint, day: date) -> None:
        """
        Store or refresh an ad and its contacts.

        :param ad: The ad.
        :param day: Today.
        """

    async def purge(self, before: date) -> None:
        """
        Delete what was last seen before a day.

        :param before: The first day to keep.
        """

    async def forget_contact(self, contact_hash: bytes) -> int:
        """
        Delete a contact's hash wherever it is stored (an erasure request).

        :param contact_hash: The keyed hash.
        :return: How many rows went.
        """


class MemoryAdStore:
    """In-process store, for tests and runs without a database."""

    def __init__(self) -> None:
        """Start empty."""
        self.ads: dict[tuple[str, bytes], tuple[int | None, date]] = {}
        self.contacts: dict[tuple[bytes, bytes], date] = {}

    async def lookup(self, ad: AdPrint, since: date) -> Seen:
        """
        Linear scan.

        :param ad: The ad.
        :param since: The first day that counts.
        :return: What was seen.
        """
        copies: dict[str, int] = {}
        if ad.simhash is not None:
            for (platform, key), (other, day) in self.ads.items():
                close = other is not None and distance(ad.simhash, other) <= SIMHASH_DISTANCE
                if key != ad.listing_key and day >= since and close:
                    copies[platform] = copies.get(platform, 0) + 1
        wanted = set(ad.contacts)
        others = {
            key for (c, key), day in self.contacts.items() if c in wanted and key != ad.listing_key and day >= since
        }
        return Seen(copies, len(others))

    async def record(self, ad: AdPrint, day: date) -> None:
        """
        Store or refresh.

        :param ad: The ad.
        :param day: Today.
        """
        self.ads[(ad.platform, ad.listing_key)] = (ad.simhash, day)
        for contact in ad.contacts:
            self.contacts[(contact, ad.listing_key)] = day

    async def purge(self, before: date) -> None:
        """
        Apply the retention limit.

        :param before: The first day to keep.
        """
        self.ads = {k: v for k, v in self.ads.items() if v[1] >= before}
        self.contacts = {k: d for k, d in self.contacts.items() if d >= before}

    async def forget_contact(self, contact_hash: bytes) -> int:
        """
        Drop a contact.

        :param contact_hash: The keyed hash.
        :return: How many rows went.
        """
        gone = [k for k in self.contacts if k[0] == contact_hash]
        for k in gone:
            del self.contacts[k]
        return len(gone)


class PostgresAdStore:
    """Ad fingerprints in the ``ad_prints`` and ``ad_contacts`` tables (migration 20)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def lookup(self, ad: AdPrint, since: date) -> Seen:
        """
        A popcount scan of the text hashes and an index lookup of the contacts.

        :param ad: The ad.
        :param since: The first day that counts.
        :return: What was seen.
        """
        copies: dict[str, int] = {}
        if ad.simhash is not None:
            rows: list[Any] = await self.pool.fetch(
                "SELECT platform, COUNT(*) AS n FROM ad_prints WHERE simhash IS NOT NULL AND listing_key <> $2 "
                "AND last_seen >= $3 AND bit_count((simhash # $1)::bit(64)) <= $4 GROUP BY platform",
                ad.simhash,
                ad.listing_key,
                since,
                SIMHASH_DISTANCE,
            )
            copies = {r["platform"]: int(r["n"]) for r in rows}
        contact_ads = 0
        if ad.contacts:
            contact_ads = int(
                await self.pool.fetchval(
                    "SELECT COUNT(DISTINCT listing_key) FROM ad_contacts WHERE contact_hash = ANY($1::bytea[]) "
                    "AND listing_key <> $2 AND last_seen >= $3",
                    list(ad.contacts),
                    ad.listing_key,
                    since,
                )
                or 0
            )
        return Seen(copies, contact_ads)

    async def record(self, ad: AdPrint, day: date) -> None:
        """
        Upsert the ad and its contacts in one transaction.

        :param ad: The ad.
        :param day: Today.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO ad_prints (platform, listing_key, simhash, last_seen) VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (platform, listing_key) DO UPDATE SET simhash = EXCLUDED.simhash, "
                "last_seen = EXCLUDED.last_seen",
                ad.platform,
                ad.listing_key,
                ad.simhash,
                day,
            )
            if ad.contacts:
                await conn.executemany(
                    "INSERT INTO ad_contacts (contact_hash, listing_key, last_seen) VALUES ($1, $2, $3) "
                    "ON CONFLICT (contact_hash, listing_key) DO UPDATE SET last_seen = EXCLUDED.last_seen",
                    [(c, ad.listing_key, day) for c in ad.contacts],
                )

    async def purge(self, before: date) -> None:
        """
        Delete expired rows.

        :param before: The first day to keep.
        """
        await self.pool.execute("DELETE FROM ad_prints WHERE last_seen < $1", before)
        await self.pool.execute("DELETE FROM ad_contacts WHERE last_seen < $1", before)

    async def forget_contact(self, contact_hash: bytes) -> int:
        """
        Delete a contact's rows.

        :param contact_hash: The keyed hash.
        :return: How many rows went.
        """
        status: str = await self.pool.execute("DELETE FROM ad_contacts WHERE contact_hash = $1", contact_hash)
        return int(status.rsplit(" ", 1)[-1])


_store: AdStore = MemoryAdStore()
_purged_on: date | None = None


def use_store(store: AdStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresAdStore` at startup).

    :param store: The store.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


def store() -> AdStore:
    """
    The store in use.

    :return: The store.
    """
    return _store


async def seen(ad: AdPrint, today: date) -> Seen | None:
    """
    Look an ad up in the retention window. Never raises: a failing store costs the signal, not the analysis.

    :param ad: The ad.
    :param today: Today.
    :return: What was seen, or ``None`` when the store failed.
    """
    try:
        return await _store.lookup(ad, today - timedelta(days=RETENTION_DAYS))
    except Exception as exc:  # noqa: BLE001 - optional signal
        logger.warning("ad_store_failed", error=type(exc).__name__)
        return None


async def record(ad: AdPrint, today: date) -> None:
    """
    Record an ad, and purge expired rows at most once a day per process. Never raises.

    :param ad: The ad.
    :param today: Today.
    """
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    try:
        await _store.record(ad, today)
        if _purged_on != today:
            _purged_on = today
            await _store.purge(today - timedelta(days=RETENTION_DAYS))
    except Exception as exc:  # noqa: BLE001 - optional, after the lookup
        logger.warning("ad_store_failed", error=type(exc).__name__)
