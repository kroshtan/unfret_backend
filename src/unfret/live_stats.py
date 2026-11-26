"""
The public live stats behind ``/live`` and ``GET /v1/stats/live``: counts only, nothing about anyone.

**What it reads.** Checks that answered, a number a UTC day and tier (``usage_counters``, ``checks_quick`` and
``checks_deep``, written by :mod:`unfret.usage`), also as a series of the last :data:`DAILY_DAYS` days; the free deep
checks that ran, a number a UTC day (``llm_spend_days.free_checks``); the stores' row counts (product photos, prices,
shops, listings);
"Wrong result?" reports that settled and agreed with the settled answer (``report_labels`` joined to
``report_answers``, held answers left out); and the contributors who opted in to the leaderboard with a nickname the
operator approved (:mod:`unfret.nicknames`), by their verified reports.

**What it never shows.** No IP address, pass, checked page, shop, report text or anything else about a person: the
answer is built field by field (:func:`compute`), an allow-list the tests pin down. A count below
:data:`SMALL` is ``{"value": null, "fewer_than_5": true}``, a day of the series too; a leaderboard entry below it is
left out. The free budget is shown as checks, never euros. This month's donated amount (and the checks it funds) shows
only from :data:`SMALL` donations on, so no single gift can be read from it: before that it is ``null`` (none at all
is ``0``).

**Caching.** Computed at most once every :data:`TTL_S` seconds per process (:func:`cached`), served with
``Cache-Control: public, max-age=60``.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg

from unfret import spend
from unfret.config import Settings

TTL_S = 60.0
SMALL = 5
LEADERBOARD_SIZE = 10
DAILY_DAYS = 30


def count(n: int | None) -> dict[str, Any]:
    """
    A public count: below :data:`SMALL` it says only "fewer than 5".

    :param n: The number, or ``None`` when unknown.
    :return: ``{"value": n, "fewer_than_5": False}``, or ``{"value": None, "fewer_than_5": True}``; unknown is
        ``{"value": None, "fewer_than_5": False}``.
    """
    if n is None:
        return {"value": None, "fewer_than_5": False}
    if n < SMALL:
        return {"value": None, "fewer_than_5": True}
    return {"value": int(n), "fewer_than_5": False}


async def free_budget_left_micro_eur(pool: asyncpg.Pool) -> int | None:
    """
    What today's free budget can still spend, in micro-euros: the free pool within today's limit.

    A donation added to the pool (``make free-pool``) counts at once (:func:`unfret.spend.standing`).

    :param pool: The database pool.
    :return: The remainder (``None`` would say it is unknown).
    """
    return (await spend.standing(pool)).left_micro_eur


async def free_budget_today_micro_eur(pool: asyncpg.Pool) -> int:
    """
    Today's whole free amount, in micro-euros: what is left plus what free checks spent today (for the ring on /live).

    :param pool: The database pool.
    :return: The amount.
    """
    standing = await spend.standing(pool)
    return standing.left_micro_eur + min(standing.spent_today_micro_eur, standing.day_limit_micro_eur)


async def donations_this_month(pool: asyncpg.Pool, now: datetime) -> tuple[float, int]:
    """
    The donations this calendar month (UTC), from the free pool's ledger: the euros and how many (never who).

    :param pool: The database pool.
    :param now: The moment (UTC).
    :return: ``(euros, donations)``.
    """
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(micro_eur), 0) AS micro, COUNT(*) AS n FROM free_pool_donations WHERE day >= $1",
        now.date().replace(day=1),
    )
    assert row is not None  # noqa: S101 - an aggregate always has a row
    return int(row["micro"]) / 1_000_000, int(row["n"])


async def _checks(pool: asyncpg.Pool, now: datetime) -> tuple[int, int, int]:
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(count) FILTER (WHERE period = $1), 0) AS today, "
        "COALESCE(SUM(count) FILTER (WHERE period LIKE $2 || '-%'), 0) AS month, COALESCE(SUM(count), 0) AS total "
        "FROM usage_counters WHERE name IN ('checks_quick', 'checks_deep')",
        day,
        month,
    )
    assert row is not None  # noqa: S101 - an aggregate always has a row
    return int(row["today"]), int(row["month"]), int(row["total"])


async def _daily(pool: asyncpg.Pool, now: datetime) -> list[dict[str, Any]]:
    # Checks that answered, a UTC day, the last DAILY_DAYS days up to today (a day without any is 0).
    days = [now.date() - timedelta(days=i) for i in range(DAILY_DAYS - 1, -1, -1)]
    rows = await pool.fetch(
        "SELECT period, SUM(count) AS n FROM usage_counters WHERE name IN ('checks_quick', 'checks_deep') "
        "AND period >= $1 GROUP BY period",
        days[0].isoformat(),
    )
    by_day = {str(r["period"]): int(r["n"]) for r in rows}
    return [{"date": d.isoformat(), **count(by_day.get(d.isoformat(), 0))} for d in days]


async def _free_given(pool: asyncpg.Pool, now: datetime) -> tuple[int, int]:
    # Free deep checks that ran (a shopper's or a developer pass's): today and this calendar month (UTC).
    day: date = now.date()
    row = await pool.fetchrow(
        "SELECT COALESCE(SUM(free_checks) FILTER (WHERE day = $1), 0) AS today, "
        "COALESCE(SUM(free_checks), 0) AS month FROM llm_spend_days WHERE day >= $2 AND day <= $1",
        day,
        day.replace(day=1),
    )
    assert row is not None  # noqa: S101 - an aggregate always has a row
    return int(row["today"]), int(row["month"])


async def _database(pool: asyncpg.Pool) -> dict[str, int]:
    row = await pool.fetchrow(
        "SELECT (SELECT COUNT(*) FROM image_prints) AS photos, "
        "(SELECT COUNT(*) FROM price_observations) + (SELECT COUNT(*) FROM market_prices) AS prices, "
        "(SELECT COUNT(*) FROM shops) AS shops, "
        "(SELECT COUNT(*) FROM image_prints WHERE source = 'seen') + (SELECT COUNT(*) FROM ad_prints) AS pages"
    )
    assert row is not None  # noqa: S101 - one row of subqueries
    return {k: int(v) for k, v in dict(row).items()}


async def _verified_reports(pool: asyncpg.Pool) -> int:
    # A report whose page settled (by agreement, or by a reviewer) on the level it gave.
    value = await pool.fetchval(
        "SELECT COUNT(*) FROM report_labels l WHERE l.settled AND l.level IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM report_answers a WHERE a.page_key = l.page_key AND a.level = l.level AND NOT a.held)"
    )
    return int(value or 0)


async def _leaderboard(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT handle, agreed FROM contributors WHERE public AND handle IS NOT NULL AND handle_review = 'approved' "
        "AND agreed >= $1 ORDER BY agreed DESC, handle LIMIT $2",
        SMALL,
        LEADERBOARD_SIZE,
    )
    return [
        {"rank": i, "nickname": str(r["handle"]), "verified_reports": count(r["agreed"])}
        for i, r in enumerate(rows, start=1)
    ]


async def compute(pool: asyncpg.Pool, config: Settings, now: datetime | None = None) -> dict[str, Any]:
    """
    The live stats, field by field (the allow-list: nothing else goes in).

    :param pool: The database pool.
    :param config: The settings.
    :param now: The moment (UTC; now by default).
    :return: The stats.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    today, month, total = await _checks(pool, now)
    cost = float(config.public_cost_per_check_eur)
    left = await free_budget_left_micro_eur(pool)
    free_left = None if left is None else spend.public_checks(left, config)  # the count /v1/pricing shows
    whole = max(await free_budget_today_micro_eur(pool), left or 0)
    given_today, given_month = await _free_given(pool, now)
    raised, donations = await donations_this_month(pool, now)
    shown = donations == 0 or donations >= SMALL  # no single gift readable from the amount
    db = await _database(pool)
    return {
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "checks": {
            "today": count(today),
            "this_month": count(month),
            "all_time": count(total),
            "daily": await _daily(pool, now),
        },
        "free_checks_left_today": {**count(free_left), "available": left is not None},
        "free_checks_today": count(spend.public_checks(whole, config)),
        "free_checks_given": {"today": count(given_today), "this_month": count(given_month)},
        "funding": {
            "goal_eur": round(config.live_funding_goal_eur, 2),
            "raised_eur": round(raised, 2) if shown else None,
            "check_cost_eur": round(cost, 2),
            "free_checks": count(spend.public_checks(round(raised * 1_000_000), config) if shown else None),
            "supporters": count(await spend.donation_count(pool)),
        },
        "database": {
            "product_photos": count(db["photos"]),
            "prices": count(db["prices"]),
            "shops": count(db["shops"]),
            "pages_checked": count(db["pages"]),
        },
        "verified_reports": count(await _verified_reports(pool)),
        "leaderboard": await _leaderboard(pool),
    }


@dataclass
class _Cache:
    at: float = float("-inf")
    value: dict[str, Any] | None = None
    lock: asyncio.Lock | None = None


_cache = _Cache()


async def cached(pool: asyncpg.Pool, config: Settings) -> dict[str, Any]:
    """
    The live stats, computed at most once every :data:`TTL_S` seconds (one computation at a time).

    :param pool: The database pool.
    :param config: The settings.
    :return: The stats.
    """
    if _cache.value is not None and time.monotonic() - _cache.at < TTL_S:
        return _cache.value
    if _cache.lock is None:
        _cache.lock = asyncio.Lock()
    async with _cache.lock:
        if _cache.value is None or time.monotonic() - _cache.at >= TTL_S:
            _cache.value = await compute(pool, config)
            _cache.at = time.monotonic()
    return _cache.value


def clear() -> None:
    """Forget the cached stats (tests)."""
    _cache.at, _cache.value, _cache.lock = float("-inf"), None, None
