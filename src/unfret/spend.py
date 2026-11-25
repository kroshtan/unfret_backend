"""
The free pool: what free LLM work may spend, a day at a time, and the meter that counts what it spent.

Every chat model the service builds (:mod:`unfret.llm`, the reports' second opinion) carries a :class:`SpendMeter`:
after each call it prices the tokens the provider reported (input and output, per million tokens, in euros:
:attr:`unfret.config.Settings.llm_prices_eur_per_mtok`) and adds the cost to a counter in memory, as **free** spend
unless the call runs for a check someone paid for (:func:`paying`: a bought check, x402), which counts as **paid**. A
background task (:func:`run_forever`) adds those counters to the day's row in ``llm_spend_days`` every few seconds
(micro-euros free and paid, and how many free deep checks ran: one row a UTC day, nothing about a page, a caller or a
model) and draws the free spend from the pool.

**The pool** (``free_pool``, one row). Every UTC day adds the daily base (``free_pool_daily_base_eur``, €3) to it; that
base part stops growing at ``free_pool_base_cap_days`` days' worth (€90). Donations go in by hand
(:func:`add_donation`, ``POST /v1/admin/free-pool/donations``: amount, day, note; nothing about donors) on top of
that cap. All free LLM spend draws from it, the base part first: shoppers' free deep checks, developer passes' free
deep checks, background work that runs for no one in particular. Paid spend never does. A day may spend at most
``free_pool_daily_max_multiple`` times the base (€9), pool or not. Once the pool is empty, or the day's limit is
spent, free deep checks pause until 00:00 UTC (``FREE_CHECKS_PAUSED``, ``reason: "spend_ceiling"``, ``scope:
"day"``): the next day adds its base and starts a fresh day's limit. A deep check that started before finishes.
Euros stay internal: in public the pool is a number of deep checks (:func:`checks_left_today`, at the measured average
cost of a free deep check).

Embedding calls (the photo and review indexes) are not metered: they cost a fraction of the chat calls.
"""

import asyncio
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import asyncpg
import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from unfret.config import Settings, settings

logger = structlog.get_logger("unfret")

type Executor = asyncpg.Pool | asyncpg.Connection

MICRO = Decimal(1_000_000)
_MTOK = Decimal(1_000_000)
# The days the average cost of a free deep check is measured over.
_COST_DAYS = 14


@dataclass(frozen=True)
class Price:
    """A model's list price in euros per million tokens."""

    input: Decimal
    output: Decimal


def parse_prices(value: str) -> dict[str, Price]:
    """
    Model prices as set: ``"model-a=0.15/0.35,model-b=1.80/5.50"`` (euros per million input/output tokens).

    :param value: The setting.
    :return: Model name → price.
    :raises ValueError: If it is malformed.
    """
    prices: dict[str, Price] = {}
    for part in filter(None, (p.strip() for p in value.split(","))):
        name, sep, pair = part.partition("=")
        given, slash, taken = pair.partition("/")
        try:
            price = Price(Decimal(given), Decimal(taken))
        except ArithmeticError as exc:
            raise ValueError(f"malformed model price: {part!r}") from exc
        if not sep or not slash or not name.strip() or price.input < 0 or price.output < 0:
            raise ValueError(f"malformed model price: {part!r}")
        prices[name.strip()] = price
    return prices


def price_of(model: str, config: Settings | None = None) -> Price:
    """
    A model's price: its own entry, else the price for models not listed (on the high side, never free).

    :param model: The model's name.
    :param config: The settings (the service's by default).
    :return: The price.
    """
    config = config or settings
    prices = parse_prices(config.llm_prices_eur_per_mtok)
    if model in prices:
        return prices[model]
    return parse_prices(f"x={config.llm_price_unlisted_eur_per_mtok}")["x"]


def cost_micro_eur(model: str, input_tokens: int, output_tokens: int, config: Settings | None = None) -> int:
    """
    What a call cost, rounded up to a micro-euro.

    :param model: The model's name.
    :param input_tokens: Tokens sent.
    :param output_tokens: Tokens received.
    :param config: The settings (the service's by default).
    :return: Micro-euros.
    """
    price = price_of(model, config)
    euros = (Decimal(max(input_tokens, 0)) * price.input + Decimal(max(output_tokens, 0)) * price.output) / _MTOK
    return int(-(-(euros * MICRO) // 1))


def today(now: datetime | None = None) -> date:
    """
    The pool's period: the UTC day.

    :param now: The time (now by default).
    :return: The day.
    """
    return (now or datetime.now(UTC)).date()


def seconds_to_tomorrow(now: datetime | None = None) -> int:
    """
    How long until free checks may run again: 00:00 UTC, when the day adds its base and its limit starts over.

    :param now: The time (now by default).
    :return: Seconds, at least 1.
    """
    now = now or datetime.now(UTC)
    tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
    return max(1, int((tomorrow - now).total_seconds()))


class _Pending:
    """Amounts per day that aren't in the database yet (thread-safe: callbacks may run in threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_day: dict[date, int] = {}

    def add(self, amount: int, day: date | None = None) -> None:
        if amount <= 0:
            return
        day = day or today()
        with self._lock:
            self._by_day[day] = self._by_day.get(day, 0) + amount

    def get(self, day: date) -> int:
        with self._lock:
            return self._by_day.get(day, 0)

    def total(self) -> int:
        with self._lock:
            return sum(self._by_day.values())

    def take(self) -> dict[date, int]:
        with self._lock:
            taken, self._by_day = self._by_day, {}
        return taken

    def put_back(self, taken: dict[date, int]) -> None:
        for day, amount in taken.items():
            self.add(amount, day)


_pending = _Pending()  # free spend, micro-euros
_pending_paid = _Pending()  # spend for checks someone paid for
_pending_checks = _Pending()  # free deep checks run
# Whether the LLM calls of this task (and the tasks it starts) run for a check someone paid for.
_paid: ContextVar[bool] = ContextVar("unfret_paid_spend", default=False)


def paying(paid: bool = True) -> None:
    """
    Mark the current task's LLM calls (and those of tasks it starts from now on) as paid for, or as free.

    Call it before an analysis runs: a bought check or an x402 payment with ``True``.

    :param paid: Whether someone paid for what runs now.
    """
    _paid.set(paid)


def record(model: str, input_tokens: int, output_tokens: int, *, paid: bool | None = None) -> int:
    """
    Count a call's cost (in memory until the next flush): free, drawn from the pool, or paid (:func:`paying`).

    :param model: The model's name.
    :param input_tokens: Tokens sent.
    :param output_tokens: Tokens received.
    :param paid: Paid for, or free; by default what :func:`paying` set for this task.
    :return: The cost in micro-euros.
    """
    cost = cost_micro_eur(model, input_tokens, output_tokens)
    (_pending_paid if (_paid.get() if paid is None else paid) else _pending).add(cost)
    return cost


def count_free_check() -> None:
    """Count a free deep check that ran (a shopper's or a developer pass's): for the average cost of one."""
    _pending_checks.add(1)


class SpendMeter(BaseCallbackHandler):
    """A LangChain callback on a chat model: prices the tokens of every call it makes."""

    def __init__(self, model: str) -> None:
        """
        Meter one model.

        :param model: The configured model name, for its price (whatever name the provider echoes).
        """
        super().__init__()
        self.model = model

    def on_llm_end(self, response: LLMResult, **_: Any) -> None:
        """
        Price the call's tokens.

        :param response: The model's result.
        """
        for generations in response.generations:
            for generation in generations:
                message = generation.message if isinstance(generation, ChatGeneration) else None
                usage = message.usage_metadata if isinstance(message, AIMessage) else None
                if usage:
                    record(self.model, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)))


async def _ensure_pool(conn: asyncpg.Connection, config: Settings, day: date) -> None:
    """
    Make the pool row exist and add the base of every day up to ``day`` that hasn't been added yet (capped).

    :param conn: A connection (in a transaction).
    :param config: The settings.
    :param day: The day to bring the pool up to.
    """
    base = int(config.free_pool_daily_base_eur * MICRO)
    cap = base * config.free_pool_base_cap_days
    await conn.execute(
        "INSERT INTO free_pool (id, base_micro_eur, donated_micro_eur, credited_day) VALUES (1, $1, 0, $2) "
        "ON CONFLICT (id) DO NOTHING",
        base,
        day,
    )
    # Days since the last credit each add the base, up to the cap (a base part in debt still gets every day's base).
    await conn.execute(
        "UPDATE free_pool SET base_micro_eur = LEAST(base_micro_eur + $1::bigint * ($3::date - credited_day), "
        "GREATEST($2::bigint, base_micro_eur)), credited_day = $3 WHERE id = 1 AND credited_day < $3",
        base,
        cap,
        day,
    )


async def _draw(conn: asyncpg.Connection, amount: int) -> None:
    """
    Draw free spend from the pool.

    The base part first (it is what stops growing at its cap), then donations; past both, the base part goes into
    debt that the next days' bases pay off.

    :param conn: A connection (in a transaction; the pool row exists).
    :param amount: Micro-euros.
    """
    row = await conn.fetchrow("SELECT base_micro_eur, donated_micro_eur FROM free_pool WHERE id = 1 FOR UPDATE")
    base, donated = int(row["base_micro_eur"]), int(row["donated_micro_eur"])
    from_base = min(max(base, 0), amount)
    from_donated = min(max(donated, 0), amount - from_base)
    base -= amount - from_donated
    await conn.execute(
        "UPDATE free_pool SET base_micro_eur = $1, donated_micro_eur = $2 WHERE id = 1", base, donated - from_donated
    )


async def flush(db: asyncpg.Pool, config: Settings | None = None) -> None:
    """
    Add what is in memory to the database: each day's spend and free checks, the free spend drawn from the pool.

    :param db: The pool.
    :param config: The settings (the service's by default).
    """
    config = config or settings
    free, paid, checks = _pending.take(), _pending_paid.take(), _pending_checks.take()
    try:
        async with db.acquire() as conn, conn.transaction():
            await _ensure_pool(conn, config, today())
            for day in sorted(set(free) | set(paid) | set(checks)):
                spent = free.get(day, 0)
                await conn.execute(
                    "INSERT INTO llm_spend_days (day, free_micro_eur, paid_micro_eur, free_checks) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (day) DO UPDATE SET "
                    "free_micro_eur = llm_spend_days.free_micro_eur + EXCLUDED.free_micro_eur, "
                    "paid_micro_eur = llm_spend_days.paid_micro_eur + EXCLUDED.paid_micro_eur, "
                    "free_checks = llm_spend_days.free_checks + EXCLUDED.free_checks",
                    day,
                    spent,
                    paid.get(day, 0),
                    checks.get(day, 0),
                )
                if spent:
                    await _draw(conn, spent)
        free, paid, checks = {}, {}, {}
    finally:
        _pending.put_back(free)
        _pending_paid.put_back(paid)
        _pending_checks.put_back(checks)


@dataclass(frozen=True)
class Standing:
    """Where the free pool and today's limit stand."""

    pool_micro_eur: int
    spent_today_micro_eur: int
    day_limit_micro_eur: int

    @property
    def reached(self) -> bool:
        """Whether free LLM work pauses: the pool is empty, or today's limit is spent."""
        return self.pool_micro_eur <= 0 or self.spent_today_micro_eur >= self.day_limit_micro_eur

    @property
    def scope(self) -> Literal["day"] | None:
        """
        Until when free checks are paused, if they are: always until 00:00 UTC.

        :return: ``day``, or ``None``.
        """
        return "day" if self.reached else None

    @property
    def left_micro_eur(self) -> int:
        """What free work may still spend today: the pool, within today's limit."""
        return max(min(self.pool_micro_eur, self.day_limit_micro_eur - self.spent_today_micro_eur), 0)

    def retry_after_s(self, now: datetime | None = None) -> int:
        """
        Seconds until free checks are back: 00:00 UTC.

        :param now: The time (now by default).
        :return: Seconds (1 at least; 0 when they aren't paused).
        """
        return seconds_to_tomorrow(now) if self.reached else 0


async def standing(db: Executor | None, config: Settings | None = None, now: datetime | None = None) -> Standing:
    """
    Where the pool stands now, what is in memory counted. Without a database it counts as empty.

    :param db: A pool or connection, or ``None``.
    :param config: The settings (the service's by default).
    :param now: The time (now by default).
    :return: The standing.
    """
    config = config or settings
    base = int(config.free_pool_daily_base_eur * MICRO)
    limit = base * config.free_pool_daily_max_multiple
    if db is None:
        return Standing(0, limit, limit)
    day = today(now)
    row = await db.fetchrow("SELECT base_micro_eur, donated_micro_eur, credited_day FROM free_pool WHERE id = 1")
    if row is None:
        pool = base  # the first day's base, added at the first flush
    else:
        days = max((day - row["credited_day"]).days, 0)
        cap = base * config.free_pool_base_cap_days
        stored = int(row["base_micro_eur"])
        grown = min(stored + base * days, max(cap, stored)) if days else stored
        pool = grown + int(row["donated_micro_eur"])
    spent = int(await db.fetchval("SELECT free_micro_eur FROM llm_spend_days WHERE day = $1", day) or 0)
    return Standing(pool - _pending.total(), spent + _pending.get(day), limit)


async def average_free_check_micro_eur(db: Executor, config: Settings | None = None) -> int:
    """
    The measured average cost of a free deep check: free spend over free deep checks, the last two weeks.

    :param db: A pool or connection.
    :param config: The settings (the estimate ``free_check_cost_eur`` until there is something to measure).
    :return: Micro-euros, at least 1.
    """
    config = config or settings
    row = await db.fetchrow(
        "SELECT COALESCE(SUM(free_micro_eur), 0) AS spent, COALESCE(SUM(free_checks), 0) AS checks "
        "FROM llm_spend_days WHERE day > $1",
        today() - timedelta(days=_COST_DAYS),
    )
    if row is None or int(row["checks"]) < 20:  # too few to measure
        return max(int(config.free_check_cost_eur * MICRO), 1)
    return max(int(row["spent"]) // int(row["checks"]), 1)


async def checks_left_today(db: Executor | None, config: Settings | None = None) -> int:
    """
    How many free deep checks may still run today, in public: what is left, at the average cost of one.

    :param db: A pool or connection, or ``None``.
    :param config: The settings (the service's by default).
    :return: A count (0 when paused).
    """
    if db is None:
        return 0
    standing_now = await standing(db, config)
    return standing_now.left_micro_eur // await average_free_check_micro_eur(db, config)


async def add_donations(
    db: asyncpg.Pool, amounts_eur: list[Decimal], note: str = "", config: Settings | None = None
) -> list[int]:
    """
    Add donations to the pool (on top of the base part's cap): one ledger entry each (amount, day, note).

    The ledger, ``free_pool_donations``, has one row per donation, so its rows count the donations
    (:func:`donation_count`, what /live shows as supporters). A donation is in the pool at once: today it can be spent
    up to the day's limit (three times the base), which a donation never raises.

    :param db: The pool.
    :param amounts_eur: The donations' amounts in euros (each positive), one entry each.
    :param note: A short note for every entry (never anything about a donor).
    :param config: The settings (the service's by default).
    :return: The ledger rows' ids, in order.
    :raises ValueError: If there is none, or an amount isn't positive.
    """
    if not amounts_eur or any(a <= 0 for a in amounts_eur):
        raise ValueError("donations must be positive, at least one")
    micros = [int(a * MICRO) for a in amounts_eur]
    day = today()
    async with db.acquire() as conn, conn.transaction():
        await _ensure_pool(conn, config or settings, day)
        await conn.execute("UPDATE free_pool SET donated_micro_eur = donated_micro_eur + $1 WHERE id = 1", sum(micros))
        return [
            int(
                await conn.fetchval(
                    "INSERT INTO free_pool_donations (day, micro_eur, note) VALUES ($1, $2, $3) RETURNING id",
                    day,
                    micro,
                    note[:200],
                )
            )
            for micro in micros
        ]


async def add_donation(db: asyncpg.Pool, amount_eur: Decimal, note: str = "", config: Settings | None = None) -> int:
    """
    Add one donation to the pool (:func:`add_donations`).

    :param db: The pool.
    :param amount_eur: The amount in euros (positive).
    :param note: A short note (never anything about the donor).
    :param config: The settings (the service's by default).
    :return: The ledger row's id.
    """
    return (await add_donations(db, [amount_eur], note, config))[0]


async def donation_count(db: asyncpg.Pool) -> int:
    """
    How many donations the ledger holds: one entry each, nothing about who gave.

    :param db: The pool.
    :return: The number of donations.
    """
    return int(await db.fetchval("SELECT count(*) FROM free_pool_donations"))


async def pool_summary(db: asyncpg.Pool, config: Settings | None = None) -> dict[str, Any]:
    """
    The pool for the operator: euros, today's spend and limit, the average cost of a check, the ledger.

    :param db: The pool.
    :param config: The settings (the service's by default).
    :return: The summary.
    """
    config = config or settings
    async with db.acquire() as conn, conn.transaction():
        await _ensure_pool(conn, config, today())
    now = await standing(db, config)
    row = await db.fetchrow("SELECT base_micro_eur, donated_micro_eur FROM free_pool WHERE id = 1")
    donations = await db.fetch("SELECT id, day, micro_eur, note FROM free_pool_donations ORDER BY id DESC LIMIT 50")

    def eur(micro: int) -> str:
        return format(Decimal(micro) / MICRO, ".2f")

    return {
        "pool_eur": eur(now.pool_micro_eur),
        "base_eur": eur(int(row["base_micro_eur"])) if row else "0.00",
        "donated_eur": eur(int(row["donated_micro_eur"])) if row else "0.00",
        "spent_today_eur": eur(now.spent_today_micro_eur),
        "day_limit_eur": eur(now.day_limit_micro_eur),
        "paused": now.reached,
        "average_check_eur": format(Decimal(await average_free_check_micro_eur(db, config)) / MICRO, ".4f"),
        "checks_left_today": await checks_left_today(db, config),
        "donations": [
            {"id": d["id"], "day": d["day"].isoformat(), "eur": eur(int(d["micro_eur"])), "note": d["note"]}
            for d in donations
        ],
    }


async def spent_free_micro_eur(db: Executor, day: date | None = None) -> int:
    """
    A day's free spend: the database's row plus what is still in memory.

    :param db: A pool or connection.
    :param day: The day (today by default).
    :return: Micro-euros.
    """
    day = day or today()
    stored = await db.fetchval("SELECT free_micro_eur FROM llm_spend_days WHERE day = $1", day)
    return int(stored or 0) + _pending.get(day)


async def run_forever(pool: asyncpg.Pool, interval_s: float = 5.0) -> None:
    """
    Flush the meter every ``interval_s`` until cancelled (then once more).

    :param pool: The database pool.
    :param interval_s: Seconds between flushes.
    """
    try:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await flush(pool)
            except Exception as exc:  # noqa: BLE001 - kept in memory, retried next round
                logger.warning("llm_spend_flush_failed", error=type(exc).__name__)
    finally:
        try:
            await asyncio.shield(flush(pool))
        except Exception as exc:  # noqa: BLE001 - shutting down
            logger.warning("llm_spend_flush_failed", error=type(exc).__name__)
