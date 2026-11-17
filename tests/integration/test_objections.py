"""
Requests from shop owners (``/objection``): stored as sent and nothing more, limited per visitor, queued.
"""

import json
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs

from unfret import objections
from unfret.config import settings
from unfret.routes import objections as objection_routes
from unfret.visitors import DailyLimit

ADMIN = {"Authorization": "Bearer operator-token"}
REQUEST = {
    "kind": "correction",
    "site": "https://www.shop.example/products/lamp?utm_source=mail&ref=42#reviews",
    "message": "We are a real shop since 2009.\nOur company number is on the contact page.",
    "reply_to": "owner@shop.example",
}


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    monkeypatch.setattr(objection_routes, "_requests", DailyLimit(5, 200))


async def test_a_request_is_stored_as_sent_and_nothing_more(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    with capture_logs() as logs:
        sent = await client.post("/v1/objections", json=REQUEST)

    assert sent.status_code == 201, sent.text
    assert sent.json() == {"status": "received", "answer_within_days": 30}
    row = dict(await pool.fetchrow("SELECT * FROM objections"))
    assert row["kind"] == "correction"
    assert row["site"] == "shop.example/products/lamp"  # no scheme, query or fragment
    assert row["message"] == REQUEST["message"]
    assert row["reply_to"] == "owner@shop.example"
    assert row["received_on"] == datetime.now(UTC).date()
    stored = json.dumps(row, default=str)
    assert "127.0.0.1" not in stored and "utm_source" not in stored and "#reviews" not in stored
    # The log says that a request came in, not what or from where.
    assert [entry["event"] for entry in logs] == ["objection_received"]
    assert "shop.example" not in json.dumps(logs, default=str) and "127.0.0.1" not in json.dumps(logs, default=str)


async def test_the_table_has_no_column_for_who_sent_it(pool: asyncpg.Pool) -> None:
    columns = {
        r["column_name"]
        for r in await pool.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'objections'")
    }
    assert columns == {"id", "kind", "site", "message", "reply_to", "received_on"}


async def test_the_reply_address_and_message_are_optional(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    sent = await client.post("/v1/objections", json={"kind": "removal", "site": "shop.example"})

    assert sent.status_code == 201
    row = await pool.fetchrow("SELECT site, message, reply_to FROM objections")
    assert dict(row) == {"site": "shop.example", "message": "", "reply_to": None}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"site": "not a site"}, "BAD_SITE"),
        ({"site": "javascript:alert(1)//x.example"}, "BAD_SITE"),
        ({"site": "localhost"}, "BAD_SITE"),
        ({"reply_to": "owner at shop"}, "BAD_REPLY_TO"),
        ({"kind": "delete_everything"}, "VALIDATION_ERROR"),
        ({"message": "x" * 2001}, "VALIDATION_ERROR"),
        ({"site": "s" * 501 + ".example"}, "VALIDATION_ERROR"),
    ],
)
async def test_a_request_is_validated(
    client: httpx.AsyncClient, pool: asyncpg.Pool, change: dict[str, str], code: str
) -> None:
    refused = await client.post("/v1/objections", json={**REQUEST, **change})

    assert refused.status_code == 422, refused.text
    assert refused.json()["error_code"] == code
    assert await pool.fetchval("SELECT COUNT(*) FROM objections") == 0


async def test_requests_per_visitor_are_limited_and_typos_dont_count(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(objection_routes, "_requests", DailyLimit(2, 200))

    for _ in range(3):
        assert (await client.post("/v1/objections", json={**REQUEST, "site": "no site"})).status_code == 422
    assert (await client.post("/v1/objections", json=REQUEST)).status_code == 201
    assert (await client.post("/v1/objections", json=REQUEST)).status_code == 201
    over = await client.post("/v1/objections", json=REQUEST)

    assert over.status_code == 429
    assert "hello@unfret.net" in over.json()["detail"]
    assert await pool.fetchval("SELECT COUNT(*) FROM objections") == 2


async def test_a_full_queue_says_to_email_instead(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(objections, "MAX_OPEN", 1)
    assert (await client.post("/v1/objections", json=REQUEST)).status_code == 201

    full = await client.post("/v1/objections", json=REQUEST)

    assert full.status_code == 503 and full.json()["error_code"] == "QUEUE_FULL"
    assert "hello@unfret.net" in full.json()["detail"]


async def test_the_operator_reads_the_queue_and_deletes_a_handled_request(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await client.post("/v1/objections", json=REQUEST)
    await client.post("/v1/objections", json={"kind": "removal", "site": "other.example", "message": "+31 30 1234567"})

    assert (await client.get("/v1/admin/objections")).status_code == 401
    queue = (await client.get("/v1/admin/objections", headers=ADMIN)).json()
    assert queue["open"] == 2
    first, second = queue["items"]
    assert first["site"] == "shop.example/products/lamp" and first["reply_to"] == "owner@shop.example"
    assert second["kind"] == "removal" and second["received_on"] == datetime.now(UTC).date().isoformat()

    assert (await client.delete(f"/v1/admin/objections/{first['id']}")).status_code == 401
    assert (await client.delete(f"/v1/admin/objections/{first['id']}", headers=ADMIN)).json() == {"deleted": True}
    assert (await client.delete(f"/v1/admin/objections/{first['id']}", headers=ADMIN)).status_code == 404
    assert [r["id"] for r in await pool.fetch("SELECT id FROM objections")] == [second["id"]]


async def test_open_requests_are_deleted_by_housekeeping(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await client.post("/v1/objections", json=REQUEST)
    today = datetime.now(UTC).date()

    assert await objections.purge(pool, settings.objection_days, today=today) == 0
    later = today + timedelta(days=settings.objection_days + 1)
    assert await objections.purge(pool, settings.objection_days, today=later) == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM objections") == 0
