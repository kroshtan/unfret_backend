"""A paid /v1/check whose analysis fails, times out or finds nothing to assess is refunded as credits, in the ledger."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
from solders.keypair import Keypair
from structlog.testing import capture_logs

from tests.unit.x402_helpers import (
    FakeRpc,
    configured,
    expected,
    make_x402,
    payment_header,
    payment_instructions,
    sign_payment,
)
from unfret.config import settings
from unfret.credits import ledger
from unfret.main import app
from unfret.x402.payment import decode_header

HOT = Keypair()
COLD = Keypair().pubkey()
BODY = {"url": "https://shop.test/p/1", "page_content": "<h1>Earbuds</h1>", "tier": "quick", "policy": "balanced"}
QUICK_USD_MICROS = 10_000  # $0.01, the quick tier's price


class Graph:
    """The quick tier's graph: answers ``score``, raises ``error`` or takes ``delay_s`` to answer."""

    def __init__(self) -> None:
        self.score = 25
        self.error: Exception | None = None
        self.delay_s = 0.0
        self.calls = 0

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(self.delay_s)
        if self.error:
            raise self.error
        return {"score": self.score, "explanation": "x", "skill_results": []}


@pytest.fixture
def graph() -> Graph:
    return Graph()


@pytest.fixture
async def client(
    pool: asyncpg.Pool, graph: Graph, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(app, "x402", make_x402(configured(HOT, COLD), FakeRpc()))
    monkeypatch.setattr(app, "quick_graph", graph)
    monkeypatch.setattr(app, "db_pool", pool)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def pay(client: httpx.AsyncClient, payer: Keypair) -> httpx.Response:
    tx = sign_payment(payment_instructions(payer.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [payer])
    header = payment_header(tx, expected(HOT.pubkey()))
    return await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})


async def assert_refunded(response: httpx.Response, pool: asyncpg.Pool, payer: Keypair) -> None:
    body = response.json()
    assert decode_header(response.headers["PAYMENT-RESPONSE"])["success"] is True  # it was paid
    assert body["refunded_usd_micros"] == QUICK_USD_MICROS
    assert str(payer.pubkey()) in body["refund"]
    assert await ledger.balance(pool, str(payer.pubkey())) == QUICK_USD_MICROS  # and the ledger holds it
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 1


async def test_a_failed_analysis_is_refunded(client: httpx.AsyncClient, graph: Graph, pool: asyncpg.Pool) -> None:
    graph.error = RuntimeError("llm down")
    payer = Keypair()

    response = await pay(client, payer)

    assert (response.status_code, response.json()["error_code"]) == (500, "INTERNAL_ERROR")
    await assert_refunded(response, pool, payer)


async def test_a_timed_out_analysis_is_refunded(
    client: httpx.AsyncClient, graph: Graph, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "x402_analysis_timeout_s", 0.05)
    graph.delay_s = 5
    payer = Keypair()

    response = await pay(client, payer)

    assert (response.status_code, response.json()["error_code"]) == (504, "GATEWAY_TIMEOUT")
    assert response.json()["detail"] == "Payment settled, but the analysis timed out."
    await assert_refunded(response, pool, payer)


async def test_a_page_that_cant_be_assessed_is_refunded(
    client: httpx.AsyncClient, graph: Graph, pool: asyncpg.Pool
) -> None:
    graph.score = -1  # e.g. not a product page
    payer = Keypair()

    response = await pay(client, payer)

    assert response.status_code == 200
    await assert_refunded(response, pool, payer)


async def test_a_refund_adds_to_an_existing_balance(
    client: httpx.AsyncClient, graph: Graph, pool: asyncpg.Pool
) -> None:
    payer = Keypair()
    await ledger.credit(pool, str(payer.pubkey()), 5)
    graph.score = -1

    response = await pay(client, payer)

    assert response.json()["refunded_usd_micros"] == QUICK_USD_MICROS
    assert await ledger.balance(pool, str(payer.pubkey())) == QUICK_USD_MICROS + 5


async def test_an_assessed_page_is_not_refunded(client: httpx.AsyncClient, graph: Graph, pool: asyncpg.Pool) -> None:
    response = await pay(client, Keypair())

    assert response.status_code == 200
    assert "refunded_usd_micros" not in response.json()
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 0


async def test_an_unrecordable_refund_is_not_claimed_and_the_answer_still_goes_out(
    client: httpx.AsyncClient, graph: Graph, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*_: Any) -> int:
        raise asyncpg.exceptions.ConnectionDoesNotExistError("gone")

    monkeypatch.setattr(ledger, "credit", down)
    graph.error = RuntimeError("llm down")

    with capture_logs() as logs:
        response = await pay(client, Keypair())

    assert (response.status_code, response.json()["error_code"]) == (500, "INTERNAL_ERROR")
    assert "refunded_usd_micros" not in response.json()  # never told it was refunded when it wasn't
    assert [e for e in logs if e["event"] == "x402_refund_unrecorded"] == [
        {
            "event": "x402_refund_unrecorded",
            "error": "ConnectionDoesNotExistError",
            "usd_micros": QUICK_USD_MICROS,
            "log_level": "error",
        }
    ]
