"""
Where an observation in the photo, price and review stores came from, and when it may count in a verdict.

Every analysis can feed Unfret's own stores: the main product photo's fingerprints (:mod:`unfret.image_store`), the
price (:mod:`unfret.price_store`, :mod:`unfret.market_prices`) and the review fingerprints
(:mod:`unfret.review_store`). Quick checks are free and anonymous, and the extension and app send page content that
Unfret didn't fetch itself, so anyone could plant a price or claim a competitor's photo is on other shops. Hence:

- **Provenance.** Each row keeps a small bit set of where it was seen: on a page Unfret fetched itself, in content a
  client supplied, by a quick or a deep check (rows from before this was kept are marked as such and count as
  before). Nothing that names the caller, their pass or their network.
- **Corroboration.** A row counts in a verdict (the reused photo, the far-below-market price, the "was" price never
  charged, the copied reviews) only when a page Unfret fetched itself showed it, or when at least
  ``observation_min_sources`` independent sources reported the same thing. A source is a pass and a network:
  another report from the same pass or the same network doesn't add one. Rows that don't count yet are kept (they
  can be corroborated later) but never shown or used.
- **No link from a pass to a page.** The row stores only a count of sources. Which sources already vouched for which
  row is held in memory only, for the current UTC day, as keyed hashes under a random salt that is replaced (with the
  set) every day and never written anywhere. A restart forgets it too. So neither the database nor a log can tell
  which pass or network reported which page; a source that reports the same thing on another day counts again.
- **Write limits.** Each network may record observations from at most ``observation_pages_per_network_per_day``
  quick checks or client-supplied pages per day (in memory, salted hashes, like the other limits); past it the check
  answers as always and records nothing.

The routes say where a page came from (:func:`for_request`, :func:`observed_as`); the stores read it here
(:func:`current`) while they record. Without it (evaluation runs, tests, internal callers) an observation counts as
one from a page Unfret fetched itself, as every observation did before.
"""

import asyncio
import contextlib
import contextvars
import hashlib
import secrets
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from unfret.config import settings
from unfret.visitors import network_of

# Provenance bits, OR-ed into a row each time it is seen.
FETCHED = 1  # on a page Unfret fetched itself
CLIENT = 2  # in content a client (the extension, the app, an API caller) supplied
QUICK = 4  # recorded after a quick check
DEEP = 8  # recorded by a deep check
LEGACY = 16  # recorded before provenance was kept (counts, as it did then)
# A row seen like this counts at once; any other needs corroboration.
TRUSTED_BITS = FETCHED | LEGACY
# What a store writes when its caller doesn't say (the column default too): a page Unfret fetched, a deep check.
DEFAULT_BITS = FETCHED | DEEP
MAX_SOURCES = 32767  # the column is a SMALLINT
_MAX_WITNESSES = 200_000  # (source, row) marks kept per day; past it nothing more is counted until tomorrow
_MAX_NETWORKS = 100_000  # networks counted per day for the write limit; past it new ones record nothing

Tier = Literal["quick", "deep"]


@dataclass(frozen=True)
class Provenance:
    """Where the page an analysis records from came from, and who sent it (as today's keyed hashes only)."""

    fetched: bool = True
    tier: Tier = "deep"
    sources: tuple[bytes, ...] = ()  # the network's and the pass's keyed hashes for today; never stored
    record: bool = True  # False past the network's daily write limit: look up, record nothing

    @property
    def bits(self) -> int:
        """
        The row's provenance bits for this observation.

        :return: ``FETCHED`` or ``CLIENT``, with ``QUICK`` or ``DEEP``.
        """
        return (FETCHED if self.fetched else CLIENT) | (QUICK if self.tier == "quick" else DEEP)


# What every observation was before provenance was kept: a page Unfret fetched itself, by a deep check.
TRUSTED = Provenance()
_current: ContextVar[Provenance] = ContextVar("unfret_provenance", default=TRUSTED)


def current() -> Provenance:
    """
    The provenance of the analysis running in this context.

    :return: What the route set with :func:`observed_as`, else :data:`TRUSTED`.
    """
    return _current.get()


@contextlib.contextmanager
def observed_as(provenance: Provenance) -> Iterator[None]:
    """
    Run an analysis (and the background writes it starts) with this provenance.

    :param provenance: From :func:`for_request`.
    :yield: Nothing.
    """
    token = _current.set(provenance)
    try:
        yield
    finally:
        _current.reset(token)


def counts(bits: int, sources: int) -> bool:
    """
    Whether a stored row may count in a verdict.

    :param bits: The row's provenance bits.
    :param sources: How many independent sources reported it.
    :return: ``True`` for a row seen on a page Unfret fetched (or kept from before), or corroborated.
    """
    return bool(bits & TRUSTED_BITS) or sources >= settings.observation_min_sources


def trusted_params() -> tuple[int, int]:
    """
    The parameters of :func:`counts` in a store's SQL: ``(provenance & $a <> 0 OR sources >= $b)``.

    :return: ``(TRUSTED_BITS, observation_min_sources)``.
    """
    return TRUSTED_BITS, settings.observation_min_sources


@dataclass(frozen=True)
class Kept:
    """A stored row's provenance."""

    bits: int
    sources: int


Action = Literal["insert", "replace", "vouch", "keep"]


def merge(old: Kept | None, bits: int, vouch: int, *, same: bool, newer_wins: bool) -> tuple[Action, Kept]:
    """
    What a new observation does to the row it is about.

    The same value adds the observation's provenance and, from a new source, one source. A different value replaces
    the row when it comes from a page Unfret fetched and the row doesn't count yet, or, where the newer value wins
    (a listing's photo), whenever the row doesn't count or the new one is fetched. A different value that doesn't
    count never replaces one that does: a forged page can't overwrite a corroborated photo or price.

    :param old: The row's provenance, or ``None`` when there is no row yet.
    :param bits: The observation's provenance bits.
    :param vouch: 1 when a new source reports it (:meth:`Witnesses.vouch`), else 0.
    :param same: Whether the observation shows the row's value (the same photo, price and currency).
    :param newer_wins: Whether a different value replaces the old one (photos) or the first one stays (prices).
    :return: The action and the row's provenance afterwards.
    """
    if old is None:
        return "insert", Kept(bits, vouch)
    if same:
        return "vouch", Kept(old.bits | bits, min(old.sources + vouch, MAX_SOURCES))
    trusted, old_counts = bool(bits & TRUSTED_BITS), counts(old.bits, old.sources)
    if old_counts and not trusted:
        return "keep", old
    if newer_wins or (trusted and not old_counts):
        return "replace", Kept(bits, vouch)
    return "keep", old


def _today() -> date:
    return datetime.now(tz=UTC).date()


class Witnesses:
    """
    Today's sources per observation, as keyed hashes in memory, so a source counts once per row per day.

    Nothing here is persisted: the salt is random and replaced every UTC day, and the marks go with it.
    """

    def __init__(self, cap: int = _MAX_WITNESSES) -> None:
        """
        Start empty.

        :param cap: The most marks kept in a day.
        """
        self.cap = cap
        self._day: date | None = None
        self._salt = b""
        self._marks: set[int] = set()

    def _roll(self) -> bytes:
        day = _today()
        if day != self._day:
            self._day, self._salt, self._marks = day, secrets.token_bytes(16), set()
        return self._salt

    def source(self, kind: str, value: str) -> bytes:
        """
        Today's keyed hash of a source.

        :param kind: ``network`` or ``pass``.
        :param value: The network (never stored) or the account.
        :return: 16 bytes; tomorrow the same source gets another.
        """
        salt = self._roll()
        return hashlib.blake2b(f"{kind}:{value}".encode(), key=salt, digest_size=16, person=b"unfret-source").digest()

    def vouch(self, provenance: Provenance, row: bytes) -> int:
        """
        Whether this observation's sources add one to a row's count: none of them reported that row today.

        :param provenance: The observation's provenance (its sources).
        :param row: What identifies the row (a store's own key; hashed again here, never kept as is).
        :return: 1 for a new source, else 0 (also without sources, and past the day's cap).
        """
        salt = self._roll()
        if not provenance.sources:
            return 0
        marks = {
            int.from_bytes(hashlib.blake2b(s + row, key=salt, digest_size=8, person=b"unfret-witness").digest())
            for s in provenance.sources
        }
        fresh = not marks & self._marks
        if len(self._marks) + len(marks) > self.cap:
            return 0
        self._marks |= marks
        return 1 if fresh else 0


class WriteLimit:
    """At most ``limit`` recorded pages per network per UTC day; in memory, keys hashed with the daily salt."""

    def __init__(self, witnesses: Witnesses) -> None:
        """
        Count with the witnesses' daily salt.

        :param witnesses: Where the salt comes from.
        """
        self._witnesses = witnesses
        self._day: date | None = None
        self._counts: dict[bytes, int] = {}

    def allow(self, network: bytes, limit: int) -> bool:
        """
        Count a recorded page for a network, unless it is over today's limit.

        :param network: The network's keyed hash for today (:meth:`Witnesses.source`).
        :param limit: Pages per network per day.
        :return: Whether the page may record.
        """
        day = _today()
        if day != self._day:
            self._day, self._counts = day, {}
        used = self._counts.get(network)
        if used is None and len(self._counts) >= _MAX_NETWORKS:
            return False
        if (used or 0) >= limit:
            return False
        self._counts[network] = (used or 0) + 1
        return True


witnesses = Witnesses()
writes = WriteLimit(witnesses)


def for_request(ip: str, account: str | None, *, fetched: bool, tier: Tier) -> Provenance:
    """
    The provenance of a check, counted against the network's daily write limit when it needs corroboration.

    A deep check of a page Unfret fetched records as before, without a limit. A quick check, or a page whose content
    the caller supplied, records only within ``observation_pages_per_network_per_day``.

    :param ip: The client address (only its network is used, hashed with today's salt; never stored).
    :param account: The pass or wallet, if any (hashed the same way).
    :param fetched: Whether Unfret fetched the page itself.
    :param tier: ``quick`` or ``deep``.
    :return: The provenance.
    """
    network = witnesses.source("network", network_of(ip))
    sources = (network,) + ((witnesses.source("pass", account),) if account else ())
    record = (fetched and tier == "deep") or writes.allow(network, settings.observation_pages_per_network_per_day)
    return Provenance(fetched=fetched, tier=tier, sources=sources, record=record)


def vouch(row: bytes) -> int:
    """
    :meth:`Witnesses.vouch` for the analysis running in this context.

    :param row: What identifies the row.
    :return: 1 for a new source, else 0.
    """
    return witnesses.vouch(current(), row)


async def stream_as[T](provenance: Provenance, stream: AsyncIterator[T]) -> AsyncIterator[T]:
    """
    Iterate an analysis's stream with this provenance.

    Each step runs in a context of its own: an async generator can't set a context variable for the steps it drives
    and reset it safely.

    :param provenance: From :func:`for_request`.
    :param stream: The graph's stream (not started yet).
    :yield: Its items.
    """
    context = contextvars.copy_context()
    context.run(_current.set, provenance)

    async def step() -> T:
        return await anext(stream)

    try:
        while True:
            try:
                item = await asyncio.create_task(step(), context=context)
            except StopAsyncIteration:
                return
            yield item
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await asyncio.create_task(close(), context=context)
