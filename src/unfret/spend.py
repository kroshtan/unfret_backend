"""
The LLM spend this calendar month, and the ceiling that pauses free deep checks.

Every chat model the service builds (:mod:`unfret.llm`, the reports' second opinion) carries a :class:`SpendMeter`:
after each call it prices the tokens the provider reported (input and output, per million tokens, in euros:
:attr:`unfret.config.Settings.llm_prices_eur_per_mtok`) and adds the cost to a counter in memory. A background task
(:func:`run_forever`) adds that counter to the month's row in ``llm_spend`` every few seconds (micro-euros, one row a
calendar month in UTC, nothing else: no page, no caller, no model). The month's spend is that row plus what is still
in memory.

**The ceiling.** Once the month's spend reaches ``llm_spend_ceiling_eur`` (€100 by default), free deep checks pause
(``FREE_CHECKS_PAUSED`` with ``reason: "spend_ceiling"``) until the first of next month (UTC). Everything that pays
keeps working: bought checks, x402, a developer pass's bought checks. Quick checks need no LLM and keep working too.
The ceiling is a kill switch, not a budget per check: a deep check that started below it finishes.

Embedding calls (the photo and review indexes) are not metered: they cost a fraction of the chat calls.
"""

import asyncio
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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


def month(now: datetime | None = None) -> str:
    """
    The ceiling's period: the calendar month in UTC.

    :param now: The time (now by default).
    :return: ``YYYY-MM``.
    """
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def seconds_to_next_month(now: datetime | None = None) -> int:
    """
    How long until the ceiling starts over (the first of next month, 00:00 UTC).

    :param now: The time (now by default).
    :return: Seconds, at least 1.
    """
    now = now or datetime.now(UTC)
    first = datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=UTC)
    return max(1, int((first - now).total_seconds()))


class _Pending:
    """Micro-euros spent per month that aren't in the database yet (thread-safe: callbacks may run in threads)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_month: dict[str, int] = {}

    def add(self, amount: int, period: str | None = None) -> None:
        if amount <= 0:
            return
        period = period or month()
        with self._lock:
            self._by_month[period] = self._by_month.get(period, 0) + amount

    def get(self, period: str) -> int:
        with self._lock:
            return self._by_month.get(period, 0)

    def take(self) -> dict[str, int]:
        with self._lock:
            taken, self._by_month = self._by_month, {}
        return taken

    def put_back(self, taken: dict[str, int]) -> None:
        for period, amount in taken.items():
            self.add(amount, period)


_pending = _Pending()


def record(model: str, input_tokens: int, output_tokens: int) -> int:
    """
    Count a call's cost towards this month's spend (in memory until the next flush).

    :param model: The model's name.
    :param input_tokens: Tokens sent.
    :param output_tokens: Tokens received.
    :return: The cost in micro-euros.
    """
    cost = cost_micro_eur(model, input_tokens, output_tokens)
    _pending.add(cost)
    return cost


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


async def flush(db: Executor) -> None:
    """
    Add what is in memory to the database.

    :param db: A pool or connection.
    """
    taken = _pending.take()
    try:
        for period, amount in taken.items():
            await db.execute(
                "INSERT INTO llm_spend (month, micro_eur) VALUES ($1, $2) ON CONFLICT (month) "
                "DO UPDATE SET micro_eur = llm_spend.micro_eur + EXCLUDED.micro_eur",
                period,
                amount,
            )
            taken[period] = 0
    finally:
        _pending.put_back({p: a for p, a in taken.items() if a})


async def spent_micro_eur(db: Executor, period: str | None = None) -> int:
    """
    The month's spend: the database's row plus what is still in memory.

    :param db: A pool or connection.
    :param period: The month (this one by default).
    :return: Micro-euros.
    """
    period = period or month()
    stored = await db.fetchval("SELECT micro_eur FROM llm_spend WHERE month = $1", period)
    return int(stored or 0) + _pending.get(period)


@dataclass(frozen=True)
class Ceiling:
    """Where this month's spend stands against the ceiling."""

    spent_micro_eur: int
    ceiling_micro_eur: int

    @property
    def reached(self) -> bool:
        """Whether free deep checks are paused."""
        return self.spent_micro_eur >= self.ceiling_micro_eur


async def ceiling(db: Executor | None, config: Settings | None = None) -> Ceiling:
    """
    Where this month's spend stands. Without a database it counts as reached: nothing can be metered then.

    :param db: A pool or connection, or ``None``.
    :param config: The settings (the service's by default).
    :return: The standing.
    """
    config = config or settings
    cap = int(config.llm_spend_ceiling_eur * MICRO)
    if db is None:
        return Ceiling(cap, cap)
    return Ceiling(await spent_micro_eur(db), cap)


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
