"""The /v1/check route end to end over ASGI, with real transactions and a stubbed RPC (no database needed)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from solders.keypair import Keypair

from tests.unit.x402_helpers import (
    FakeRpc,
    configured,
    cpi_payment_instructions,
    payment_header,
    payment_instructions,
    sign_payment,
)
from veridex.config import Settings
from veridex.main import app
from veridex.x402.payment import decode_header
from veridex.x402.service import X402
from veridex.x402.solana import RpcError, SettlementPendingError

HOT = Keypair()
CLIENT = Keypair()
COLD = Keypair().pubkey()
BODY = {"url": "https://shop.test/p/1", "page_content": "<h1>Earbuds</h1>", "tier": "quick", "policy": "balanced"}


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls.append(state)
        if self.error:
            raise self.error
        return {"score": 25, "explanation": "x", "skill_results": ["[page_content]\nUnknown brand."]}


@pytest.fixture
def rpc() -> FakeRpc:
    return FakeRpc()


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
async def client(rpc: FakeRpc, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(app, "x402", X402(configured(HOT, COLD), rpc=rpc))
    monkeypatch.setattr(app, "quick_graph", graph)
    monkeypatch.setattr(app, "graph", graph)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def paid_header(amount: int = 100_000) -> str:
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), amount), HOT.pubkey(), [CLIENT])
    x402: X402 = app.x402
    return payment_header(tx, x402.requirements("quick"))


async def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "x402", X402(Settings(), rpc=FakeRpc()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/v1/check", json=BODY)).status_code == 503


async def test_unpaid_request_gets_402_with_requirements(client: httpx.AsyncClient, graph: FakeGraph) -> None:
    response = await client.post("/v1/check", json=BODY)

    assert response.status_code == 402
    required = decode_header(response.headers["PAYMENT-REQUIRED"])
    assert required["x402Version"] == 2
    (accepts,) = required["accepts"]
    assert accepts["asset"] == "So11111111111111111111111111111111111111112"
    assert accepts["amount"] == "100000"
    assert accepts["payTo"] == str(HOT.pubkey())
    assert accepts["extra"]["feePayer"] == str(HOT.pubkey())
    assert graph.calls == []


async def test_paid_request_settles_then_returns_verdict(
    client: httpx.AsyncClient, rpc: FakeRpc, graph: FakeGraph
) -> None:
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})

    assert response.status_code == 200
    body = response.json()
    assert (body["action"], body["risk_score"], body["tier"], body["policy_applied"]) == (
        "block",
        0.75,
        "quick",
        "balanced",
    )
    settlement = decode_header(response.headers["PAYMENT-RESPONSE"])
    assert settlement["success"] is True
    assert settlement["payer"] == str(CLIENT.pubkey())
    assert len(rpc.sent) == 1
    assert graph.calls[0]["page_content"] == BODY["page_content"]


async def test_wrong_payment_is_refused_without_settling(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    rpc.sim_delta = 1  # what the chain would show for this underpayment
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header(amount=1)})

    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-REQUIRED"])["error"] == "wrong_amount"
    assert rpc.sent == []


async def test_smart_wallet_payment_is_accepted(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    ixs = cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    x402: X402 = app.x402
    header = payment_header(sign_payment(ixs, HOT.pubkey(), [CLIENT]), x402.requirements("quick"))

    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})

    assert response.status_code == 200
    assert decode_header(response.headers["PAYMENT-RESPONSE"])["success"] is True


async def test_payment_that_landed_differently_gets_no_analysis(
    client: httpx.AsyncClient, rpc: FakeRpc, graph: FakeGraph
) -> None:
    rpc.landed_delta = 0
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-RESPONSE"])["errorReason"] == "settlement_mismatch"
    assert graph.calls == []


async def test_malformed_payment_is_400(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": "%%%"})
    assert response.status_code == 400


async def test_replayed_payment_is_refused(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    header = paid_header()
    assert (await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})).status_code == 200

    replay = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})

    assert replay.status_code == 402
    assert decode_header(replay.headers["PAYMENT-REQUIRED"])["error"] == "duplicate_settlement"
    assert len(rpc.sent) == 1


async def test_unfetchable_page_costs_nothing(
    client: httpx.AsyncClient, rpc: FakeRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(url: str, **_: object) -> bytes:
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr("veridex.routes.check.fetch_public", blocked)
    body = {k: v for k, v in BODY.items() if k != "page_content"}

    response = await client.post("/v1/check", json=body, headers={"PAYMENT-SIGNATURE": paid_header()})

    assert response.status_code == 422
    assert rpc.sent == []


async def test_failed_settlement_is_reported(client: httpx.AsyncClient, rpc: FakeRpc, graph: FakeGraph) -> None:
    rpc.fail_with = RpcError("insufficient funds")
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-RESPONSE"])["errorReason"] == "settlement_failed"
    assert graph.calls == []


async def test_pending_settlement_returns_the_signature(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    rpc.fail_with = SettlementPendingError("5ig")
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    settlement = decode_header(response.headers["PAYMENT-RESPONSE"])
    assert (response.status_code, settlement["errorReason"], settlement["transaction"]) == (
        402,
        "settlement_pending",
        "5ig",
    )


async def test_analysis_failure_after_payment_says_so(client: httpx.AsyncClient, graph: FakeGraph) -> None:
    graph.error = RuntimeError("llm down")
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert response.status_code == 500
    assert "Payment settled" in response.json()["detail"]
    assert decode_header(response.headers["PAYMENT-RESPONSE"])["success"] is True


async def test_low_balance_refuses_payments(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    rpc.sol = 1
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert response.status_code == 503
    assert rpc.sent == []


async def test_discovery_and_llms_txt(client: httpx.AsyncClient) -> None:
    discovery = (await client.get("/.well-known/x402")).json()
    assert discovery["tiers"]["quick"]["price_lamports"] == 100_000
    assert discovery["tiers"]["deep"]["price_lamports"] == 1_500_000
    text = (await client.get("/llms.txt")).text
    assert "0.0001 SOL" in text
    assert "0.0015 SOL" in text
