"""Free checks for shoppers: quick and deep with no pass, the proof of work, and the free pool."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.conftest import DROP_ALL, FakeGraph
from tests.integration.test_keys import PAGE, PRICE, balance, bearer, solve
from unfret import spend
from unfret.config import settings
from unfret.credits import keys, ledger
from unfret.main import app
from unfret.migrations import MIGRATIONS, migrate
from unfret.routes import analyze


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(settings, "deep_pow_bits", 4)
    monkeypatch.setattr(settings, "public_base_url", "https://unfret.test")
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(analyze, "_free_deep_rate", analyze.RateLimit(1000, window_s=3600))
    for pending in (spend._pending, spend._pending_paid, spend._pending_checks):
        pending.take()  # nothing metered by another test


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    graph = FakeGraph()
    graph.result = {"score": 60, "explanation": "A quick look.", "skill_results": []}
    monkeypatch.setattr(app, "quick_graph", graph)
    return graph


async def solved(client: httpx.AsyncClient) -> dict[str, str]:
    """A solved challenge, as a free deep check sends it."""
    challenge = (await client.get("/v1/analyze/challenge")).json()
    return {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}


async def free_deep(client: httpx.AsyncClient, body: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
    return await client.post("/v1/analyze", json={**(body or PAGE), "proof_of_work": await solved(client)}, **kwargs)


async def at_ceiling(pool: asyncpg.Pool) -> None:
    """The free pool is empty: free deep checks pause until 00:00 UTC."""
    await pool.execute(
        "INSERT INTO free_pool (id, base_micro_eur, donated_micro_eur, credited_day) VALUES (1, 0, 0, $1) "
        "ON CONFLICT (id) DO UPDATE SET base_micro_eur = 0, donated_micro_eur = 0, credited_day = $1",
        spend.today(),
    )


async def a_pass(client: httpx.AsyncClient) -> dict[str, Any]:
    challenge = (await client.get("/v1/keys/challenge")).json()
    body = {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], challenge["bits"])}
    response = await client.post("/v1/keys", json={**body, "free": True})
    assert response.status_code == 201
    issued: dict[str, Any] = response.json()
    return issued


async def test_a_deep_check_is_free_without_a_pass_with_a_solved_challenge(
    client: httpx.AsyncClient, graph: FakeGraph
) -> None:
    response = await free_deep(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "deep" and body["charged"] is False and body["free_check"] is True
    assert body["charge_note"] == "Deep checks are free."
    assert "report_token" not in body and "balance_usd_micros" not in body  # tied to no one
    assert body["share_token"]  # anyone holding the result can still share it
    assert len(graph.calls) == 1


async def test_a_free_deep_check_needs_a_solved_unused_challenge(client: httpx.AsyncClient, graph: FakeGraph) -> None:
    missing = await client.post("/v1/analyze", json=PAGE)
    assert (missing.status_code, missing.json()["error_code"]) == (428, "PROOF_OF_WORK_REQUIRED")
    assert missing.json()["metadata"]["challenge_url"] == "/v1/analyze/challenge"

    challenge = (await client.get("/v1/analyze/challenge")).json()
    assert challenge["bits"] == 4 and challenge["paused"] is False and "pause" not in challenge
    unsolved = {"challenge": challenge["challenge"], "solution": "x"}
    if not keys.pow_ok(challenge["challenge"], "x", 4):
        bad = await client.post("/v1/analyze", json={**PAGE, "proof_of_work": unsolved})
        assert (bad.status_code, bad.json()["error_code"]) == (400, "BAD_CHALLENGE")
    good = {"challenge": challenge["challenge"], "solution": solve(challenge["challenge"], 4)}
    forged = {**good, "challenge": challenge["challenge"].replace(".4.", ".1.")}
    assert (await client.post("/v1/analyze", json={**PAGE, "proof_of_work": forged})).status_code == 400
    assert (await client.post("/v1/analyze", json={**PAGE, "proof_of_work": good})).status_code == 200
    again = await client.post("/v1/analyze", json={**PAGE, "proof_of_work": good})
    assert (again.status_code, again.json()["error_code"]) == (400, "BAD_CHALLENGE")  # used once
    assert len(graph.calls) == 1


async def test_a_harder_setting_refuses_easier_challenges(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    easy = await solved(client)
    monkeypatch.setattr(settings, "deep_pow_bits", 6)
    refused = await client.post("/v1/analyze", json={**PAGE, "proof_of_work": easy})
    assert refused.status_code == 400


async def test_a_pass_without_bought_checks_is_not_used_for_a_free_check(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    issued = await a_pass(client)
    assert issued["free_checks"] == 0  # "free": true gives nothing now: no pass is needed

    response = await free_deep(client, headers=bearer(issued["key"]))

    assert response.status_code == 200
    body = response.json()
    assert body["free_check"] is True and body["charged"] is False and "report_token" not in body
    assert await balance(pool, issued["account"]) is None  # the pass was never touched


async def test_an_older_store_client_checks_free_with_its_pass_and_no_work(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The extension and the Android app in the stores send their pass and solve no challenge.
    monkeypatch.setattr(analyze, "_legacy_pass_rate", analyze.RateLimit(2, window_s=3600))
    issued = await a_pass(client)
    credits = (await client.get("/v1/credits", headers=bearer(issued["key"]))).json()
    assert (credits["checks_remaining"], credits["free_checks_remaining"], credits["free_checks_paused"]) == (
        2,
        2,
        False,
    )

    first = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))
    second = await client.post(
        "/v1/analyze/stream", json={**PAGE, "url": "https://shop.test/2"}, headers=bearer(issued["key"])
    )
    third = await client.post(
        "/v1/analyze", json={**PAGE, "url": "https://shop.test/3"}, headers=bearer(issued["key"])
    )

    assert first.status_code == 200 and first.json()["free_check"] is True and "report_token" not in first.json()
    assert second.status_code == 200 and '"free_check": true' in second.text
    assert (third.status_code, third.json()["error_code"]) == (429, "TOO_MANY_REQUESTS")  # a few an hour per pass
    assert (await client.get("/v1/credits", headers=bearer(issued["key"]))).json()["checks_remaining"] == 0
    assert await balance(pool, issued["account"]) is None
    # At the ceiling it says so, as the older clients understand: paused free checks, quick checks only.
    monkeypatch.setattr(analyze, "_legacy_pass_rate", analyze.RateLimit(2, window_s=3600))
    await at_ceiling(pool)
    paused = (await client.get("/v1/credits", headers=bearer(issued["key"]))).json()
    assert paused["free_checks_paused"] is True and paused["free_checks_remaining"] == paused["checks_remaining"]
    refused = await client.post(
        "/v1/analyze", json={**PAGE, "url": "https://shop.test/4"}, headers=bearer(issued["key"])
    )
    assert refused.json()["error_code"] == "FREE_CHECKS_PAUSED"


async def test_a_client_that_may_not_link_to_donations_gets_none(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await at_ceiling(pool)
    off = {"X-Unfret-Donate": "off"}

    paused = await client.post("/v1/analyze", json={**PAGE, "proof_of_work": await solved(client)}, headers=off)
    challenge = (await client.get("/v1/analyze/challenge", headers=off)).json()
    pricing = (await client.get("/v1/pricing", headers=off)).json()["free"]

    meta = paused.json()["metadata"]
    assert meta["message_key"] == "free_checks_paused" and "donate_url" not in meta
    assert "donation" not in paused.json()["detail"]
    assert challenge["pause"]["message_key"] == "free_checks_paused" and "donate_url" not in challenge["pause"]
    assert "donate_url" not in pricing and "donate_url" not in pricing["pause"]
    cost = await analyze.deep_cost(_request(pool, off), None, None)
    assert cost[1] is None and "donation" not in cost[0]


async def test_bought_checks_pay_without_any_work(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await a_pass(client)
    await ledger.credit(pool, issued["account"], PRICE)

    response = await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))

    assert response.status_code == 200
    body = response.json()
    assert body["charged"] is True and "free_check" not in body and body["report_token"]
    assert await balance(pool, issued["account"]) == 0


async def test_the_spend_ceiling_pauses_free_deep_checks_only(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, quick: FakeGraph
) -> None:
    pow_ = await solved(client)
    await at_ceiling(pool)

    paused = await client.post("/v1/analyze", json={**PAGE, "proof_of_work": pow_})

    assert (paused.status_code, paused.json()["error_code"]) == (429, "FREE_CHECKS_PAUSED")
    meta = paused.json()["metadata"]
    assert meta["reason"] == "spend_ceiling" and meta["quick_checks"] is True
    assert meta["message_key"] == "free_checks_run_on_donations"
    assert meta["donate_url"] == "https://unfret.test/donate"
    assert 0 < meta["retry_after_s"] <= 24 * 3600 and meta["scope"] == "day"
    assert "donations" in paused.json()["detail"]
    challenge = (await client.get("/v1/analyze/challenge")).json()
    assert challenge["paused"] is True and challenge["pause"] == {
        **meta,
        "retry_after_s": challenge["pause"]["retry_after_s"],
    }
    pricing = (await client.get("/v1/pricing")).json()["free"]
    assert pricing["paused"] is True and pricing["pause"]["message_key"] == "free_checks_run_on_donations"
    # Quick checks and bought checks go on.
    assert (await client.post("/v1/analyze", json={**PAGE, "tier": "quick"})).status_code == 200
    issued = await a_pass(client)
    await ledger.credit(pool, issued["account"], PRICE)
    assert (await client.post("/v1/analyze", json=PAGE, headers=bearer(issued["key"]))).status_code == 200
    assert len(graph.calls) == 1  # the free one never ran


async def test_the_spend_counts_from_the_meter_and_draws_from_the_pool(pool: asyncpg.Pool) -> None:
    spend.record("mistral-small-3.2-24b-instruct-2506", 1_000_000, 1_000_000)  # €0.15 + €0.35
    spend.record("some-new-model", 1_000_000, 0)  # not listed: the high price, €2.00
    assert await spend.spent_free_micro_eur(pool) == 2_500_000
    before = await spend.standing(pool)
    assert before.pool_micro_eur == 3_000_000 - 2_500_000  # the first day's base, what is in memory counted
    await spend.flush(pool)
    row = await pool.fetchrow("SELECT * FROM llm_spend_days WHERE day = $1", spend.today())
    assert (row["free_micro_eur"], row["paid_micro_eur"]) == (2_500_000, 0)
    assert await spend.spent_free_micro_eur(pool) == 2_500_000  # nothing counted twice
    after = await spend.standing(pool)
    assert (after.pool_micro_eur, after.spent_today_micro_eur) == (500_000, 2_500_000) and not after.reached


async def test_the_meter_prices_a_models_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_prices_eur_per_mtok", "m=1.00/2.00")
    assert spend.cost_micro_eur("m", 1000, 500) == 2000  # €0.001 + €0.001
    assert spend.cost_micro_eur("m", 1, 0) == 1  # rounded up: never free
    assert (await spend.standing(None)).reached  # without a database nothing can be metered: paused


async def test_free_deep_checks_per_network_are_limited_an_hour(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyze, "_free_deep_rate", analyze.RateLimit(2, window_s=3600))
    assert (await free_deep(client)).status_code == 200
    assert (await free_deep(client, {**PAGE, "url": "https://shop.test/other"})).status_code == 200
    third = await free_deep(client, {**PAGE, "url": "https://shop.test/third"})
    assert (third.status_code, third.json()["error_code"]) == (429, "TOO_MANY_REQUESTS")


async def test_a_cached_result_is_free_for_a_shopper_but_still_needs_the_work(
    client: httpx.AsyncClient, graph: FakeGraph
) -> None:
    assert (await free_deep(client)).status_code == 200
    assert (await client.post("/v1/analyze", json=PAGE)).status_code == 428  # same gate as a fresh check
    again = await free_deep(client)
    assert again.status_code == 200 and again.json()["free_check"] is True
    assert len(graph.calls) == 1  # served from the cache


async def test_the_stream_runs_a_free_deep_check(client: httpx.AsyncClient) -> None:
    body = {**PAGE, "proof_of_work": await solved(client)}
    streamed = await client.post("/v1/analyze/stream", json=body)
    assert streamed.status_code == 200
    result = json.loads(streamed.text.split("event: result\ndata: ", 1)[1].split("\n", 1)[0])
    assert result["free_check"] is True and result["charged"] is False
    needs_work = await client.post("/v1/analyze/stream", json={**PAGE, "url": "https://shop.test/x"})
    assert needs_work.status_code == 428


async def test_a_quick_check_is_free_for_everyone(client: httpx.AsyncClient, quick: FakeGraph) -> None:
    response = await client.post("/v1/analyze", json={**PAGE, "tier": "quick"})
    assert response.status_code == 200
    assert response.json()["tier"] == "quick" and response.json()["charge_note"] == "Quick checks are free."


async def test_a_quick_result_suggests_a_free_deep_check_or_says_why_not(
    client: httpx.AsyncClient, pool: asyncpg.Pool, quick: FakeGraph
) -> None:
    quick.result = {**quick.result, "verdict": None}
    offer = await analyze.deep_cost(_request(pool), None, None)
    assert offer == ("Deep checks are free.", "run_deep")
    await at_ceiling(pool)
    paused = await analyze.deep_cost(_request(pool), None, None)
    assert paused[1] == "donate" and "donations" in paused[0]


def _request(pool: asyncpg.Pool, headers: dict[str, str] | None = None) -> Any:
    class _App:
        db_pool = pool

    class _Request:
        app = _App()

    request = _Request()
    request.headers = headers or {}  # type: ignore[attr-defined]
    return request


async def test_nothing_about_the_network_or_the_check_is_stored(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await free_deep(client)
    tables = ["api_keys", "usage_counters", "wallets", "llm_spend_days", "free_pool"]
    dump = json.dumps([[dict(r) for r in await pool.fetch(f"SELECT * FROM {t}")] for t in tables], default=str)  # noqa: S608
    assert "127.0.0.1" not in dump and "testclient" not in dump and "shop.test" not in dump
    assert await pool.fetchval("SELECT count(*) FROM api_keys") == 0  # no pass was made


async def test_pricing_describes_the_free_tier(client: httpx.AsyncClient) -> None:
    free_doc = (await client.get("/v1/pricing")).json()["free"]
    assert free_doc["quick_checks"] is True and free_doc["deep_checks"] is True and free_doc["paused"] is False
    assert free_doc["proof_of_work"] == {"challenge_url": "/v1/analyze/challenge", "bits": 4}
    assert free_doc["donate_url"] == "https://unfret.test/donate"
    assert free_doc["developer_checks_per_pass"] == 20 and free_doc["checks_per_pass"] == 0


async def test_shoppers_free_checks_leave_their_passes(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 43])
    for key_id, developer in (("shopper234", False), ("develop234", True)):
        await pool.execute(
            "INSERT INTO api_keys (id, key_hash, recovery_hash, free_checks, developer) VALUES ($1, $2, $3, 5, $4)",
            key_id,
            key_id.encode().ljust(32, b"\x00"),
            key_id.encode().ljust(32, b"\x01"),
            developer,
        )
    assert 43 in await migrate(pool)
    left = {r["id"]: r["free_checks"] for r in await pool.fetch("SELECT id, free_checks FROM api_keys")}
    assert left == {"shopper234": 0, "develop234": 5}


async def test_donate_redirects_to_the_configured_link_and_counts_only(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = await client.get("/donate")
    assert (closed.status_code, closed.headers["location"]) == (302, "/support")
    assert "Donations aren't open yet" in (await client.get("/support")).text

    monkeypatch.setattr(settings, "donate_url", "https://pay.example/link")
    response = await client.get("/donate")

    assert (response.status_code, response.headers["location"]) == (302, "https://pay.example/link")
    assert response.headers["cache-control"] == "no-store"
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM usage_counters WHERE name = 'donate_clicks'")]
    assert rows == [{"name": "donate_clicks", "period": spend.today().strftime("%Y-%m"), "count": 1}]
    page = (await client.get("/support")).text
    assert 'href="/donate"' in page and "pay.example" not in page  # the page links to /donate, never the link
    assert "Nothing in return" in page


# ── The free pool ────────────────────────────────────────────────────────────────────────────────────────────────


async def test_each_day_adds_its_base_up_to_thirty_days_worth(pool: asyncpg.Pool) -> None:
    day1 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    await pool.execute(
        "INSERT INTO free_pool (id, base_micro_eur, donated_micro_eur, credited_day) VALUES (1, 3000000, 0, $1)",
        day1.date(),
    )
    assert (await spend.standing(pool, now=day1 + timedelta(days=4))).pool_micro_eur == 15_000_000  # 5 days: €15
    # The base part stops growing at 30 days' worth (€90), however long nothing is spent.
    assert (await spend.standing(pool, now=day1 + timedelta(days=100))).pool_micro_eur == 90_000_000


async def test_donations_go_on_top_of_the_cap_but_a_day_spends_three_times_the_base(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "INSERT INTO free_pool (id, base_micro_eur, donated_micro_eur, credited_day) VALUES (1, 90000000, 0, $1)",
        spend.today(),
    )
    donation = await spend.add_donation(pool, Decimal(50), "Revolut, by hand")

    now = await spend.standing(pool)
    assert now.pool_micro_eur == 140_000_000 and now.day_limit_micro_eur == 9_000_000
    assert now.left_micro_eur == 9_000_000  # the day's limit, not the pool
    ledger = [dict(r) for r in await pool.fetch("SELECT day, micro_eur, note FROM free_pool_donations")]
    assert donation > 0 and ledger == [{"day": spend.today(), "micro_eur": 50_000_000, "note": "Revolut, by hand"}]
    # Spending €9 in a day pauses free checks until 00:00 UTC, with €131 still in the pool.
    spend.record("some-new-model", 4_500_000, 0)  # €9
    await spend.flush(pool)
    paused = await spend.standing(pool)
    assert paused.reached and paused.scope == "day" and 0 < paused.retry_after_s() <= 24 * 3600
    assert paused.pool_micro_eur == 131_000_000
    # The base part was drawn first; the donation is still whole.
    assert await pool.fetchval("SELECT donated_micro_eur FROM free_pool") == 50_000_000


async def test_several_donations_are_one_ledger_entry_each_and_spendable_today(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await pool.execute(
        "INSERT INTO free_pool (id, base_micro_eur, donated_micro_eur, credited_day) VALUES (1, 0, 0, $1)",
        spend.today(),
    )
    assert (await spend.standing(pool)).reached  # an empty pool: paused
    monkeypatch.setattr(settings, "admin_token", SecretStr("admin-secret"))
    admin = {"Authorization": "Bearer admin-secret"}
    body = {"amounts_eur": ["5", "10", "2.50"], "note": "Revolut, 25 Nov"}
    response = await client.post("/v1/admin/free-pool/donations", json=body, headers=admin)
    assert response.status_code == 200
    assert len(response.json()["ids"]) == 3 and response.json()["id"] == response.json()["ids"][0]
    rows = await pool.fetch("SELECT day, micro_eur, note FROM free_pool_donations ORDER BY id")
    assert [(r["day"], r["micro_eur"], r["note"]) for r in rows] == [
        (spend.today(), 5_000_000, "Revolut, 25 Nov"),
        (spend.today(), 10_000_000, "Revolut, 25 Nov"),
        (spend.today(), 2_500_000, "Revolut, 25 Nov"),
    ]
    assert await spend.donation_count(pool) == 3  # what /live shows as supporters
    # In the pool at once: free checks run again today, up to the day's limit (three times the base), never more.
    now = await spend.standing(pool)
    assert not now.reached and now.pool_micro_eur == 17_500_000 and now.left_micro_eur == 9_000_000
    one = await client.post("/v1/admin/free-pool/donations", json={"amount_eur": "1"}, headers=admin)
    assert one.status_code == 200 and await spend.donation_count(pool) == 4
    bad_bodies: list[dict[str, Any]] = [{}, {"amounts_eur": []}, {"amounts_eur": ["0"]}, {"amount_eur": "-1"}]
    for bad in bad_bodies:
        assert (await client.post("/v1/admin/free-pool/donations", json=bad, headers=admin)).status_code in {400, 422}
    assert await spend.donation_count(pool) == 4


async def test_an_empty_pool_pauses_until_the_next_day_adds_its_base(pool: asyncpg.Pool) -> None:
    today = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
    await pool.execute(
        "INSERT INTO free_pool (id, base_micro_eur, donated_micro_eur, credited_day) VALUES (1, 0, 0, $1)",
        today.date(),
    )
    assert (await spend.standing(pool, now=today)).reached
    tomorrow = await spend.standing(pool, now=today + timedelta(hours=7))
    assert not tomorrow.reached and tomorrow.pool_micro_eur == 3_000_000


async def test_the_pool_in_public_is_a_number_of_checks(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    free_doc = (await client.get("/v1/pricing")).json()["free"]
    # €3 today at the public €0.05 a check: 60, never euros, never the measured average.
    assert free_doc["deep_checks_left_today"] == 60
    assert not any("eur" in key for key in free_doc)
    await pool.execute(
        "INSERT INTO llm_spend_days (day, free_micro_eur, free_checks) VALUES ($1, 1000000, 100)",
        spend.today() - timedelta(days=1),
    )
    assert await spend.average_free_check_micro_eur(pool) == 10_000  # measured: €1 over 100 checks
    await at_ceiling(pool)
    assert (await client.get("/v1/pricing")).json()["free"]["deep_checks_left_today"] == 0


async def test_the_operator_adds_a_donation(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr  # noqa: PLC0415

    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    admin = {"Authorization": "Bearer operator-token"}
    added = await client.post(
        "/v1/admin/free-pool/donations", json={"amount_eur": "25", "note": "Revolut"}, headers=admin
    )
    assert added.status_code == 200, added.text
    assert added.json()["donated_eur"] == "25.00" and added.json()["donations"][0]["note"] == "Revolut"
    shown = (await client.get("/v1/admin/free-pool", headers=admin)).json()
    assert shown["pool_eur"] == "28.00"  # today's base and the donation
    assert (await client.get("/v1/admin/free-pool")).status_code in {401, 403}


async def test_paid_spend_never_counts_towards_the_free_budget(pool: asyncpg.Pool) -> None:
    spend.record("mistral-small-3.2-24b-instruct-2506", 1_000_000, 0, paid=True)  # €0.15, a bought check's
    spend.record("mistral-small-3.2-24b-instruct-2506", 1_000_000, 0)  # €0.15, free
    assert await spend.spent_free_micro_eur(pool) == 150_000
    await spend.flush(pool)
    row = await pool.fetchrow("SELECT * FROM llm_spend_days WHERE day = $1", spend.today())
    assert (row["free_micro_eur"], row["paid_micro_eur"]) == (150_000, 150_000)


async def test_a_bought_checks_llm_calls_count_as_paid(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    import contextvars  # noqa: PLC0415

    def in_task(paid: bool) -> None:
        spend.paying(paid)
        spend.record("m", 1_000_000, 0)

    contextvars.copy_context().run(in_task, True)
    contextvars.copy_context().run(in_task, False)
    await spend.flush(pool)
    row = await pool.fetchrow("SELECT * FROM llm_spend_days WHERE day = $1", spend.today())
    assert row["free_micro_eur"] == row["paid_micro_eur"] == 2_000_000  # the unlisted price, €2 a million


async def test_the_pause_says_until_midnight(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await at_ceiling(pool)
    paused = await free_deep(client)
    meta = paused.json()["metadata"]
    assert meta["scope"] == "day" and 0 < meta["retry_after_s"] <= 24 * 3600
    assert "until 00:00 UTC" in paused.json()["detail"]
    off = await client.post(
        "/v1/analyze", json={**PAGE, "proof_of_work": await solved(client)}, headers={"X-Unfret-Donate": "off"}
    )
    assert off.json()["metadata"]["scope"] == "day" and "donate_url" not in off.json()["metadata"]
    assert (await client.get("/v1/pricing")).json()["free"]["back_at"] == "00:00 UTC"
