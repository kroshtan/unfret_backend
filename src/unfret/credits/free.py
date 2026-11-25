"""
A developer pass's free deep checks.

Shoppers need no pass: their quick and deep checks are free and anonymous (:mod:`unfret.routes.analyze`, with a proof
of work for a deep one, :mod:`unfret.deep_pow`). A pass's own free checks are a **developer pass's** only.

**Developer passes.** ``POST /v1/keys`` with ``"developer": true`` makes a pass with ``developer_free_checks`` (20)
free deep checks, kept as a count in ``api_keys.free_checks`` (nothing else: not who asked, nor from where), for
building an agent against ``/v1/check`` (which takes a pass like ``/v1/analyze`` does), the MCP server or the clients;
at most ``developer_passes_per_ip_per_day`` per visitor network, under a proof of work ``developer_pow_extra_bits``
harder (:class:`unfret.credits.keys.KeyGuard`). They count **only on** ``/v1/check`` (``api=True`` below). After them,
the pass pays from bought checks (packs), as before.

**The budget.** Every developer free deep check counts towards a global counter a UTC day (``usage_counters``
``developer_free_checks``: a number per day, nothing about anyone), capped at
:attr:`unfret.config.Settings.developer_free_daily_checks_cap`. Once it is reached, or once this month's LLM spend
reaches the ceiling (:mod:`unfret.spend`), a developer pass with only free checks is answered as an empty pass is
(``developer_paused``), saying when they are back. Bought checks and quick checks go on either way. A free check whose
analysis fails or can't be done is given back, to the pass and to the day's budget.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import asyncpg

from unfret import spend
from unfret.config import Settings

type Executor = asyncpg.Pool | asyncpg.Connection

DEVELOPER_COUNTER = "developer_free_checks"

Taken = Literal["taken", "none", "developer_paused"]


def today() -> str:
    """
    The budget's period: the UTC day.

    :return: ``YYYY-MM-DD``.
    """
    return datetime.now(UTC).date().isoformat()


def seconds_to_tomorrow(now: datetime | None = None) -> int:
    """
    How long until the day's budget starts over (00:00 UTC).

    :param now: The time (now by default).
    :return: Seconds, at least 1.
    """
    now = now or datetime.now(UTC)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    return max(1, int((tomorrow - now).total_seconds()))


@dataclass(frozen=True)
class Spent:
    """A free deep check taken from a pass and the day's budget, to give back on a refund."""

    period: str
    key_id: str
    counter: str = DEVELOPER_COUNTER


async def used_today(db: Executor) -> int:
    """
    The developer passes' free deep checks used today.

    :param db: A pool or connection.
    :return: The count.
    """
    count = await db.fetchval(
        "SELECT count FROM usage_counters WHERE name = $1 AND period = $2", DEVELOPER_COUNTER, today()
    )
    return int(count or 0)


async def paused(db: Executor, settings: Settings) -> bool:
    """
    Whether developer passes' free deep checks are paused: today's budget is spent, or the month's LLM spend ceiling.

    :param db: A pool or connection.
    :param settings: The service settings.
    :return: ``True`` while they are.
    """
    cap = settings.developer_free_daily_checks_cap
    if cap <= 0 or await used_today(db) >= cap:
        return True
    return (await spend.ceiling(db, settings)).reached


async def back_in_s(db: Executor, settings: Settings) -> int:
    """
    When paused developer free checks are back: when the free LLM budget is (spend), or 00:00 UTC.

    :param db: A pool or connection.
    :param settings: The service settings.
    :return: Seconds.
    """
    standing = await spend.ceiling(db, settings)
    return standing.retry_after_s() if standing.reached else seconds_to_tomorrow()


async def take_for_key(
    pool: asyncpg.Pool, settings: Settings, key_id: str, *, api: bool = False
) -> tuple[Taken, Spent | None]:
    """
    Take one of a developer pass's free deep checks, and one from the day's budget, together.

    :param pool: The pool.
    :param settings: The service settings.
    :param key_id: The pass's id.
    :param api: The check is on ``/v1/check``, the only place a developer pass's free checks count.
    :return: ``("taken", spent)``; ``("none", None)`` without free checks that count here; ``("developer_paused",
        None)`` when the pass has some but they are paused (nothing is taken then).
    """
    if not api:
        return "none", None
    period = today()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT free_checks, developer FROM api_keys WHERE id = $1 FOR UPDATE", key_id)
        if row is None or not row["free_checks"] or not row["developer"]:
            return "none", None
        if (await spend.ceiling(conn, settings)).reached:
            return "developer_paused", None
        cap = settings.developer_free_daily_checks_cap
        # One statement: concurrent checks can't both take the budget's last check.
        taken = cap > 0 and await conn.fetchval(
            "INSERT INTO usage_counters (name, period, count) VALUES ($1, $2, 1) ON CONFLICT (name, period) "
            "DO UPDATE SET count = usage_counters.count + 1 WHERE usage_counters.count < $3 RETURNING count",
            DEVELOPER_COUNTER,
            period,
            cap,
        )
        if not taken:
            return "developer_paused", None
        await conn.execute("UPDATE api_keys SET free_checks = free_checks - 1 WHERE id = $1", key_id)
    return "taken", Spent(period, key_id)


async def give_back(db: Executor, spent: Spent) -> None:
    """
    Undo a free deep check that didn't happen (a failed or unreadable analysis): back to the pass and its budget.

    :param db: A pool or connection.
    :param spent: What was taken.
    """
    await db.execute(
        "UPDATE usage_counters SET count = GREATEST(count - 1, 0) WHERE name = $1 AND period = $2",
        spent.counter,
        spent.period,
    )
    await db.execute("UPDATE api_keys SET free_checks = free_checks + 1 WHERE id = $1", spent.key_id)


async def left(db: Executor, key_id: str, *, api: bool = False) -> int:
    """
    A developer pass's free deep checks left that count where the check is made.

    :param db: A pool or connection.
    :param key_id: The pass's id.
    :param api: On ``/v1/check``; elsewhere a pass has none.
    :return: The count (0 for an unknown id, a pass that isn't a developer pass, or off the API).
    """
    if not api:
        return 0
    row = await db.fetchrow("SELECT free_checks, developer FROM api_keys WHERE id = $1", key_id)
    return int(row["free_checks"]) if row is not None and row["developer"] else 0
