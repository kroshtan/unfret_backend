import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.conftest import FakeGraph
from veridex import reports
from veridex.config import settings
from veridex.credits import keys

PRICE = settings.analysis_price_usd_micros
ADMIN = {"Authorization": "Bearer operator-token"}
Wallet = Callable[[int], Awaitable[tuple[str, dict[str, str]]]]


def page(asin: str = "B000000001", **extra: Any) -> dict[str, Any]:
    return {
        "url": f"https://www.amazon.nl/Some-Sweater/dp/{asin}/ref=sr_1_3?keywords=sweater&qid=123&session-id=999",
        "page_content": "Hello, Floris van Beers\nDeliver to Floris Amsterdam 1012 AB\nWool sweater, 85% off. "
        "Questions? mail jan.jansen@example.nl or call +31 6 1234 5678.",
        "page_facts": {"platform": "amazon", "title": "Wool sweater", "meta": {"csrf-token": "abc", "og:title": "x"}},
        **extra,
    }


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))


async def check(client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/v1/analyze", json=body, headers=headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


async def report(
    client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any], result: dict[str, Any], **extra: Any
) -> httpx.Response:
    payload = {
        "report_token": result["report_token"],
        "reason": "scam",
        "consent": True,
        "request": body,
        "explanation": result["explanation"],
        **extra,
    }
    return await client.post("/v1/reports", json=payload, headers=headers)


async def reporter(wallet: Wallet) -> dict[str, str]:
    _, headers = await wallet(10 * PRICE)
    return headers


async def test_a_report_enters_the_queue_scrubbed(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet
) -> None:
    headers, body = await reporter(wallet), page()
    result = await check(client, headers, body)

    response = await report(client, headers, body, result)

    assert response.status_code == 201
    assert response.json() == {"status": "queued", "reports_used": 1, "reports_per_month": 5}
    row = await pool.fetchrow("SELECT * FROM reports")
    assert row["page_key"] == "amazon.nl|asin:B000000001"
    assert row["score"] == 80
    stored = row["payload"]
    for secret in ("Floris", "Beers", "jan.jansen", "1234 5678", "1012 AB", "session-id", "qid", "csrf"):
        assert secret not in stored, secret
    data = json.loads(stored)
    assert data["url"] == "https://www.amazon.nl/Some-Sweater/dp/B000000001"
    assert "Wool sweater, 85% off" in data["text"]
    assert data["facts"]["meta"] == {"og:title": "x"}


async def test_consent_is_required(client: httpx.AsyncClient, wallet: Wallet) -> None:
    headers, body = await reporter(wallet), page()
    result = await check(client, headers, body)
    assert (await report(client, headers, body, result, consent=False)).status_code == 422


async def test_reports_must_match_the_check(client: httpx.AsyncClient, wallet: Wallet) -> None:
    headers, other, body = await reporter(wallet), await reporter(wallet), page()
    result = await check(client, headers, body)

    someone_elses = await report(client, other, body, result)
    altered = await report(client, headers, {**body, "page_content": "Something else entirely."}, result)
    rewritten = await report(client, headers, body, {**result, "explanation": "Obvious scam."})

    for response in (someone_elses, altered, rewritten):
        assert response.status_code == 400
        assert response.json()["error_code"] == "BAD_REPORT_TOKEN"


async def test_duplicates_are_closed_before_the_queue_and_corroborate(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet
) -> None:
    body = page()
    first, second, third = await reporter(wallet), await reporter(wallet), await reporter(wallet)
    assert (await report(client, first, body, await check(client, first, body))).json()["status"] == "queued"
    # The same product with another tracking query string is the same page.
    again = page(url="https://amazon.nl/dp/B000000001?tag=affiliate-21")
    second_result = await check(client, second, again)

    response = await report(client, second, again, second_result)
    await report(client, third, body, await check(client, third, body))

    assert response.json()["status"] == "already_reported"
    assert await pool.fetchval("SELECT COUNT(*) FROM reports") == 1
    assert await pool.fetchval("SELECT corroborations FROM reports") == 2
    twice = await report(client, first, body, await check(client, first, body))
    assert twice.status_code == 409
    assert twice.json()["error_code"] == "ALREADY_REPORTED_BY_YOU"


async def test_a_recently_labelled_page_is_already_known(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet
) -> None:
    headers, body = await reporter(wallet), page()
    await report(client, headers, body, await check(client, headers, body))
    report_id = await pool.fetchval("SELECT id FROM reports")
    await client.post(f"/v1/admin/reports/{report_id}/label", json={"label": "scam"}, headers=ADMIN)

    other = await reporter(wallet)
    response = await report(client, other, body, await check(client, other, body))

    assert response.json()["status"] == "already_known"
    assert await pool.fetchval("SELECT COUNT(*) FROM reports") == 0


async def test_monthly_limit(client: httpx.AsyncClient, wallet: Wallet, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "reports_per_month", 2)
    headers = await reporter(wallet)
    statuses = []
    for n in range(3):
        body = page(asin=f"B00000000{n}")
        statuses.append((await report(client, headers, body, await check(client, headers, body))).status_code)

    assert statuses == [201, 201, 429]


async def test_a_full_queue_says_so(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "report_queue_cap", 1)
    headers = await reporter(wallet)
    first, second = page(asin="B00000000A"), page(asin="B00000000B")
    await report(client, headers, first, await check(client, headers, first))

    response = await report(client, headers, second, await check(client, headers, second))

    assert response.status_code == 503
    assert response.json()["error_code"] == "QUEUE_FULL"
    # A page that is already queued is still confirmed when the queue is full.
    other = await reporter(wallet)
    assert (await report(client, other, first, await check(client, other, first))).status_code == 201


@pytest.mark.parametrize(
    ("score", "label", "reward", "training"),
    [
        (80, "scam", 3 * PRICE, True),  # 40 points above the scam zone (<= 40): the model was badly wrong
        (70, "junk", 3 * PRICE, True),  # exactly 30 off
        (60, "junk", 0, True),  # 20 off: training data, no reward
        (45, "scam", 0, False),  # 5 off: close enough
        (20, "legit", 3 * PRICE, True),  # 50 below the legit zone (>= 70)
        (80, "legit", 0, False),  # the model was right
    ],
)
async def test_labelling_rewards_the_reporter_and_unlinks(  # noqa: PLR0917 - fixtures and parameters
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    graph: FakeGraph,
    wallet: Wallet,
    score: int,
    label: str,
    reward: int,
    training: bool,
) -> None:
    graph.result = {**graph.result, "score": score}
    address, headers = await wallet(PRICE)
    body = page()
    await report(client, headers, body, await check(client, headers, body))
    queue = (await client.get("/v1/admin/reports", headers=ADMIN)).json()
    assert queue["open"] == 1
    assert "account" not in queue["items"][0]

    response = await client.post(
        f"/v1/admin/reports/{queue['items'][0]['id']}/label", json={"label": label}, headers=ADMIN
    )

    assert response.json()["reward_usd_micros"] == reward
    assert response.json()["training"] is training
    assert await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address) == reward
    assert await pool.fetchval("SELECT COUNT(*) FROM reports") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM report_votes") == 0
    sample = await pool.fetchrow("SELECT * FROM labelled_pages")
    assert sample["label"] == label
    assert sample["score"] == score
    assert address not in json.dumps(dict(sample), default=str)  # the training sample belongs to the page
    gone = await client.post(
        f"/v1/admin/reports/{queue['items'][0]['id']}/label", json={"label": label}, headers=ADMIN
    )
    assert gone.status_code == 404  # labelled once, rewarded once


async def test_a_rejected_report_keeps_nothing(client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet) -> None:
    headers, body = await reporter(wallet), page()
    await report(client, headers, body, await check(client, headers, body))
    report_id = await pool.fetchval("SELECT id FROM reports")

    response = await client.post(f"/v1/admin/reports/{report_id}/label", json={"label": "reject"}, headers=ADMIN)

    assert response.json()["reward_usd_micros"] == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM reports") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM labelled_pages") == 0


async def test_queue_order_and_admin_only(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, wallet: Wallet
) -> None:
    for asin, score in (("B00000000C", 95), ("B00000000D", 55), ("B00000000E", 90)):
        graph.result = {**graph.result, "score": score}
        headers, body = await reporter(wallet), page(asin=asin)
        await report(client, headers, body, await check(client, headers, body))
    graph.result = {**graph.result, "score": 90}
    late, body = await reporter(wallet), page(asin="B00000000E")
    await report(client, late, body, await check(client, late, body))  # corroborates E

    assert (await client.get("/v1/admin/reports")).status_code == 401
    items = (await client.get("/v1/admin/reports", headers=ADMIN)).json()["items"]
    assert [i["page_key"][-1] for i in items] == ["E", "D", "C"]  # corroborated, then least certain, then the rest


async def test_unhandled_reports_expire_and_unlink(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet
) -> None:
    headers, body = await reporter(wallet), page()
    await report(client, headers, body, await check(client, headers, body))

    assert await reports.expire(pool, settings, now=datetime.now(UTC) + timedelta(days=29)) == 0
    assert await reports.expire(pool, settings, now=datetime.now(UTC) + timedelta(days=31)) == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM reports") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM report_votes") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM report_quota") == 0  # a past month's counts go too


async def test_api_keys_can_report(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await keys.create(pool)
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", issued.account, PRICE)
    headers, body = {"Authorization": f"Bearer {issued.key}"}, page()

    response = await report(client, headers, body, await check(client, headers, body))

    assert response.json()["status"] == "queued"
    assert await pool.fetchval("SELECT account FROM reports") == issued.account


async def test_the_operator_grants_a_little_credit_to_an_existing_key_only(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    issued = await keys.create(pool)
    url = f"/v1/admin/keys/{issued.key_id}/grant"

    assert (await client.post(url, json={"usd_micros": 1_000_000})).status_code == 401
    response = await client.post(url, json={"usd_micros": 1_000_000}, headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {"key_id": issued.key_id, "balance_usd_micros": 1_000_000}
    assert await keys.balance(pool, issued.key_id) == 1_000_000

    too_much = await client.post(url, json={"usd_micros": 10_000_001}, headers=ADMIN)
    assert too_much.status_code == 422
    unknown = await client.post("/v1/admin/keys/aaaaaaaaaa/grant", json={"usd_micros": 1}, headers=ADMIN)
    assert unknown.status_code == 404
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets WHERE pubkey = 'key:aaaaaaaaaa'") == 0
