"""
Free checks: a few deep checks on a new pass, and quick checks for all.

**On a pass.** ``POST /v1/keys`` with ``"free": true`` makes a pass with ``free_checks_per_pass`` free deep checks,
kept as a count in ``api_keys.free_checks`` (nothing else: not who asked, nor from where). The browser extension asks
for one when it is installed, the link checker (``/check``) on a browser's first visit; each remembers in the browser
that it got one, so a reinstall gets a pass without free checks. A pass is made with its free checks only within
``free_passes_per_ip_per_day`` per visitor network (:class:`unfret.visitors.DailyLimit`: in process memory, salted
hashes of the network, gone at a restart); past that it is made empty, like any other new pass, on top of the key
guard's own proof of work and per-network limits. Free checks are spent before the balance, and can move to another
pass of the same holder (:func:`move`) but are never turned into balance.

**Developer passes.** ``"developer": true`` instead makes a pass with ``developer_free_checks`` (20) free deep
checks, for building an agent against ``/v1/check`` (which takes a pass like ``/v1/analyze`` does), the MCP server or
the clients; at most ``developer_passes_per_ip_per_day`` per visitor network, under a proof of work
``developer_pow_extra_bits`` harder (:class:`unfret.credits.keys.KeyGuard`). The ``/developers`` page asks for one
once per browser. They count **only on** ``/v1/check`` (``api=True`` below): the pass keeps a flag
(``api_keys.developer``, nothing about who asked), and on ``/v1/analyze`` (the website, the extension, the phone
app) a developer pass is a pass without free checks: its bought checks work there as anywhere, and without them it
answers as an empty pass does. Its free checks never move to another pass, and none move onto it. Passes made before
the flag have none set.

**Two budgets.** Every free deep check counts towards a global counter a UTC day (``usage_counters``: a number per
day, nothing about anyone). A shopper's pass counts under ``free_checks``, capped at
:attr:`unfret.config.Settings.free_daily_checks_cap` (the day's budget in euros over the estimated cost of one deep
check); a developer pass counts under ``developer_free_checks``, capped at
:attr:`unfret.config.Settings.developer_free_daily_checks_cap` in the same way. Neither touches the other: a script
that keeps making developer passes from many networks can use up the developers' day, never the shoppers'. Once
the shoppers' budget is reached, their free deep checks are "back tomorrow" (00:00 UTC, ``paused``); once the
developers' is, a developer pass with only free checks is answered as an empty pass is, saying they are back at
00:00 UTC (``developer_paused``). Bought checks and quick checks go on either way. A free check whose analysis fails
or can't be done is given back, to the pass and to the budget it came from.

**Quick checks** (rules only, no LLM: the x402 quick tier) cost nothing to run and are free for everyone, with or
without a pass, within a per-minute rate limit (:mod:`unfret.routes.analyze`).

Wording: this tier is "free checks" ("5 free checks"); gift codes keep their own words
(:func:`unfret.credits.pricing.pack_labels`).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import asyncpg

from unfret.config import Settings

type Executor = asyncpg.Pool | asyncpg.Connection

COUNTER = "free_checks"
DEVELOPER_COUNTER = "developer_free_checks"

Taken = Literal["taken", "none", "paused", "developer_paused"]


def today() -> str:
    """
    The budget's period: the UTC day.

    :return: ``YYYY-MM-DD``.
    """
    return datetime.now(UTC).date().isoformat()


def seconds_to_tomorrow(now: datetime | None = None) -> int:
    """
    How long until the budget starts over (00:00 UTC).

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
    counter: str = COUNTER  # the budget it came from: the shoppers' or the developers'


def _budget(settings: Settings, *, developer: bool) -> tuple[str, int]:
    if developer:
        return DEVELOPER_COUNTER, settings.developer_free_daily_checks_cap
    return COUNTER, settings.free_daily_checks_cap


async def used_today(db: Executor, *, developer: bool = False) -> int:
    """
    The free deep checks used today from one budget, by every pass that counts in it.

    :param db: A pool or connection.
    :param developer: The developer passes' budget; otherwise the shoppers'.
    :return: The count.
    """
    counter = DEVELOPER_COUNTER if developer else COUNTER
    count = await db.fetchval("SELECT count FROM usage_counters WHERE name = $1 AND period = $2", counter, today())
    return int(count or 0)


async def paused(db: Executor, settings: Settings, *, developer: bool = False) -> bool:
    """
    Whether today's budget for free deep checks is spent.

    :param db: A pool or connection.
    :param settings: The service settings.
    :param developer: The developer passes' budget; otherwise the shoppers'.
    :return: ``True`` until 00:00 UTC once it is.
    """
    _, cap = _budget(settings, developer=developer)
    return cap <= 0 or await used_today(db, developer=developer) >= cap


async def _take_budget(conn: Executor, settings: Settings, period: str, *, developer: bool) -> str | None:
    counter, cap = _budget(settings, developer=developer)
    if cap <= 0:
        return None
    # One statement: concurrent checks can't both take the budget's last check.
    taken = await conn.fetchval(
        "INSERT INTO usage_counters (name, period, count) VALUES ($1, $2, 1) ON CONFLICT (name, period) "
        "DO UPDATE SET count = usage_counters.count + 1 WHERE usage_counters.count < $3 RETURNING count",
        counter,
        period,
        cap,
    )
    return counter if taken is not None else None


async def take_for_key(
    pool: asyncpg.Pool, settings: Settings, key_id: str, *, api: bool = False
) -> tuple[Taken, Spent | None]:
    """
    Take one of a pass's free deep checks, and one from the day's budget, together.

    :param pool: The pool.
    :param settings: The service settings.
    :param key_id: The pass's id.
    :param api: The check is on ``/v1/check``, where a developer pass's free checks count too.
    :return: ``("taken", spent)``; ``("none", None)`` without free checks left (or a developer pass off the API);
        ``("paused", None)`` when the pass has some but the shoppers' budget is spent today, ``("developer_paused",
        None)`` when it is a developer pass and the developers' budget is (nothing is taken then).
    """
    period = today()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT free_checks, developer FROM api_keys WHERE id = $1 FOR UPDATE", key_id)
        if row is None or not row["free_checks"] or (row["developer"] and not api):
            return "none", None
        developer = bool(row["developer"])
        counter = await _take_budget(conn, settings, period, developer=developer)
        if counter is None:
            return ("developer_paused" if developer else "paused"), None
        await conn.execute("UPDATE api_keys SET free_checks = free_checks - 1 WHERE id = $1", key_id)
    return "taken", Spent(period, key_id, counter)


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


@dataclass(frozen=True)
class Held:
    """A pass's free deep checks left, and whether they are a developer pass's (they count on ``/v1/check`` only)."""

    checks: int
    developer: bool

    def usable(self, *, api: bool) -> int:
        """
        The free checks that count where the check is made.

        :param api: On ``/v1/check`` (``True``) or ``/v1/analyze`` (``False``).
        :return: The count: a developer pass's only on the API.
        """
        return self.checks if api or not self.developer else 0


async def held(db: Executor, key_id: str) -> Held:
    """
    A pass's free deep checks left, whichever endpoint they count on.

    :param db: A pool or connection.
    :param key_id: The pass's id.
    :return: The count and the developer flag (0 and ``False`` for an unknown id).
    """
    row = await db.fetchrow("SELECT free_checks, developer FROM api_keys WHERE id = $1", key_id)
    return Held(0, False) if row is None else Held(int(row["free_checks"]), bool(row["developer"]))


async def left(db: Executor, key_id: str, *, api: bool = False) -> int:
    """
    A pass's free deep checks left that count where the check is made.

    :param db: A pool or connection.
    :param key_id: The pass's id.
    :param api: On ``/v1/check``; otherwise ``/v1/analyze``, where a developer pass has none.
    :return: The count (0 for an unknown id).
    """
    return (await held(db, key_id)).usable(api=api)


async def move(pool: asyncpg.Pool, from_id: str, to_id: str) -> int:
    """
    Move every free check left on one pass to another (the extension switching to a pass its user bought).

    :param pool: The pool.
    :param from_id: The pass they are on (the caller holds it).
    :param to_id: The pass to put them on.
    :return: How many moved (0 if the other pass doesn't exist, there were none, or either is a developer pass: its
        free checks are for the API, and a shopper's don't become that).
    """
    if from_id == to_id:
        return 0
    async with pool.acquire() as conn, conn.transaction():
        # Lock both rows in id order: two moves the other way round can't deadlock.
        await conn.execute(
            "SELECT 1 FROM api_keys WHERE id = ANY($1::text[]) ORDER BY id FOR UPDATE", [from_id, to_id]
        )
        if not await conn.fetchval("SELECT TRUE FROM api_keys WHERE id = $1", to_id):
            return 0
        if await conn.fetchval("SELECT bool_or(developer) FROM api_keys WHERE id = ANY($1::text[])", [from_id, to_id]):
            return 0
        moved = int(await conn.fetchval("SELECT free_checks FROM api_keys WHERE id = $1", from_id) or 0)
        if moved:
            await conn.execute("UPDATE api_keys SET free_checks = 0 WHERE id = $1", from_id)
            await conn.execute("UPDATE api_keys SET free_checks = free_checks + $2 WHERE id = $1", to_id, moved)
    return moved
