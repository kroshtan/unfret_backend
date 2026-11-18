"""``POST /v1/check`` paid with an Unfret pass, and developer passes (more free checks, for building agents)."""

from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from tests.integration.test_keys import PAGE, PRICE, balance, bearer, solve
from unfret.config import settings
from unfret.credits import free
from unfret.main import app
from unfret.routes import analyze, check

CHECK = {**PAGE, "tier": "deep"}


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(check, "_rate", check.RateLimit(1000))


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    graph = FakeGraph()
    graph.result = {"score": 60, "explanation": "A quick look.", "skill_results": []}
    monkeypatch.setattr(app, "quick_graph", graph)
    return graph


async def new_pass(client: httpx.AsyncClient, **options: bool) -> dict[str, Any]:
    challenge = (await client.get("/v1/keys/challenge")).json()
    body = {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}
    response = await client.post("/v1/keys", json={**body, **options})
    assert response.status_code == 201
    issued: dict[str, Any] = response.json()
    return issued


async def test_a_developer_pass_has_twenty_free_checks_once_a_day_per_network(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    first = await new_pass(client, developer=True)
    second = await new_pass(client, developer=True)

    assert first["free_checks"] == settings.developer_free_checks == 20
    assert await free.left(pool, first["key_id"]) == 20
    assert second["free_checks"] == 0  # past the network's developer pass for today: made empty
    # A developer pass doesn't use up the network's ordinary free passes (each has its own allowance).
    assert (await new_pass(client, free=True))["free_checks"] == settings.free_checks_per_pass


async def test_a_deep_check_with_a_pass_uses_a_free_check(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    issued = await new_pass(client, developer=True)

    response = await client.post("/v1/check", json=CHECK, headers=bearer(issued["key"]))

    assert response.status_code == 200
    verdict = response.json()
    assert verdict["action"] in {"allow", "warn", "block"}
    assert verdict["tier"] == "deep" and verdict["policy_applied"] == "balanced"
    assert verdict["charged"] is True and verdict["free_check"] is True
    assert verdict["free_checks_left"] == 19 and verdict["balance_usd_micros"] == 0
    assert "PAYMENT-RESPONSE" not in response.headers
    assert len(graph.calls) == 1
    assert await free.left(pool, issued["key_id"]) == 19


async def test_bought_checks_pay_once_the_free_ones_are_gone(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await new_pass(client)
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", issued["account"], PRICE)

    paid = await client.post("/v1/check", json=CHECK, headers=bearer(issued["key"]))

    assert paid.status_code == 200
    assert paid.json()["charged"] is True and paid.json()["free_check"] is False
    assert paid.json()["balance_usd_micros"] == 0
    assert await balance(pool, issued["account"]) == 0


async def test_an_empty_pass_is_refused_before_any_work(client: httpx.AsyncClient, graph: FakeGraph) -> None:
    issued = await new_pass(client)

    refused = await client.post("/v1/check", json=CHECK, headers=bearer(issued["key"]))

    assert refused.status_code == 402
    assert refused.json()["error_code"] == "INSUFFICIENT_CREDITS"
    assert refused.json()["metadata"]["analysis_usd_micros"] == PRICE
    assert "PAYMENT-REQUIRED" not in refused.headers  # the caller chose a pass: no x402 offer
    assert graph.calls == []


async def test_a_quick_check_with_a_pass_is_free(
    client: httpx.AsyncClient, pool: asyncpg.Pool, quick: FakeGraph
) -> None:
    issued = await new_pass(client)  # empty: quick checks need no checks

    response = await client.post("/v1/check", json={**PAGE, "tier": "quick"}, headers=bearer(issued["key"]))

    assert response.status_code == 200
    assert response.json()["tier"] == "quick" and response.json()["charged"] is False
    assert len(quick.calls) == 1


async def test_quick_checks_with_a_pass_are_limited_per_minute(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, quick: FakeGraph
) -> None:
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1))
    issued = await new_pass(client)
    body = {**PAGE, "tier": "quick"}

    assert (await client.post("/v1/check", json=body, headers=bearer(issued["key"]))).status_code == 200
    limited = await client.post("/v1/check", json=body, headers=bearer(issued["key"]))

    assert limited.status_code == 429
    assert limited.json()["metadata"]["retry_after_s"] >= 1


async def test_a_wrong_pass_is_401(client: httpx.AsyncClient) -> None:
    for token in ("ufk_aaaaaaaaaa_" + "a" * 52, "a-session-token"):
        refused = await client.post("/v1/check", json=CHECK, headers={"Authorization": f"Bearer {token}"})
        assert refused.status_code == 401
        assert refused.json()["error_code"] == "INVALID_API_KEY"


async def test_an_invalid_body_with_a_pass_is_a_422_and_costs_nothing(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    issued = await new_pass(client, developer=True)
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", issued["account"], PRICE)

    json_type = {"Content-Type": "application/json"}
    for response in (
        await client.post("/v1/check", headers=bearer(issued["key"])),
        await client.post("/v1/check", content=b"{", headers={**json_type, **bearer(issued["key"])}),
        await client.post("/v1/check", json={"tier": "deep"}, headers=bearer(issued["key"])),
    ):
        assert (response.status_code, response.json()["error_code"]) == (422, "VALIDATION_ERROR")

    assert await free.left(pool, issued["key_id"]) == 20
    assert await balance(pool, issued["account"]) == PRICE
    assert graph.calls == []


async def test_a_payment_header_is_always_x402(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": "Bearer not-a-pass", "PAYMENT-SIGNATURE": "e30="}

    response = await client.post("/v1/check", json=CHECK, headers=headers)

    assert response.status_code != 401  # the pass is ignored; x402 answers (here: payments are off)
    assert response.json()["error_code"] != "INVALID_API_KEY"


async def test_a_cached_answer_costs_a_check_except_for_the_pass_that_paid(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    first, second = await new_pass(client), await new_pass(client)
    for issued in (first, second):
        await pool.execute(
            "INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", issued["account"], 2 * PRICE
        )
    assert (await client.post("/v1/check", json=CHECK, headers=bearer(first["key"]))).json()["charged"] is True

    again = await client.post("/v1/check", json={**CHECK, "policy": "strict"}, headers=bearer(first["key"]))
    other = await client.post("/v1/check", json=CHECK, headers=bearer(second["key"]))

    assert again.json()["cached"] is True and again.json()["charged"] is False
    assert again.json()["policy_applied"] == "strict"  # the caller's policy, applied to the kept answer
    assert other.json()["cached"] is True and other.json()["charged"] is True
    assert len(graph.calls) == 1
    assert await balance(pool, first["account"]) == PRICE
    assert await balance(pool, second["account"]) == PRICE


async def test_an_empty_pass_gets_the_same_402_for_a_cached_answer(client: httpx.AsyncClient) -> None:
    payer, empty = await new_pass(client, developer=True), await new_pass(client)
    assert (await client.post("/v1/check", json=CHECK, headers=bearer(payer["key"]))).status_code == 200

    refused = await client.post("/v1/check", json=CHECK, headers=bearer(empty["key"]))

    assert refused.status_code == 402 and refused.json()["error_code"] == "INSUFFICIENT_CREDITS"


async def test_a_page_that_cant_be_assessed_is_given_back(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    graph.result = {"score": -1, "explanation": "Not a product page.", "skill_results": []}
    issued = await new_pass(client, developer=True)

    response = await client.post("/v1/check", json=CHECK, headers=bearer(issued["key"]))

    assert response.status_code == 200
    assert response.json()["assessed"] is False and response.json()["charged"] is False
    assert response.json()["free_checks_left"] == 20
    assert await free.used_today(pool) == 0


async def test_a_failed_analysis_is_given_back(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    graph.error = RuntimeError("boom")
    issued = await new_pass(client, developer=True)

    response = await client.post("/v1/check", json=CHECK, headers=bearer(issued["key"]))

    assert response.status_code == 500 and response.json()["error_code"] == "INTERNAL_ERROR"
    assert await free.left(pool, issued["key_id"]) == 20


async def test_a_page_that_cant_be_fetched_costs_nothing(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(url: str, **_: object) -> bytes:
        raise OSError("refused")

    monkeypatch.setattr(check, "fetch_public", blocked)
    issued = await new_pass(client, developer=True)

    response = await client.post(
        "/v1/check", json={"url": "https://shop.test/x", "tier": "deep"}, headers=bearer(issued["key"])
    )

    assert response.status_code == 422 and response.json()["error_code"] == "PAGE_UNAVAILABLE"
    assert await free.left(pool, issued["key_id"]) == 20
    assert graph.calls == []


async def test_the_days_budget_pauses_free_checks_on_check_too(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "free_check_cost_eur", Decimal("0.01"))
    monkeypatch.setattr(settings, "free_daily_budget_eur", Decimal("0.01"))  # one free deep check today
    first, second = await new_pass(client, developer=True), await new_pass(client, free=True)
    assert (await client.post("/v1/check", json=CHECK, headers=bearer(first["key"]))).status_code == 200

    paused = await client.post(
        "/v1/check", json={**CHECK, "url": "https://shop.test/2"}, headers=bearer(second["key"])
    )

    assert paused.status_code == 429 and paused.json()["error_code"] == "FREE_CHECKS_PAUSED"
    assert await free.left(pool, second["key_id"]) == settings.free_checks_per_pass


async def test_discovery_and_llms_txt_name_the_pass(client: httpx.AsyncClient) -> None:
    discovery = (await client.get("/.well-known/x402")).json()
    assert discovery["auth"]["developer_pass"]["free_deep_checks"] == 20
    assert "Authorization: Bearer ufk_" in (await client.get("/llms.txt")).text
