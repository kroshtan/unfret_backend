"""
The public live stats (``/live``, ``/live?stream=1``, ``GET /v1/stats/live``): an allow-list of counts, nothing about
anyone, small counts hidden, cached, no cookies, nothing from another origin; and the nicknames' review.

The allow-list test fails on purpose when a field is added: adding one is a privacy decision, not a refactor.
"""

import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.test_crowd_labels import _fresh_until, new_key
from unfret import live_stats, spend, usage
from unfret.config import settings
from unfret.migrations import migrate

ADMIN = {"Authorization": "Bearer operator-token"}
COUNT = {"value", "fewer_than_5"}
# Exactly these fields, nested. Anything else in the answer fails here.
SHAPE: dict[str, Any] = {
    "generated_at": str,
    "checks": {"today": COUNT, "this_month": COUNT, "all_time": COUNT},
    "free_checks_left_today": COUNT | {"available"},
    "funding": {
        "goal_eur": float,
        "raised_eur": float,
        "check_cost_eur": float,
        "free_checks": COUNT,
        "supporters": COUNT,
    },
    "database": {"product_photos": COUNT, "prices": COUNT, "shops": COUNT, "pages_checked": COUNT},
    "verified_reports": COUNT,
    "leaderboard": [{"rank", "nickname", "verified_reports"}],
}
TODAY = datetime.now(UTC).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    live_stats.clear()
    yield
    live_stats.clear()


def assert_shape(value: Any, shape: Any, where: str = "stats") -> None:
    if shape is str:
        assert isinstance(value, str), where
    elif shape is float:
        assert isinstance(value, (int, float)), where
    elif isinstance(shape, set):
        assert isinstance(value, dict) and set(value) == shape, f"{where}: {sorted(value)} != {sorted(shape)}"
        if shape >= COUNT:
            assert value["value"] is None or isinstance(value["value"], int), where
            if value["fewer_than_5"]:
                assert value["value"] is None, where
    elif isinstance(shape, list):
        assert isinstance(value, list), where
        for i, item in enumerate(value):
            assert isinstance(item, dict) and set(item) == shape[0], f"{where}[{i}]: {sorted(item)}"
            assert set(item["verified_reports"]) == COUNT
    else:
        assert isinstance(value, dict) and set(value) == set(shape), f"{where}: {sorted(value)} != {sorted(shape)}"
        for key, sub in shape.items():
            assert_shape(value[key], sub, f"{where}.{key}")


async def contributor(pool: asyncpg.Pool, handle: str, agreed: int, review: str, public: bool = True) -> None:
    await pool.execute(
        "INSERT INTO contributors (key_hash, handle, public, labels, settled, agreed, handle_review) "
        "VALUES ($1, $2, $3, $4, $4, $4, $5)",
        handle.encode().ljust(32, b"_"),
        handle,
        public,
        agreed,
        review,
    )


async def donate(pool: asyncpg.Pool, amounts: list[int], day: date | None = None) -> None:
    day = day or datetime.now(UTC).date()
    await pool.executemany(
        "INSERT INTO free_pool_donations (day, micro_eur, note) VALUES ($1, $2, 'a note never shown')",
        [(day, a) for a in amounts],
    )


async def counters(pool: asyncpg.Pool, rows: list[tuple[str, str, int]]) -> None:
    await pool.executemany("INSERT INTO usage_counters (name, period, count) VALUES ($1, $2, $3)", rows)


async def test_the_answer_is_exactly_the_allow_list(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await counters(pool, [("checks_quick", TODAY, 40), ("checks_deep", TODAY, 2), ("checks_deep", "2025-01-03", 9)])
    await contributor(pool, "lamp_fan", 7, "approved")
    response = await client.get("/v1/stats/live")
    assert response.status_code == 200
    stats = response.json()
    assert_shape(stats, SHAPE)
    assert stats["checks"] == {
        "today": {"value": 42, "fewer_than_5": False},
        "this_month": {"value": 42, "fewer_than_5": False},
        "all_time": {"value": 51, "fewer_than_5": False},
    }
    assert stats["leaderboard"] == [
        {"rank": 1, "nickname": "lamp_fan", "verified_reports": {"value": 7, "fewer_than_5": False}}
    ]


async def test_counts_below_five_say_only_fewer_than_five(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await donate(pool, [50_000] * 3)  # €0.15 from three donations
    await counters(pool, [("checks_quick", TODAY, 4)])
    stats = (await client.get("/v1/stats/live")).json()
    small = {"value": None, "fewer_than_5": True}
    assert stats["checks"]["today"] == small
    assert stats["checks"]["all_time"] == small
    assert stats["funding"]["supporters"] == small
    assert stats["funding"]["free_checks"] == small  # €0.15 at €0.05 a check
    assert stats["verified_reports"] == small
    assert stats["database"]["shops"] == small


async def test_free_checks_left_are_checks_never_euros(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def left(pool: asyncpg.Pool) -> int:
        return 3_120_000  # €3.12

    monkeypatch.setattr(live_stats, "free_budget_left_micro_eur", left)
    stats = (await client.get("/v1/stats/live")).json()
    assert stats["free_checks_left_today"] == {"value": 62, "fewer_than_5": False, "available": True}
    assert "3.12" not in json.dumps(stats)


async def test_the_leaderboard_shows_approved_nicknames_with_five_verified_reports(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    assert (await client.get("/v1/stats/live")).json()["leaderboard"] == []
    await contributor(pool, "approved_few", 4, "approved")
    await contributor(pool, "pending_many", 50, "pending")
    await contributor(pool, "rejected_many", 50, "rejected")
    await contributor(pool, "private_many", 50, "approved", public=False)
    for i in range(12):
        await contributor(pool, f"helper_{i:02}", 5 + i, "approved")
    live_stats.clear()
    board = (await client.get("/v1/stats/live")).json()["leaderboard"]
    assert [e["nickname"] for e in board] == [f"helper_{i:02}" for i in range(11, 1, -1)]
    assert [e["rank"] for e in board] == list(range(1, 11))
    # The apps' leaderboard keeps showing pending nicknames, never rejected ones.
    handles = {e["handle"] for e in (await client.get("/v1/contributors/leaderboard")).json()["entries"]}
    assert "pending_many" in handles
    assert "rejected_many" not in handles


async def test_verified_reports_are_settled_and_agreeing(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    rows = [("p", 4, True)] * 3 + [("p", 0, True), ("q", 3, True)] * 1 + [("p", 4, False), ("h", 1, True)] * 2
    await pool.executemany(
        "INSERT INTO report_labels (page_key, anonymous, tier, level, weight, settled, created_day) "
        "VALUES ($1, TRUE, 'deep', $2, 1, $3, CURRENT_DATE)",
        rows,
    )
    await pool.execute(
        "INSERT INTO report_answers (page_key, level, tier, labels, weight, held, by_person, settled_on) VALUES "
        "('p', 4, 'deep', 4, 3, FALSE, FALSE, CURRENT_DATE), ('q', 3, 'deep', 1, 1, FALSE, TRUE, CURRENT_DATE), "
        "('h', 1, 'deep', 2, 2, TRUE, FALSE, CURRENT_DATE)"
    )
    stats = (await client.get("/v1/stats/live")).json()
    # p: three agreeing, settled (one disagreed, two still open); q: one, by a reviewer; h: held.
    assert stats["verified_reports"] == {"value": None, "fewer_than_5": True}
    assert await live_stats._verified_reports(pool) == 4


async def test_nothing_about_a_person_a_page_or_a_shop(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await pool.executemany(
        "INSERT INTO shops (domain, first_seen, last_seen, platform) VALUES ($1, CURRENT_DATE, CURRENT_DATE, 'x')",
        [(f"needle-{i}.example",) for i in range(6)],
    )
    await pool.execute(
        "INSERT INTO report_labels (page_key, anonymous, tier, level, weight, settled, created_day, comment) "
        "VALUES ('needle-page-key', TRUE, 'deep', 4, 1, TRUE, CURRENT_DATE, 'needle comment by a shopper')"
    )
    await pool.execute(
        "INSERT INTO usage_events (kind, route, attrs) VALUES ('check', 'extension', "
        '\'{"tier": "deep", "outcome": "ok", "payment": "pass"}\')'
    )
    stats = await client.get("/v1/stats/live", headers={"X-Forwarded-For": "203.0.113.77"})
    page = await client.get("/live", headers={"X-Forwarded-For": "203.0.113.77"})
    assert stats.json()["database"]["shops"] == {"value": 6, "fewer_than_5": False}
    for body in (stats.text, page.text):
        assert "needle" not in body
        assert "203.0.113" not in body
        assert not re.search(r"\b(pass|ip|url|domain|comment|handle|key)\b\"\s*:", body)


async def test_cached_for_a_minute(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    compute = live_stats.compute

    async def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return await compute(*args, **kwargs)

    monkeypatch.setattr(live_stats, "compute", counting)
    first = await client.get("/v1/stats/live")
    await counters(pool, [("checks_quick", TODAY, 100)])
    await client.get("/live")
    second = await client.get("/v1/stats/live")
    assert len(calls) == 1
    assert first.json() == second.json()
    assert first.headers["cache-control"] == "public, max-age=60"
    monkeypatch.setattr(live_stats._cache, "at", live_stats._cache.at - live_stats.TTL_S)
    third = await client.get("/v1/stats/live")
    assert len(calls) == 2
    assert third.json()["checks"]["today"]["value"] == 100


@pytest.mark.parametrize("path", ["/live", "/live?stream=1", "/v1/stats/live"])
async def test_no_cookies(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path, headers={"Origin": "https://unfret.net", "Cookie": "session=abc"})
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert not client.cookies


@pytest.mark.parametrize("path", ["/live", "/live?stream=1"])
async def test_the_page_loads_nothing_from_another_origin(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    page = response.text
    assert response.headers["cache-control"] == "public, max-age=60"
    # No address with a scheme or a protocol-relative one, in any attribute, the CSS or the script.
    assert not re.search(r"(https?:)?//[a-z0-9.-]+\.[a-z]{2,}", page, re.IGNORECASE)
    assert not re.search(r"<(link|img|iframe|object|embed)\b", page)
    assert not re.search(r"<script\b[^>]*\bsrc=", page)
    assert "@import" not in page
    assert "url(" not in page
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "unsafe-inline" not in policy
    assert policy.count("'sha256-") == 2  # the one script and the one style


async def test_stream_mode_has_no_navigation(client: httpx.AsyncClient) -> None:
    page = (await client.get("/live")).text
    stream = (await client.get("/live?stream=1")).text
    assert "<nav>" in page
    assert '<html lang="en" class="page">' in page
    assert "<nav>" not in stream
    assert '<html lang="en" class="stream">' in stream
    data = re.search(r'<script type="application/json" id="live-data">(.*?)</script>', stream, re.DOTALL)
    assert data is not None
    assert_shape(json.loads(data.group(1)), SHAPE)


async def test_offensive_nicknames_are_refused_and_new_ones_wait_for_review(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    key = new_key()
    for bad in ("sh1t_lord", "Fuck-it", "kanker_x"):
        refused = await client.post("/v1/contributors", json={"contributor_key": key, "handle": bad})
        assert refused.status_code == 400, bad
    made = await client.post("/v1/contributors", json={"contributor_key": key, "handle": "lamp_fan", "public": True})
    assert made.status_code == 200
    assert await pool.fetchval("SELECT handle_review FROM contributors WHERE handle = 'lamp_fan'") == "pending"

    assert (await client.post("/v1/admin/nicknames/lamp_fan/approve")).status_code == 401
    listed = await client.get("/v1/admin/nicknames", headers=ADMIN)
    assert listed.json() == [{"handle": "lamp_fan", "review": "pending", "public": True, "agreed": 0}]
    approved = await client.post("/v1/admin/nicknames/lamp_fan/approve", headers=ADMIN)
    assert approved.json() == {"handle": "lamp_fan", "review": "approved"}
    assert (await client.post("/v1/admin/nicknames/nobody/approve", headers=ADMIN)).status_code == 404

    # Saving the same nickname keeps its review; a new one is pending again.
    await client.post("/v1/contributors", json={"contributor_key": key, "handle": "lamp_fan", "public": False})
    assert await pool.fetchval("SELECT handle_review FROM contributors WHERE handle = 'lamp_fan'") == "approved"
    await client.post("/v1/contributors", json={"contributor_key": key, "handle": "lamp_friend"})
    assert await pool.fetchval("SELECT handle_review FROM contributors WHERE handle = 'lamp_friend'") == "pending"
    rejected = await client.post("/v1/admin/nicknames/lamp_friend/reject", headers=ADMIN)
    assert rejected.json()["review"] == "rejected"
    assert (await client.get("/v1/admin/nicknames?review=rejected", headers=ADMIN)).json()[0]["handle"] == (
        "lamp_friend"
    )


async def test_checks_that_answered_are_counted_a_day_and_tier(pool: asyncpg.Pool) -> None:
    at = datetime(2026, 11, 24, 23, 59, tzinfo=UTC)
    events: list[Any] = [
        (at, "check", "api", {"tier": "deep", "outcome": "ok"}, None),
        (at, "check", "site", {"tier": "deep", "outcome": "ok"}, None),
        (at, "check", "app", {"tier": "quick", "outcome": "ok"}, None),
        (at, "check", "app", {"tier": "quick", "outcome": "refused"}, None),
        (at, "check", "app", {"outcome": "error"}, None),
        (at, "mcp_tools_list", "mcp-remote", {}, None),
    ]
    await usage._write(pool, events)
    await usage._write(pool, events[:1])
    rows = {(r["name"], r["period"]): r["count"] for r in await pool.fetch("SELECT * FROM usage_counters")}
    assert rows == {("checks_deep", "2026-11-24"): 3, ("checks_quick", "2026-11-24"): 1}


async def test_the_migration_counts_the_checks_already_recorded(pool: asyncpg.Pool) -> None:
    await _fresh_until(pool, 45)
    await pool.execute(
        "INSERT INTO usage_events (at, kind, route, attrs) VALUES "
        "('2026-11-20 10:00+00', 'check', 'api', '{\"tier\": \"deep\", \"outcome\": \"ok\"}'), "
        "('2026-11-20 23:30+00', 'check', 'site', '{\"tier\": \"deep\", \"outcome\": \"ok\"}'), "
        "('2026-11-21 08:00+00', 'check', 'app', '{\"tier\": \"quick\", \"outcome\": \"ok\"}'), "
        "('2026-11-21 08:00+00', 'check', 'app', '{\"tier\": \"quick\", \"outcome\": \"unreadable\"}')"
    )
    await pool.execute("INSERT INTO contributors (key_hash, handle) VALUES ('\\x00', 'old_name')")
    await migrate(pool)
    rows = {(r["name"], r["period"]): r["count"] for r in await pool.fetch("SELECT * FROM usage_counters")}
    assert rows == {("checks_deep", "2026-11-20"): 2, ("checks_quick", "2026-11-21"): 1}
    assert await pool.fetchval("SELECT handle_review FROM contributors") == "pending"


async def test_funding_is_this_months_donations_from_the_ledger(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    first = datetime.now(UTC).date().replace(day=1)
    await donate(pool, [10_000_000] * 4 + [20_000_000], first)
    await donate(pool, [99_000_000], first - timedelta(days=1))
    stats = (await client.get("/v1/stats/live")).json()
    assert stats["funding"] == {
        "goal_eur": 90.0,
        "raised_eur": 60.0,
        "check_cost_eur": 0.05,
        "free_checks": {"value": 1200, "fewer_than_5": False},
        "supporters": {"value": 5, "fewer_than_5": False},
    }
    assert "note" not in json.dumps(stats)


async def test_a_donation_shows_within_a_cache_period(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = (await client.get("/v1/stats/live")).json()
    await spend.add_donation(pool, Decimal(25), "by hand")
    monkeypatch.setattr(live_stats._cache, "at", live_stats._cache.at - live_stats.TTL_S)
    after = (await client.get("/v1/stats/live")).json()
    assert after["funding"]["raised_eur"] == before["funding"]["raised_eur"] + 25
    left_before = before["free_checks_left_today"]["value"] or 0
    assert after["free_checks_left_today"]["available"]
    assert after["free_checks_left_today"]["value"] > left_before
