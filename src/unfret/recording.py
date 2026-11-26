"""
What a quick check records once it has answered: the page's photo, price and reviews, as a deep check would.

A quick check reads only the page and looks nothing up. After its answer (never before, and never changing it), the
same rows a deep check would write from that page go into Unfret's own stores, through the stores' own record paths:
the main product photo's fingerprints into the image index (:func:`unfret.graph.skills.reverse_image.record_photo`),
today's price into the price history and the market prices (:func:`unfret.price_store.record`,
:func:`unfret.market_prices.record`) and the review fingerprints (:func:`unfret.review_store.record`). Only product
pages; never the test shop; off with ``price_observations`` and ``fingerprints`` as for deep checks.

Each row carries the check's provenance (:mod:`unfret.observations`): a quick check of a page Unfret fetched counts
at once, client-supplied content only once corroborated. The work is bounded: at most ``quick_record_queue`` pages
are recorded at a time (more are skipped, nothing waits), the photo is fetched with the photo check's own size and
time limits, at most :data:`PHOTOS_AT_ONCE` are fingerprinted at a time, and each page gets :data:`TIMEOUT_S`.

The counts in :data:`stats` (since the process started; no page, caller or time) are what ``GET
/v1/admin/observations`` shows.
"""

import asyncio
import weakref
from collections import Counter
from typing import Any

import structlog

from unfret import background, market_prices, observations, price_store, review_store
from unfret.config import settings
from unfret.graph.state import PRODUCT_TYPES
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

PHOTOS_AT_ONCE = 2
TIMEOUT_S = 20.0

stats: Counter[str] = Counter()
_pending: set[asyncio.Task[None]] = set()
_photo_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if (slots := _photo_slots.get(loop)) is None:
        slots = _photo_slots[loop] = asyncio.Semaphore(PHOTOS_AT_ONCE)
    return slots


async def record(state: dict[str, Any]) -> None:
    """
    Record a finished quick check's page. Never raises.

    :param state: The final analysis state (``url``, ``facts``, ``page_type``).
    """
    from unfret.graph.skills import reverse_image  # noqa: PLC0415 - the graph package imports this module

    url, facts, page_type = str(state.get("url") or ""), state.get("facts"), str(state.get("page_type") or "")
    try:
        async with asyncio.timeout(TIMEOUT_S):
            if settings.price_observations:
                stats["prices"] += await price_store.record(url, facts, page_type)  # type: ignore[arg-type]
                stats["market_prices"] += await market_prices.record(url, facts)  # type: ignore[arg-type]
            if settings.fingerprints:
                stats["reviews"] += await review_store.record(url, facts)  # type: ignore[arg-type]
                async with _slots():
                    stats["photos"] += await reverse_image.record_photo(url, facts, page_type)  # type: ignore[arg-type]
        stats["pages"] += 1
    except TimeoutError:
        stats["timed_out"] += 1
    except Exception as exc:  # noqa: BLE001 - optional, after the answer
        stats["failed"] += 1
        logger.warning("quick_record_failed", error=type(exc).__name__)


def record_later(state: dict[str, Any]) -> None:
    """
    Record a quick check's page in the background, after its answer; skipped when ``quick_record_queue`` are running.

    The task inherits the check's provenance (:func:`unfret.observations.current`), as every background write does.

    :param state: The final analysis state.
    """
    if state.get("page_type") not in PRODUCT_TYPES or state.get("facts") is None:
        return
    if is_test_shop(str(state.get("url") or "")) or not (settings.price_observations or settings.fingerprints):
        return
    if not observations.current().record:
        stats["over_limit"] += 1
        return
    if len(_pending) >= settings.quick_record_queue:
        stats["queue_full"] += 1
        return
    task = asyncio.create_task(record(dict(state)))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for background writes (tests and shutdown)."""
    await background.drain(_pending)
