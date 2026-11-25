"""Free checks: a new pass's free deep checks, the day's budget for them, and quick checks for everyone."""

import json
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import DROP_ALL, FakeGraph
from tests.integration.test_keys import PAGE, PRICE, balance, bearer, solve
from unfret.config import settings
from unfret.credits import free
from unfret.main import app
from unfret.migrations import MIGRATIONS, migrate
from unfret.routes import analyze


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    graph = FakeGraph()
    graph.result = {"score": 60, "explanation": "A quick look.", "skill_results": []}
    monkeypatch.setattr(app, "quick_graph", graph)
    return graph


def budget(monkeypatch: pytest.MonkeyPatch, checks: int) -> None:
    """Set the day's budget to exactly ``checks`` free deep checks."""
    monkeypatch.setattr(settings, "free_check_cost_eur", Decimal("0.01"))
    monkeypatch.setattr(settings, "free_daily_budget_eur", Decimal(checks) / 100)


async def free_pass(client: httpx.AsyncClient, *, free_checks: bool = True) -> dict[str, Any]:
    challenge = (await client.get("/v1/keys/challenge")).json()
    body = {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}
    response = await client.post("/v1/keys", json={**body, "free": free_checks})
    assert response.status_code == 201
    issued: dict[str, Any] = response.json()
    return issued


async def test_a_free_pass_checks_five_pages_without_a_balance(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    issued = await free_pass(client)
    assert issued["free_checks"] == settings.free_checks_per_pass == 5
    assert issued["balance_usd_micros"] == 0

    credits = (await client.get("/v1/credits", headers=bearer(issued["key"]))).json()
    assert credits["checks_remaining"] == 5 and credits["free_checks_remaining"] == 5
    assert credits["free_checks_paused"] is False

    for n in range(5):
        page = {**PAGE, "url": f"https://shop.test/item-{n}"}
        checked = await client.post("/v1/analyze", json=page, headers=bearer(issued["key"]))
        assert checked.status_code == 200
        assert checked.json()["charged"] is True and checked.json()["free_check"] is True
        left = {3: "1 is left", 4: "none are left"}.get(n, f"{4 - n} are left")
        assert checked.json()["charge_note"] == f"This check used one of your free checks; {left}."
    assert await free.left(pool, issued["key_id"]) == 0
    assert await balance(pool, issued["account"]) is None  # never touched the ledger

    refused = await client.post(
        "/v1/analyze", json={**PAGE, "url": "https://shop.test/6"}, headers=bearer(issued["key"])
    )
    assert refused.status_code == 402
    assert refused.json()["error_code"] == "INSUFFICIENT_CREDITS"


async def test_free_checks_go_before_bought_ones(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await free_pass(client)
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", issued["account"], PRICE)

    checked = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))

    assert checked.json()["free_check"] is True
    assert checked.json()["balance_usd_micros"] == PRICE
    assert await free.left(pool, issued["key_id"]) == 4
    credits = (await client.get("/v1/credits", headers=bearer(issued["key"]))).json()
    assert credits["checks_remaining"] == 5  # one bought, four free


async def test_a_pass_without_free_checks_unless_asked(client: httpx.AsyncClient) -> None:
    issued = await free_pass(client, free_checks=False)
    assert issued["free_checks"] == 0
    old_client = await client.get("/v1/keys/challenge")  # an older client never sends "free": same as before
    body = {"challenge": old_client.json()["challenge"], "solution": solve(old_client.json()["challenge"], 4)}
    plain = await client.post("/v1/keys", json=body)
    assert plain.json()["free_checks"] == 0


async def test_one_network_gets_two_free_passes_a_day_then_empty_ones(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    passes = [await free_pass(client) for _ in range(3)]

    assert [p["free_checks"] for p in passes] == [5, 5, 0]
    assert await free.left(pool, passes[2]["key_id"]) == 0


async def test_the_days_budget_pauses_free_checks_but_not_bought_ones(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(monkeypatch, 1)
    first, second = await free_pass(client), await free_pass(client)
    assert (await client.post("/v1/analyze", json=PAGE, headers=bearer(first["key"]))).json()["free_check"] is True

    paused = await client.post(
        "/v1/analyze", json={**PAGE, "url": "https://shop.test/2"}, headers=bearer(second["key"])
    )

    assert paused.status_code == 429
    assert paused.json()["error_code"] == "FREE_CHECKS_PAUSED"
    assert paused.json()["detail"].startswith("Free deep checks are back tomorrow")
    assert paused.json()["metadata"]["quick_checks"] is True
    assert await free.left(pool, second["key_id"]) == 5  # nothing taken
    credits = (await client.get("/v1/credits", headers=bearer(second["key"]))).json()
    assert credits["free_checks_paused"] is True and credits["free_checks_remaining"] == 5
    pricing = (await client.get("/v1/pricing")).json()
    assert pricing["free"]["paused"] is True and pricing["free"]["checks_per_pass"] == 5

    # A bought check still works, and the free ones wait.
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", second["account"], PRICE)
    bought = await client.post(
        "/v1/analyze", json={**PAGE, "url": "https://shop.test/3"}, headers=bearer(second["key"])
    )
    assert bought.status_code == 200 and bought.json().get("free_check") is None
    assert await balance(pool, second["account"]) == 0
    assert await free.left(pool, second["key_id"]) == 5


async def test_a_zero_budget_turns_free_deep_checks_off(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(monkeypatch, 0)
    issued = await free_pass(client)
    response = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))
    assert response.status_code == 429


async def test_a_failed_free_check_is_given_back(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    issued = await free_pass(client)
    graph.error = RuntimeError("boom")

    failed = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))
    streamed = await client.post("/v1/analyze/stream", json=PAGE, headers=bearer(issued["key"]))

    assert failed.status_code == 500 and "event: error" in streamed.text
    assert await free.left(pool, issued["key_id"]) == 5
    assert await free.used_today(pool) == 0


async def test_a_page_that_couldnt_be_analysed_gives_the_free_check_back(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    issued = await free_pass(client)
    graph.result = {"score": -1, "explanation": "Not a product page.", "skill_results": []}

    response = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))

    assert response.json()["charged"] is False
    assert await free.left(pool, issued["key_id"]) == 5
    assert await free.used_today(pool) == 0


async def test_the_stream_says_it_was_a_free_check(client: httpx.AsyncClient) -> None:
    issued = await free_pass(client)
    streamed = await client.post("/v1/analyze/stream", json=PAGE, headers=bearer(issued["key"]))
    result = json.loads(streamed.text.strip().split("\n\n")[-1].split("\n")[1].removeprefix("data: "))
    assert result["free_check"] is True and result["tier"] == "deep"


async def test_free_checks_move_to_the_users_other_pass_and_never_become_balance(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    gift = await free_pass(client)
    bought = await free_pass(client, free_checks=False)
    await client.post("/v1/analyze", json=PAGE, headers=bearer(gift["key"]))

    moved = await client.post(
        "/v1/keys/move-free-checks", json={"to_key_id": bought["key_id"]}, headers=bearer(gift["key"])
    )

    assert moved.status_code == 200 and moved.json() == {"moved": 4}
    assert await free.left(pool, gift["key_id"]) == 0
    assert await free.left(pool, bought["key_id"]) == 4
    assert await balance(pool, bought["account"]) is None
    again = await client.post(
        "/v1/keys/move-free-checks", json={"to_key_id": bought["key_id"]}, headers=bearer(gift["key"])
    )
    assert again.json() == {"moved": 0}
    unknown = await client.post(
        "/v1/keys/move-free-checks", json={"to_key_id": "a" * 10}, headers=bearer(bought["key"])
    )
    assert unknown.json() == {"moved": 0}
    assert await free.left(pool, bought["key_id"]) == 4
    assert (await client.post("/v1/keys/move-free-checks", json={"to_key_id": "x"})).status_code == 401


async def test_a_quick_check_is_free_for_everyone_and_never_charged(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, quick: FakeGraph
) -> None:
    anonymous = await client.post("/v1/analyze", json={**PAGE, "tier": "quick"})

    assert anonymous.status_code == 200
    answer = anonymous.json()
    assert answer.pop("label_token")  # anyone can report it ("Wrong result?")
    assert answer == {
        "score": 60,
        "explanation": "A quick look.",
        "charged": False,
        "tier": "quick",
        "charge_note": "Quick checks are free.",
    }
    assert quick.calls and not graph.calls  # the rules-only graph, never the LLM one

    issued = await free_pass(client)
    with_pass = await client.post("/v1/analyze/stream", json={**PAGE, "tier": "quick"}, headers=bearer(issued["key"]))
    assert '"tier": "quick"' in with_pass.text and '"charged": false' in with_pass.text
    assert '"charge_note": "Quick checks are free."' in with_pass.text
    # A pass can report a quick result (the token binds the tier); a quick result is never shared.
    assert '"report_token"' in with_pass.text and "share_token" not in with_pass.text
    assert await free.left(pool, issued["key_id"]) == 5
    assert await free.used_today(pool) == 0


async def test_a_cached_result_uses_a_free_check(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph
) -> None:
    first, second = await free_pass(client), await free_pass(client)
    await client.post("/v1/analyze", json=PAGE, headers=bearer(first["key"]))

    cached = await client.post("/v1/analyze", json=PAGE, headers=bearer(second["key"]))
    repeat = await client.post("/v1/analyze", json=PAGE, headers=bearer(second["key"]))

    assert cached.json()["charged"] is True and cached.json()["free_check"] is True
    assert repeat.json()["charged"] is False and "free_check" not in repeat.json()
    assert cached.json()["charge_note"] == "This check used one of your free checks; 4 are left."
    assert repeat.json()["charge_note"] == "You checked this page recently, so no new check was used."
    assert len(graph.calls) == 1
    assert await free.left(pool, second["key_id"]) == 4
    assert await free.used_today(pool) == 2


async def test_a_cached_result_isnt_served_while_free_checks_are_paused(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(monkeypatch, 1)
    first, second = await free_pass(client), await free_pass(client)
    await client.post("/v1/analyze", json=PAGE, headers=bearer(first["key"]))

    refused = await client.post("/v1/analyze", json=PAGE, headers=bearer(second["key"]))
    repeat = await client.post("/v1/analyze", json=PAGE, headers=bearer(first["key"]))

    assert refused.status_code == 429 and refused.json()["error_code"] == "FREE_CHECKS_PAUSED"
    assert repeat.status_code == 200 and repeat.json()["charged"] is False  # the pass that paid gets it again
    assert await free.left(pool, second["key_id"]) == 5


async def test_a_cached_quick_check_is_still_free(
    client: httpx.AsyncClient, pool: asyncpg.Pool, quick: FakeGraph
) -> None:
    first, second = await free_pass(client), await free_pass(client)
    body = {**PAGE, "tier": "quick"}

    responses = [
        await client.post("/v1/analyze", json=body, headers=bearer(first["key"])),
        await client.post("/v1/analyze", json=body, headers=bearer(second["key"])),
        await client.post("/v1/analyze", json=body),
    ]

    assert [r.json()["charged"] for r in responses] == [False, False, False]
    assert len(quick.calls) == 1  # the others came from the cache
    assert await free.left(pool, second["key_id"]) == 5
    assert await free.used_today(pool) == 0


async def test_a_deep_check_still_needs_a_sign_in(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/analyze", json=PAGE)
    assert response.status_code == 401
    expired = await client.post(
        "/v1/analyze", json={**PAGE, "tier": "quick"}, headers=bearer(f"ufk_{'a' * 10}_{'b' * 52}")
    )
    assert expired.status_code == 401  # a pass that doesn't work is refused, not ignored


async def test_quick_checks_are_rate_limited(
    client: httpx.AsyncClient, quick: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(2))
    statuses = [
        (await client.post("/v1/analyze", json={**PAGE, "url": f"https://s.test/{n}", "tier": "quick"})).status_code
        for n in range(3)
    ]
    assert statuses == [200, 200, 429]


async def test_nothing_about_the_network_is_stored(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await free_pass(client)
    await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))
    tables = ["api_keys", "usage_counters", "wallets"]
    dump = json.dumps([[dict(r) for r in await pool.fetch(f"SELECT * FROM {t}")] for t in tables], default=str)  # noqa: S608
    assert "127.0.0.1" not in dump and "testclient" not in dump
    assert await pool.fetchval("SELECT count FROM usage_counters WHERE name = 'free_checks'") == 1


async def test_passes_from_before_the_developer_flag_keep_their_free_checks(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 34])
    old = await pool.fetchval(
        "INSERT INTO api_keys (id, key_hash, recovery_hash, free_checks) VALUES ('oldpass234', $1, $2, 20) "
        "RETURNING id",
        b"\x00" * 32,
        b"\x01" * 32,
    )
    assert 34 in await migrate(pool)
    assert await pool.fetchval("SELECT developer FROM api_keys WHERE id = $1", old) is False
    assert await free.left(pool, old) == 20  # counted everywhere, as before
