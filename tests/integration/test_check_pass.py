"""``POST /v1/check`` paid with an Unfret pass, and developer passes (more free checks, for building agents)."""

from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest

from tests.conftest import page_fetcher
from tests.integration.conftest import FakeGraph
from tests.integration.test_keys import PAGE, PRICE, balance, bearer, solve
from unfret import spend
from unfret.config import settings
from unfret.credits import free
from unfret.main import app
from unfret.net import FetchedPage
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
    # A developer pass needs a developer challenge (harder); any other pass the ordinary one.
    query = {"developer": "true"} if options.get("developer") else {}
    challenge = (await client.get("/v1/keys/challenge", params=query)).json()
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
    assert await free.left(pool, first["key_id"], api=True) == 20
    assert second["free_checks"] == 0  # past the network's developer pass for today: made empty
    assert (await new_pass(client, free=True))["free_checks"] == 0  # only a developer pass has free checks


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
    assert await free.left(pool, issued["key_id"], api=True) == 19


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

    assert await free.left(pool, issued["key_id"], api=True) == 20
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
    assert await free.left(pool, issued["key_id"], api=True) == 20


async def test_a_page_that_cant_be_fetched_costs_nothing(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(url: str, **_: object) -> bytes:
        raise OSError("refused")

    monkeypatch.setattr(check, "fetch_public_page", page_fetcher(blocked))
    issued = await new_pass(client, developer=True)

    response = await client.post(
        "/v1/check", json={"url": "https://shop.test/x", "tier": "deep"}, headers=bearer(issued["key"])
    )

    assert response.status_code == 422 and response.json()["error_code"] == "PAGE_UNAVAILABLE"
    assert await free.left(pool, issued["key_id"], api=True) == 20
    assert graph.calls == []


async def test_a_short_link_is_checked_under_the_pages_own_address(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    landed = "https://shop.test/products/lamp?id=4&utm_medium=social#top"

    async def redirected(url: str, **_: object) -> FetchedPage:
        return FetchedPage(PAGE["page_content"].encode(), landed if "short.test" in url else url)

    monkeypatch.setattr(check, "fetch_public_page", redirected)
    issued = await new_pass(client, developer=True)

    response = await client.post(
        "/v1/check", json={"url": "https://short.test/s/9x", "tier": "deep"}, headers=bearer(issued["key"])
    )
    again = await client.post(
        "/v1/check",
        json={"url": "https://shop.test/products/lamp?id=4", "tier": "deep"},
        headers=bearer(issued["key"]),
    )

    assert response.status_code == 200, response.text
    assert graph.calls[0]["url"] == "https://shop.test/products/lamp?id=4"
    assert again.status_code == 200 and again.json()["cached"] is True  # the page's own address: the same result
    assert len(graph.calls) == 1
    assert await free.left(pool, issued["key_id"], api=True) == 19  # the repeat within the hour is free


async def test_the_spend_ceiling_pauses_a_developer_pass_free_checks(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    developer = await new_pass(client, developer=True)
    await pool.execute(
        "INSERT INTO llm_spend (month, micro_eur) VALUES ($1, $2)",
        spend.month(),
        int(settings.llm_spend_ceiling_eur * 1_000_000),
    )

    paused = await client.post("/v1/check", json=CHECK, headers=bearer(developer["key"]))

    assert paused.status_code == 402 and paused.json()["error_code"] == "INSUFFICIENT_CREDITS"
    assert paused.json()["detail"] == "Free developer checks are paused until next month. Bought checks still work."
    assert await free.left(pool, developer["key_id"], api=True) == 20
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", developer["account"], PRICE)
    assert (await client.post("/v1/check", json=CHECK, headers=bearer(developer["key"]))).status_code == 200


async def test_discovery_and_llms_txt_name_the_pass(client: httpx.AsyncClient) -> None:
    discovery = (await client.get("/.well-known/x402")).json()
    assert discovery["auth"]["developer_pass"]["free_deep_checks"] == 20
    assert "Authorization: Bearer ufk_" in (await client.get("/llms.txt")).text


# ── A developer pass's free checks count on /v1/check only ─────────────────────────────────────────────────────


async def test_a_developer_pass_is_flagged_and_nothing_else_is_kept(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    developer, shopper, empty = (
        await new_pass(client, developer=True),
        await new_pass(client, free=True),
        await new_pass(client),
    )
    assert developer["developer"] is True and shopper["developer"] is False and empty["developer"] is False
    flags = dict(await pool.fetch("SELECT id, developer FROM api_keys"))
    assert flags == {developer["key_id"]: True, shopper["key_id"]: False, empty["key_id"]: False}


async def test_a_developer_pass_spends_no_free_check_on_analyze(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    developer, empty = await new_pass(client, developer=True), await new_pass(client)

    refused = await client.post("/v1/analyze", json=PAGE, headers=bearer(developer["key"]))
    as_empty = await client.post("/v1/analyze", json=PAGE, headers=bearer(empty["key"]))
    streamed = await client.post("/v1/analyze/stream", json=PAGE, headers=bearer(developer["key"]))
    url_only = await client.post(
        "/v1/analyze", json={"url": "https://shop.test/x", "tier": "deep"}, headers=bearer(developer["key"])
    )

    # Exactly what a pass with no checks left gets, before any work: a shopper's free check, needing its work.
    assert (refused.status_code, refused.json()) == (as_empty.status_code, as_empty.json())
    assert refused.status_code == 428 and refused.json()["error_code"] == "PROOF_OF_WORK_REQUIRED"
    assert streamed.status_code == 428 and url_only.status_code == 428
    assert graph.calls == []
    assert await free.left(pool, developer["key_id"], api=True) == 20 and await free.used_today(pool) == 0
    # ...while /v1/check spends them.
    checked = await client.post("/v1/check", json=CHECK, headers=bearer(developer["key"]))
    assert checked.json()["free_check"] is True and checked.json()["free_checks_left"] == 19


async def test_a_developer_pass_pays_from_its_balance_everywhere(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    developer = await new_pass(client, developer=True)
    await pool.execute(
        "INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", developer["account"], 2 * PRICE
    )

    analyzed = await client.post("/v1/analyze", json=PAGE, headers=bearer(developer["key"]))

    assert analyzed.status_code == 200
    assert analyzed.json()["charged"] is True and not analyzed.json().get("free_check")  # bought, not free
    assert await balance(pool, developer["account"]) == PRICE
    assert await free.left(pool, developer["key_id"], api=True) == 20

    await pool.execute("UPDATE api_keys SET free_checks = 0 WHERE id = $1", developer["key_id"])
    checked = await client.post(
        "/v1/check", json={**CHECK, "url": "https://shop.test/2"}, headers=bearer(developer["key"])
    )
    assert checked.status_code == 200
    assert checked.json()["charged"] is True and checked.json()["free_check"] is False
    assert await balance(pool, developer["account"]) == 0


async def test_credits_say_a_developer_pass_free_checks_are_for_the_api(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    developer, shopper = await new_pass(client, developer=True), await new_pass(client, free=True)
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", developer["account"], PRICE)

    dev = (await client.get("/v1/credits", headers=bearer(developer["key"]))).json()
    shop = (await client.get("/v1/credits", headers=bearer(shopper["key"]))).json()

    assert (dev["checks_remaining"], dev["free_checks_remaining"], dev["api_free_checks_remaining"]) == (1, 0, 20)
    assert (shop["checks_remaining"], shop["free_checks_remaining"], shop["api_free_checks_remaining"]) == (0, 0, 0)


async def test_a_developer_pass_free_checks_dont_move(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    developer, shopper = await new_pass(client, developer=True), await new_pass(client, free=True)

    out = await client.post(
        "/v1/keys/move-free-checks", json={"to_key_id": shopper["key_id"]}, headers=bearer(developer["key"])
    )
    into = await client.post(
        "/v1/keys/move-free-checks", json={"to_key_id": developer["key_id"]}, headers=bearer(shopper["key"])
    )

    assert out.json() == into.json() == {"moved": 0}
    assert await free.left(pool, developer["key_id"], api=True) == 20


async def test_a_recovered_developer_pass_stays_one(client: httpx.AsyncClient) -> None:
    developer = await new_pass(client, developer=True)
    recovered = await client.post("/v1/keys/recover", json={"recovery_code": developer["recovery_code"]})
    assert recovered.json()["developer"] is True and recovered.json()["free_checks"] == 20


# ── Developer passes have their own day's budget and a harder proof of work ──────────────────────────────────


def budget(monkeypatch: pytest.MonkeyPatch, developers: int) -> None:
    """Set the developers' day's budget to exactly that many free deep checks."""
    monkeypatch.setattr(settings, "free_check_cost_eur", Decimal("0.01"))
    monkeypatch.setattr(settings, "developer_free_daily_budget_eur", Decimal(developers) / 100)


async def test_the_developers_budget_used_up_answers_like_an_empty_pass(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(monkeypatch, developers=1)
    developer = await new_pass(client, developer=True)
    assert (await client.post("/v1/check", json=CHECK, headers=bearer(developer["key"]))).status_code == 200
    assert await free.used_today(pool) == 1

    refused = await client.post(
        "/v1/check", json={**CHECK, "url": "https://shop.test/2"}, headers=bearer(developer["key"])
    )

    assert refused.status_code == 402 and refused.json()["error_code"] == "INSUFFICIENT_CREDITS"
    assert refused.json()["detail"] == (
        "Today's free developer checks are used up; they're back at 00:00 UTC. Bought checks still work."
    )
    assert (
        refused.json()["metadata"]["analysis_usd_micros"] == PRICE and refused.json()["metadata"]["retry_after_s"] > 0
    )
    assert len(graph.calls) == 1  # refused before any work
    assert await free.left(pool, developer["key_id"], api=True) == 19  # nothing taken
    credits = (await client.get("/v1/credits", headers=bearer(developer["key"]))).json()
    assert credits["api_free_checks_remaining"] == 19 and credits["free_checks_paused"] is True
    # Bought checks still work on the developer pass.
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", developer["account"], PRICE)
    paid = await client.post(
        "/v1/check", json={**CHECK, "url": "https://shop.test/4"}, headers=bearer(developer["key"])
    )
    assert paid.status_code == 200 and paid.json()["free_check"] is False and paid.json()["charged"] is True


async def test_a_given_back_free_check_goes_back_to_the_budget(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(monkeypatch, developers=10)
    issued = await new_pass(client, developer=True)
    graph.error = RuntimeError("boom")

    failed = await client.post("/v1/check", json=CHECK, headers=bearer(issued["key"]))

    assert failed.status_code == 500
    assert await free.used_today(pool) == 0
    assert await free.left(pool, issued["key_id"], api=True) == 20


async def test_a_developer_pass_needs_the_harder_challenge(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    normal = (await client.get("/v1/keys/challenge")).json()
    harder = (await client.get("/v1/keys/challenge", params={"developer": "true"})).json()
    assert normal["bits"] == settings.key_pow_bits and normal["developer"] is False
    assert harder["bits"] == settings.key_pow_bits + settings.developer_pow_extra_bits and harder["developer"] is True

    solved = {"challenge": normal["challenge"], "solution": solve(normal["challenge"], normal["bits"])}
    refused = await client.post("/v1/keys", json={**solved, "developer": True})

    assert refused.status_code == 400 and refused.json()["error_code"] == "BAD_CHALLENGE"
    assert "GET /v1/keys/challenge?developer=true" in refused.json()["detail"]
    assert await pool.fetchval("SELECT COUNT(*) FROM api_keys") == 0
    # The ordinary challenge still makes an ordinary pass (it wasn't used up by the refusal) ...
    assert (await client.post("/v1/keys", json={**solved, "free": True})).status_code == 201
    # ... and a developer challenge makes a developer pass.
    made = await client.post(
        "/v1/keys",
        json={
            "challenge": harder["challenge"],
            "solution": solve(harder["challenge"], harder["bits"]),
            "developer": True,
        },
    )
    assert made.status_code == 201 and made.json()["developer"] is True and made.json()["free_checks"] == 20
