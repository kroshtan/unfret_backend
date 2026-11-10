"""
A cache for store-level facts: what is true of a shop or an image whoever asks.

That is RDAP data, the first certificate, a VAT check, or reverse-image results by image hash. Entries are
keyed by ``(kind, key)`` only: never by caller, page or listing, so the cache can't link anyone to what they
checked. Values must be JSON-serialisable. Misses (``None``) are cached for a shorter time so a flaky
upstream isn't hammered but recovers soon. The default store is in-process memory; the app swaps in Postgres at
startup (:func:`use_store`) so entries survive restarts and are shared between workers.
"""

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

logger = structlog.get_logger("unfret")

FACT_TTL_S = 7 * 24 * 3600
MISS_TTL_S = 24 * 3600


class FactStore(Protocol):
    """Where cached facts live."""

    async def get(self, kind: str, key: str) -> tuple[bool, Any]:
        """
        Look an entry up.

        :param kind: The fact kind, e.g. ``"rdap"``.
        :param key: The fact key, e.g. a domain.
        :return: ``(found, value)``; expired entries count as not found.
        """
        ...

    async def put(self, kind: str, key: str, value: Any, ttl_s: float) -> None:
        """
        Store an entry.

        :param kind: The fact kind.
        :param key: The fact key.
        :param value: A JSON-serialisable value (``None`` records a miss).
        :param ttl_s: Seconds until the entry expires.
        """
        ...


class MemoryStore:
    """A bounded in-process LRU store with expiry."""

    def __init__(self, max_entries: int = 10_000) -> None:
        """
        Create an empty store.

        :param max_entries: Oldest entries are evicted beyond this size.
        """
        self._entries: OrderedDict[tuple[str, str], tuple[float, Any]] = OrderedDict()
        self._max = max_entries

    async def get(self, kind: str, key: str) -> tuple[bool, Any]:
        """
        Look an entry up.

        :param kind: The fact kind.
        :param key: The fact key.
        :return: ``(found, value)``.
        """
        entry = self._entries.get((kind, key))
        if entry is None or entry[0] < time.monotonic():
            self._entries.pop((kind, key), None)
            return False, None
        self._entries.move_to_end((kind, key))
        return True, entry[1]

    async def put(self, kind: str, key: str, value: Any, ttl_s: float) -> None:
        """
        Store an entry.

        :param kind: The fact kind.
        :param key: The fact key.
        :param value: The value.
        :param ttl_s: Seconds until it expires.
        """
        self._entries[(kind, key)] = (time.monotonic() + ttl_s, value)
        self._entries.move_to_end((kind, key))
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)


_store: FactStore = MemoryStore()


def use_store(store: FactStore) -> None:
    """
    Replace the process-wide store (the app does this at startup when a database is available).

    :param store: The store to use from now on.
    """
    global _store  # noqa: PLW0603 - one process-wide cache, swapped once at startup
    _store = store


async def cached_fact[T](kind: str, key: str, load: Callable[[], Awaitable[T | None]]) -> T | None:
    """
    Return a cached fact, loading and caching it on a miss.

    A failing store never fails the analysis: it is logged and the fact is loaded directly.

    :param kind: The fact kind, e.g. ``"rdap"``.
    :param key: The fact key, e.g. a domain or an image hash.
    :param load: Loads the fact; returns ``None`` when there is none.
    :return: The fact, or ``None``.
    """
    try:
        found, value = await _store.get(kind, key)
        if found:
            return value  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        logger.warning("fact_cache_read_failed", kind=kind)
    value = await load()
    try:
        await _store.put(kind, key, value, FACT_TTL_S if value is not None else MISS_TTL_S)
    except Exception:  # noqa: BLE001
        logger.warning("fact_cache_write_failed", kind=kind)
    return value


async def peek(kind: str, key: str) -> Any:
    """
    A cached fact without loading it on a miss (e.g. to record what an analysis already looked up).

    :param kind: The fact kind.
    :param key: The fact key.
    :return: The value, or ``None`` when absent, expired or unreadable.
    """
    try:
        _, value = await _store.get(kind, key)
    except Exception:  # noqa: BLE001
        logger.warning("fact_cache_read_failed", kind=kind)
        return None
    return value
