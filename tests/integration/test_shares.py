"""
Shared results: a shopper's own check made public as a link, anonymously, and taken down again.

The analysis graph is the test double from conftest; its result carries a verdict with quotes from the page.
"""

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.conftest import SESSION_SECRET, FakeGraph
from unfret import shares
from unfret.config import settings
from unfret.facts.schema import PageFacts
from unfret.routes import shares as share_routes
from unfret.visitors import DailyLimit, RateLimit

PRICE = settings.analysis_price_usd_micros
ADMIN = {"Authorization": "Bearer operator-token"}
URL = "https://shop.example/products/led-mask?variant=2&utm_source=newsletter#reviews"
PRODUCT = "https://shop.example/products/led-mask?variant=2"
PAGE = {"url": URL, "page_content": "<html><body><h1>LED mask</h1><p>90% off, only today!</p></body></html>"}
Wallet = Callable[[int], Awaitable[tuple[str, dict[str, str]]]]


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    monkeypatch.setattr(share_routes, "_creations", DailyLimit(20, 1000))
    monkeypatch.setattr(share_routes, "_objections", DailyLimit(20, 1000))
    monkeypatch.setattr(share_routes, "_previews", RateLimit(1000))


@pytest.fixture
def warned(graph: FakeGraph) -> FakeGraph:
    graph.result = {
        "score": 12,
        "explanation": "Looks like a scam: fake discount.",
        "verdict": {
            "band": "junk_or_scam",
            "label": "Many warning signs",
            "headline": "Many warning signs: a generic product with a discount it never had.",
            "reasons": [
                {
                    "type": "big_discount",
                    "title": "Too-good discount",
                    "detail": "A 90% discount on a 'was' price that may never have been charged.",
                    "severity": "high",
                },
                {
                    "type": "implausible_claims",
                    "title": "Hard-to-believe claims",
                    "detail": "The page makes claims that are hard to believe: “clinically proven in 3 days”.",
                    "severity": "medium",
                },
            ],
            "positives": [],
            "language": "en",
        },
        "assessment": {
            "risk_score": 0.9,
            "confidence": 0.8,
            "model_version": "test",
            "flags": [{"type": "content.pushy", "label": "Pushy sales talk", "evidence": "only today!"}],
        },
        "facts": PageFacts(title="LED <b>mask</b>"),
        "skill_results": [],
    }
    return graph


async def analysed(client: httpx.AsyncClient, wallet: Wallet, body: dict[str, Any] | None = None) -> dict[str, Any]:
    _, headers = await wallet(10 * PRICE)
    response = await client.post("/v1/analyze", json=body or PAGE, headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def share_body(result: dict[str, Any], url: str = URL) -> dict[str, Any]:
    return {"share_token": result["share_token"], "url": url, "result": result}


async def shared(client: httpx.AsyncClient, result: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/v1/shares", json=share_body(result))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def test_preview_share_and_view_without_anything_about_the_shopper(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    result = await analysed(client, wallet)
    assert result["share_token"] and result["report_token"]

    preview = await client.post("/v1/shares/preview", json=share_body(result))
    assert preview.status_code == 200, preview.text
    shown = preview.json()
    assert shown["notice"] == (
        "Anyone with the link sees this verdict and the product link. It never says who checked."
    )
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 0  # a preview stores nothing

    created = await shared(client, result)
    assert created["link"] == f"/r/{created['id']}" and len(created["id"]) == 26
    assert created["delete_code"]

    public = await client.get(f"/v1/shares/{created['id']}")
    assert public.status_code == 200
    assert public.headers["x-robots-tag"].startswith("noindex")
    body = public.json()
    assert {k: v for k, v in body.items() if k not in {"id", "link", "expires_at"}} == shown
    assert body["product_url"] == PRODUCT and body["host"] == "shop.example"
    assert body["title"] == "LED <b>mask</b>" and body["label"] == "Unfret would warn you about this listing"
    reasons = {r["title"]: r["detail"] for r in body["result"]["verdict"]["reasons"]}
    assert reasons["Hard-to-believe claims"] == ""  # the page's words are left out, the finding stays
    assert body["result"]["assessment"]["flags"] == [{"label": "Pushy sales talk", "evidence": "", "kind": "signal"}]
    text = json.dumps(body)
    assert "clinically" not in text and "only today" not in text and "scam" not in text.lower()
    assert "score" not in text and "explanation" not in text

    page = await client.get(created["link"])
    assert page.status_code == 200
    assert page.headers["x-robots-tag"].startswith("noindex")
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert '<meta name="robots" content="noindex, nofollow">' in page.text
    assert f'<a href="{PRODUCT}" rel="nofollow noopener noreferrer">' in page.text
    assert "Checked by an Unfret user on " in page.text and "the page may have changed" in page.text
    assert "LED &lt;b&gt;mask&lt;/b&gt;" in page.text and "<b>mask" not in page.text

    row = dict(await pool.fetchrow("SELECT * FROM shared_results"))
    assert row["checked_on"] == datetime.now(UTC).date()
    assert row["expires_at"].time().isoformat() == "00:00:00"  # a day boundary: no time of the share
    assert row["delete_hash"] == shares.code_hash(created["delete_code"])
    everything = json.dumps(row, default=str)
    assert created["delete_code"] not in everything and result["report_token"] not in everything
    assert "utm_source" not in everything and "#reviews" not in everything


async def test_the_table_has_no_column_that_could_name_who_checked(pool: asyncpg.Pool) -> None:
    columns = {
        r["column_name"]
        for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'shared_results'"
        )
    }
    assert columns == {
        "id",
        "host",
        "product_url",
        "title",
        "checked_on",
        "result",
        "delete_hash",
        "expires_at",
        "hidden_reason",
    }


async def test_a_streamed_result_can_be_shared(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, wallet: Wallet
) -> None:
    _, headers = await wallet(10 * PRICE)
    async with client.stream("POST", "/v1/analyze/stream", json=PAGE, headers=headers) as response:
        events = [line async for line in response.aiter_lines() if line.startswith("data: ")]
    result = json.loads(events[-1].removeprefix("data: "))
    assert result["share_token"]

    created = await shared(client, result)
    assert (await client.get(f"/v1/shares/{created['id']}")).json()["title"] == "shop.example"  # no title: the shop


async def test_a_share_is_sent_without_any_account(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    _, headers = await wallet(10 * PRICE)
    result = (await client.post("/v1/analyze", json=PAGE, headers=headers)).json()

    for path in ("/v1/shares", "/v1/shares/preview"):
        refused = await client.post(path, json=share_body(result), headers=headers)
        assert refused.status_code == 400 and refused.json()["error_code"] == "AUTHORIZATION_NOT_ALLOWED"
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 0


async def test_a_forged_tampered_or_old_token_is_refused(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    result = await analysed(client, wallet)

    friendlier = json.loads(json.dumps(result))
    friendlier["verdict"]["band"] = "reliable"
    other_page = share_body(result, url="https://shop.example/products/other")
    forged = share_body(result) | {"share_token": result["share_token"][:-2] + "00"}
    for body in (share_body(friendlier), other_page, forged):
        refused = await client.post("/v1/shares", json=body)
        assert refused.status_code == 400 and refused.json()["error_code"] == "BAD_SHARE_TOKEN", body

    old = shares.issue_token(
        SESSION_SECRET,
        url=URL,
        title="LED mask",
        response=result,
        checkout=False,
        ttl_s=settings.share_token_ttl_s,
        now=datetime.now(UTC).timestamp() - settings.share_token_ttl_s - 60,
    )
    expired = await client.post("/v1/shares", json=share_body(result) | {"share_token": old})
    assert expired.status_code == 422 and expired.json()["error_code"] == "SHARE_TOKEN_EXPIRED"
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 0


async def test_a_check_with_the_checkout_page_is_refused(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    result = await analysed(client, wallet, {**PAGE, "checkout_content": "<p>Your cart: 1 mask, Jane Doe</p>"})

    refused = await client.post("/v1/shares", json=share_body(result))

    assert refused.status_code == 422 and refused.json()["error_code"] == "CHECKOUT_NOT_SHAREABLE"
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 0


async def test_a_page_that_wasnt_assessed_has_no_share_token(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Wallet
) -> None:
    graph.result = {"score": -1, "explanation": "Not a product page.", "skill_results": []}
    assert "share_token" not in await analysed(client, wallet)


async def test_shares_per_visitor_are_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, warned: FakeGraph, wallet: Wallet
) -> None:
    monkeypatch.setattr(share_routes, "_creations", DailyLimit(1, 1000))
    result = await analysed(client, wallet)

    await shared(client, result)
    again = await client.post("/v1/shares", json=share_body(result))

    assert again.status_code == 429


async def test_the_sharer_deletes_it_with_the_code(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    created = await shared(client, await analysed(client, wallet))
    path = f"/v1/shares/{created['id']}"

    missing = await client.delete(path)
    wrong = await client.delete(path, headers={"X-Delete-Code": "a" * 26})
    assert missing.status_code == 400 and missing.json()["error_code"] == "DELETE_CODE_REQUIRED"
    assert wrong.status_code == 403 and wrong.json()["error_code"] == "WRONG_DELETE_CODE"
    assert (await client.get(path)).status_code == 200

    deleted = await client.delete(path, headers={"X-Delete-Code": created["delete_code"].upper()})
    assert deleted.status_code == 204
    assert (await client.get(path)).status_code == 404
    assert (await client.get(created["link"])).status_code == 404
    assert (await client.delete(path, headers={"X-Delete-Code": created["delete_code"]})).status_code == 404
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 0


async def test_an_objection_hides_it_until_the_operator_reviews_it(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    result = await analysed(client, wallet)
    first, second = await shared(client, result), await shared(client, result)
    path = f"/v1/shares/{first['id']}"

    objected = await client.post(f"{path}/object", json={"reason": "  This is our   real product. "})
    assert objected.status_code == 200 and objected.json() == {"hidden": True}
    assert (await client.get(path)).status_code == 404
    assert (await client.get(first["link"])).status_code == 404
    assert (await client.post(f"{path}/object", json={})).status_code == 404  # hidden already
    assert (await client.get(f"/v1/shares/{second['id']}")).status_code == 200

    assert (await client.get("/v1/admin/shares")).status_code == 401
    [item] = (await client.get("/v1/admin/shares", headers=ADMIN)).json()["items"]
    assert item["id"] == first["id"] and item["hidden_reason"] == "Objection: This is our real product."
    everything = (await client.get("/v1/admin/shares?hidden=false", headers=ADMIN)).json()["items"]
    assert [i["id"] for i in everything] == [first["id"], second["id"]]  # hidden first

    assert (await client.post(f"/v1/admin/shares/{first['id']}/unhide", headers=ADMIN)).status_code == 200
    assert (await client.get(path)).status_code == 200
    assert (await client.post(f"/v1/admin/shares/{first['id']}/unhide", headers=ADMIN)).status_code == 404

    assert (await client.post(f"{path}/object", json={})).status_code == 200
    assert (await client.delete(f"/v1/admin/shares/{first['id']}", headers=ADMIN)).status_code == 200
    assert (await client.delete(f"/v1/admin/shares/{first['id']}", headers=ADMIN)).status_code == 404
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 1


async def test_expired_shares_are_gone_and_deleted_by_housekeeping(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, wallet: Wallet
) -> None:
    result = await analysed(client, wallet)
    old, kept = await shared(client, result), await shared(client, result)
    await pool.execute("UPDATE shared_results SET expires_at = NOW() - interval '1 day' WHERE id = $1", old["id"])

    assert (await client.get(f"/v1/shares/{old['id']}")).status_code == 404
    assert await shares.purge(pool) == 1
    assert [r["id"] for r in await pool.fetch("SELECT id FROM shared_results")] == [kept["id"]]
    assert await shares.purge(pool, now=datetime.now(UTC) + timedelta(days=settings.share_days + 1)) == 1


async def test_an_unknown_id_is_a_404(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    for path in ("/v1/shares/nope", "/r/nope", "/v1/shares/" + "a" * 26, "/r/" + "<x>"):
        assert (await client.get(path)).status_code == 404, path
