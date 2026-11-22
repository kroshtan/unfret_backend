"""
Anonymous usage events (unfret.usage) and the stats views: nothing that identifies a person, a pass or a page.

The schema tests fail on purpose when a column or an attribute is added: adding one is a privacy decision (the
privacy page and README say exactly what is stored), not a refactor.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from tests.integration.test_check_pass import new_pass
from tests.integration.test_keys import bearer, solve
from tests.integration.test_mcp_remote import mcp_client
from unfret import mcp_remote, usage
from unfret.config import settings
from unfret.main import app
from unfret.routes import analyze, check

PAGE = {"url": "https://usage-probe.shop/products/needle-7f3a", "page_content": "<h1>Sweater</h1>"}
EXTENSION = {"Origin": "chrome-extension://abcdefghijklmnop"}

# Exactly these columns. A new one (a pass id, a hash, an address, a client...) fails here on purpose.
EVENT_COLUMNS = {"id", "at", "kind", "route", "attrs"}
PROBE_COLUMNS = {"id", "at", "target", "endpoint", "status", "latency_ms", "ok"}
# Exactly these attributes, each a short enum or a yes/no.
APPROVED_ATTRS = {
    "source",
    "developer",
    "free_checks",
    "tier",
    "payment",
    "network",
    "outcome",
    "returning",
    "method",
    "purpose",
    "provider",
    "currency",
    "tool",
}
IDENTIFYING = frozenset(
    {"id", "key", "hash", "ip", "addr", "wallet", "pubkey", "payer", "account", "url", "domain", "host"}
    | {"agent", "client", "token", "signature", "order", "email", "country", "beneficiary", "code"}
)


def identifying(name: str) -> bool:
    return any(part in IDENTIFYING or part.removesuffix("s") in IDENTIFYING for part in name.split("_"))


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(check, "_rate", check.RateLimit(1000))
    monkeypatch.setattr(mcp_remote, "_bad_passes", mcp_remote.RateLimit(1000))


@pytest.fixture
async def events(pool: asyncpg.Pool) -> AsyncIterator[None]:
    usage.dropped.clear()
    usage.start(pool)
    yield
    await usage.stop()


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    graph = FakeGraph()
    graph.result = {"score": 60, "explanation": "A quick look.", "skill_results": []}
    monkeypatch.setattr(app, "quick_graph", graph)
    return graph


async def stored(pool: asyncpg.Pool, kind: str | None = None) -> list[dict[str, Any]]:
    await usage.flush()
    rows = await pool.fetch("SELECT * FROM usage_events ORDER BY id")
    found = [{**dict(row), "attrs": json.loads(row["attrs"])} for row in rows]
    return [row for row in found if kind is None or row["kind"] == kind]


async def test_the_tables_have_no_identifying_column(pool: asyncpg.Pool) -> None:
    for table, expected in (("usage_events", EVENT_COLUMNS), ("uptime_probes", PROBE_COLUMNS)):
        columns = await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1",
            table,
        )
        assert {c["column_name"] for c in columns} == expected, table


def test_every_attribute_is_an_approved_coarse_enum() -> None:
    names = {name for allowed in usage.KINDS.values() for name in allowed}
    assert names == APPROVED_ATTRS
    for name in names:
        assert not identifying(name), name
    for allowed in usage.KINDS.values():
        for values in allowed.values():
            assert isinstance(values, frozenset) and 0 < len(values) <= 10
            assert all(isinstance(v, bool) or (isinstance(v, str) and len(v) <= 20) for v in values)
    assert all(len(route) <= 20 for route in usage.ROUTES)


async def test_an_event_has_a_real_time_and_only_its_enums(pool: asyncpg.Pool, events: None) -> None:
    before = datetime.now(UTC)
    usage.record("pass_created", source="website", developer=False, free_checks=True)
    usage.record("pass_created", source="https://example.test")  # not an enum value: dropped
    usage.record("pass_created", source="api", key_id="abc")  # not an attribute: dropped
    usage.record("no_such_kind")
    usage.record("check", "somewhere")  # not a route

    rows = await stored(pool)

    assert len(rows) == 1
    assert rows[0]["kind"] == "pass_created" and rows[0]["route"] is None
    assert rows[0]["attrs"] == {"source": "website", "developer": False, "free_checks": True}
    assert before - timedelta(seconds=1) <= rows[0]["at"] <= datetime.now(UTC) + timedelta(seconds=1)
    assert usage.dropped["invalid"] == 4


async def test_switched_off_nothing_is_recorded(
    pool: asyncpg.Pool, events: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "usage_events_enabled", False)
    usage.record("oauth_connected")
    assert await stored(pool) == []


async def test_a_new_pass_is_counted_by_its_source_and_nothing_else(
    client: httpx.AsyncClient, pool: asyncpg.Pool, events: None
) -> None:
    async def solved() -> dict[str, str]:
        challenge = (await client.get("/v1/keys/challenge")).json()
        return {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}

    made = (await client.post("/v1/keys", json=await solved(), headers=EXTENSION)).json()
    assert (await client.post("/v1/keys", json={**await solved(), "source": "android"})).status_code == 201
    await new_pass(client)
    body = await solved()

    rows = await stored(pool, "pass_created")

    assert [r["attrs"]["source"] for r in rows] == ["extension", "android", "api"]
    dumped = json.dumps([{**r, "at": str(r["at"])} for r in rows])
    assert made["key_id"] not in dumped and made["key"] not in dumped
    assert (await client.post("/v1/keys", json={**body, "source": "elsewhere"})).status_code == 422


async def test_checks_record_route_tier_payment_and_outcome(
    client: httpx.AsyncClient, pool: asyncpg.Pool, events: None, quick: FakeGraph
) -> None:
    issued = await new_pass(client, developer=True)
    await pool.execute("UPDATE api_keys SET created_on = CURRENT_DATE - 3 WHERE id = $1", issued["key_id"])
    fresh = await new_pass(client)

    assert (await client.post("/v1/check", json={**PAGE, "tier": "deep"}, headers=bearer(issued["key"]))).is_success
    assert (await client.post("/v1/check", json={**PAGE, "tier": "quick"}, headers=bearer(fresh["key"]))).is_success
    assert (await client.post("/v1/analyze", json={**PAGE, "tier": "quick"}, headers=EXTENSION)).is_success
    empty = await client.post("/v1/check", json={**PAGE, "tier": "deep"}, headers=bearer(fresh["key"]))
    assert empty.status_code == 402
    await client.post("/v1/check", json={})  # an x402 price quote (402 without a payment): not a check

    rows = await stored(pool, "check")

    assert [(r["route"], r["attrs"]) for r in rows] == [
        ("api", {"tier": "deep", "payment": "free", "outcome": "ok", "returning": True}),
        ("api", {"tier": "quick", "payment": "free", "outcome": "ok", "returning": False}),
        ("extension", {"tier": "quick", "payment": "free", "outcome": "ok"}),
        ("api", {"tier": "deep", "payment": "pass", "outcome": "refused", "returning": False}),
    ]
    dumped = json.dumps([{**r, "at": str(r["at"])} for r in rows])
    for secret in (issued["key_id"], fresh["key_id"], "usage-probe", "needle-7f3a", "chrome-extension"):
        assert secret not in dumped


async def test_an_unreadable_page_is_its_own_outcome(
    client: httpx.AsyncClient, pool: asyncpg.Pool, events: None, quick: FakeGraph
) -> None:
    quick.result = {"score": -1, "explanation": "Couldn't assess.", "skill_results": []}
    await client.post("/v1/analyze/stream", json={**PAGE, "tier": "quick"}, headers=EXTENSION)
    rows = await stored(pool, "check")
    assert rows[0]["attrs"]["outcome"] == "unreadable" and rows[0]["route"] == "extension"


async def test_mcp_tool_calls_are_counted_by_tool_and_route(
    client: httpx.AsyncClient, pool: asyncpg.Pool, events: None, quick: FakeGraph
) -> None:
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with mcp_remote.running(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await started.wait()
    try:
        issued = await new_pass(client)
        async with mcp_client(issued["key"]) as session:
            await session.list_tools()
            await session.call_tool("check_store_quick", {"url": PAGE["url"], "page_content": PAGE["page_content"]})
    finally:
        stop.set()
        await task

    assert len(await stored(pool, "mcp_tools_list")) >= 1
    assert [r["attrs"] for r in await stored(pool, "mcp_tool_call")] == [{"tool": "check_store_quick"}]
    checks = await stored(pool, "check")
    assert [(r["route"], r["attrs"]["tier"], r["attrs"]["outcome"]) for r in checks] == [("mcp-remote", "quick", "ok")]


async def test_a_failing_write_never_breaks_a_request(
    client: httpx.AsyncClient, pool: asyncpg.Pool, events: None, quick: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object) -> None:
        raise asyncpg.PostgresError("down")

    monkeypatch.setattr(usage, "_write", broken)
    response = await client.post("/v1/analyze", json={**PAGE, "tier": "quick"})
    await usage.flush()

    assert response.status_code == 200
    assert usage.dropped["write"] == 1


async def test_a_failing_recorder_never_breaks_a_request(
    client: httpx.AsyncClient, events: None, quick: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(usage, "record", broken)
    assert (await client.post("/v1/analyze", json={**PAGE, "tier": "quick"})).status_code == 200


async def test_a_full_queue_drops_without_waiting(events: None, monkeypatch: pytest.MonkeyPatch) -> None:
    assert usage._writer.queue is not None
    monkeypatch.setattr(usage._writer, "queue", asyncio.Queue(maxsize=1))
    usage.record("oauth_connected")
    usage.record("oauth_connected")
    assert usage.dropped["full"] == 1


async def test_old_events_and_probes_are_purged(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "INSERT INTO usage_events (at, kind) VALUES (NOW() - INTERVAL '401 days', 'old'), (NOW(), 'new')"
    )
    await pool.execute(
        "INSERT INTO uptime_probes (at, target, endpoint, ok) "
        "VALUES (NOW() - INTERVAL '401 days', 'dev', 'ready', TRUE)"
    )
    await usage.purge(pool, 400)
    assert [r["kind"] for r in await pool.fetch("SELECT kind FROM usage_events")] == ["new"]
    assert await pool.fetchval("SELECT COUNT(*) FROM uptime_probes") == 0


async def test_the_stats_views_are_aggregates_and_the_role_sees_only_them(pool: asyncpg.Pool) -> None:
    for n in range(3):
        await pool.execute(
            "INSERT INTO api_keys (id, key_hash, recovery_hash) VALUES ($1, $2, $2)", f"abcdefghi{'jkl'[n]}", bytes(32)
        )
    await pool.execute(
        "INSERT INTO wallets (pubkey, balance_usd_micros, purchased) VALUES ('key:abcdefghij', 5, TRUE)"
    )

    assert [dict(r) for r in await pool.fetch("SELECT passes, developer FROM stats.passes_daily")] == [
        {"passes": 3, "developer": False}
    ]
    cohort = await pool.fetchrow("SELECT passes, with_balance, purchased FROM stats.pass_cohorts")
    assert dict(cohort) == {"passes": 3, "with_balance": 1, "purchased": 1}

    views = await pool.fetch("SELECT viewname, definition FROM pg_views WHERE schemaname = 'stats'")
    assert {v["viewname"] for v in views} == {
        "usage_events",
        "uptime_probes",
        "passes_daily",
        "pass_cohorts",
        "card_payments_daily",
        "crypto_sales_daily",
        "x402_checks_daily",
    }
    for view in views:
        if view["viewname"] not in {"usage_events", "uptime_probes"}:
            assert "GROUP BY" in view["definition"].upper(), view["viewname"]  # one row per group, never per pass
        columns = await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'stats' AND table_name = $1",
            view["viewname"],
        )
        for column in (c["column_name"] for c in columns):
            assert column == "id" or not identifying(column), (view["viewname"], column)

    role = "unfret_stats_read"
    assert not await pool.fetchval("SELECT rolcanlogin FROM pg_roles WHERE rolname = $1", role)
    tables = await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    for table in (t["tablename"] for t in tables):
        assert not await pool.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", role, f"public.{table}"), table
    for view in views:
        assert await pool.fetchval("SELECT has_table_privilege($1, $2, 'SELECT')", role, f"stats.{view['viewname']}")
    probe = "unfret_probe_write"
    assert await pool.fetchval("SELECT has_table_privilege($1, 'public.uptime_probes', 'INSERT')", probe)
    assert not await pool.fetchval("SELECT has_table_privilege($1, 'public.uptime_probes', 'SELECT')", probe)
