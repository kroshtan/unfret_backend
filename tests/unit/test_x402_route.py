"""The /v1/check route end to end over ASGI, with real transactions and a stubbed RPC (no database needed)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from solders.keypair import Keypair

from tests.unit.x402_helpers import (
    FakeRpc,
    configured,
    cpi_payment_instructions,
    expected,
    make_x402,
    payment_header,
    payment_instructions,
    sign_payment,
)
from unfret.config import Settings
from unfret.main import app
from unfret.routes.check import _errors_table
from unfret.visitors import RateLimit
from unfret.x402.payment import decode_header
from unfret.x402.solana import USDC_MINTS, RpcError, SettlementPendingError

HOT = Keypair()
CLIENT = Keypair()
COLD = Keypair().pubkey()
BODY = {"url": "https://shop.test/p/1", "page_content": "<h1>Earbuds</h1>", "tier": "quick", "policy": "balanced"}


class FakeGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.score = 25

    async def ainvoke(self, state: dict[str, Any], config: Any = None) -> dict[str, Any]:
        self.calls.append(state)
        if self.error:
            raise self.error
        return {"score": self.score, "explanation": "x", "skill_results": ["[page_content]\nUnknown brand."]}


@pytest.fixture
def rpc() -> FakeRpc:
    return FakeRpc()


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
async def client(rpc: FakeRpc, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(app, "x402", make_x402(configured(HOT, COLD), rpc))
    monkeypatch.setattr(app, "quick_graph", graph)
    monkeypatch.setattr(app, "graph", graph)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def paid_header(amount: int = 100_000) -> str:
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), amount), HOT.pubkey(), [CLIENT])
    return payment_header(tx, expected(HOT.pubkey()))


async def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "x402", make_x402(Settings(), FakeRpc()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.post("/v1/check", json=BODY)).status_code == 503


async def test_unpaid_request_gets_402_with_requirements(client: httpx.AsyncClient, graph: FakeGraph) -> None:
    response = await client.post("/v1/check", json=BODY)

    assert response.status_code == 402
    required = decode_header(response.headers["PAYMENT-REQUIRED"])
    assert required["x402Version"] == 2
    accepts, usdc = required["accepts"]  # wSOL first, then USDC once its account exists
    assert accepts["asset"] == "So11111111111111111111111111111111111111112"
    assert accepts["amount"] == "100000"  # $0.01 at $100/SOL
    assert accepts["payTo"] == str(HOT.pubkey())
    assert accepts["extra"]["feePayer"] == str(HOT.pubkey())
    assert (usdc["asset"], usdc["amount"]) == (str(USDC_MINTS["solana-devnet"]), "10000")  # exactly $0.01
    assert graph.calls == []
    assert "reason" not in required


async def test_402_options_state_decimals_tolerance_and_fee_cap_units(client: httpx.AsyncClient) -> None:
    wsol, usdc = decode_header((await client.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])["accepts"]
    assert (wsol["extra"]["decimals"], usdc["extra"]["decimals"]) == (9, 6)
    assert (wsol["extra"]["quoteToleranceBps"], usdc["extra"]["quoteToleranceBps"]) == (200, 0)
    # The fee cap is in lamports on both options: 5% of the $0.01 price at $100/SOL = 5% of 100_000 lamports.
    assert wsol["extra"]["constraints"]["maxPriorityFeeLamports"] == 5_000
    assert usdc["extra"]["constraints"]["maxPriorityFeeLamports"] == 5_000
    for option in (wsol, usdc):
        note = option["extra"]["feeCapNote"]
        assert "in lamports (SOL) for every option, USDC included" in note
        assert "5% of this tier's dollar price" in note


async def test_failed_simulation_says_why(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    rpc.sim_error = {"InstructionError": [1, {"Custom": 1}]}
    rpc.sim_logs = [
        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [1]",
        "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA failed: custom program error: 0x1",
    ]
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})

    assert response.status_code == 402
    body = response.json()
    assert (body["error"], body["reason"], body["error_code"]) == (
        "simulation_failed",
        "insufficient_funds",
        "PAYMENT_REQUIRED",
    )
    assert decode_header(response.headers["PAYMENT-REQUIRED"])["reason"] == "insufficient_funds"
    assert rpc.sent == []


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
    header = payment_header(sign_payment(ixs, HOT.pubkey(), [CLIENT]), expected(HOT.pubkey()))

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


async def test_a_retry_with_the_same_payment_gets_the_same_answer_once_paid(
    client: httpx.AsyncClient, rpc: FakeRpc, graph: FakeGraph
) -> None:
    header = paid_header()
    first = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
    assert first.status_code == 200

    replay = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})

    assert (replay.status_code, replay.json()) == (200, first.json())
    assert replay.headers["PAYMENT-RESPONSE"] == first.headers["PAYMENT-RESPONSE"]
    assert len(rpc.sent) == 1 and len(graph.calls) == 1  # charged and analysed once


async def test_concurrent_retries_share_one_settlement(
    client: httpx.AsyncClient, rpc: FakeRpc, graph: FakeGraph
) -> None:
    header = {"PAYMENT-SIGNATURE": paid_header()}
    responses = await asyncio.gather(*(client.post("/v1/check", json=BODY, headers=header) for _ in range(3)))
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(rpc.sent) == 1 and len(graph.calls) == 1


async def test_a_refusal_before_payment_can_be_retried(
    client: httpx.AsyncClient, rpc: FakeRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc.fail_with = RpcError("sendTransaction: ConnectError")  # never reached the network: nothing was charged
    header = {"PAYMENT-SIGNATURE": paid_header()}
    assert (await client.post("/v1/check", json=BODY, headers=header)).status_code == 402
    rpc.fail_with = None
    assert (await client.post("/v1/check", json=BODY, headers=header)).status_code == 200


async def test_an_unassessable_page_is_refunded(
    client: httpx.AsyncClient, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    credited: list[tuple[str, int]] = []

    async def credit(pool: object, payer: str, usd_micros: int) -> int:
        credited.append((payer, usd_micros))
        return usd_micros

    monkeypatch.setattr("unfret.routes.check._credit", credit)
    graph.score = -1
    response = await client.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert response.status_code == 200
    assert response.json()["refunded_usd_micros"] == 10_000
    assert credited == [(str(CLIENT.pubkey()), 10_000)]


async def test_a_bot_check_page_costs_nothing(client: httpx.AsyncClient, rpc: FakeRpc) -> None:
    body = {
        **BODY,
        "page_content": "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'>",
    }
    response = await client.post("/v1/check", json=body, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert (response.status_code, response.json()["error_code"]) == (422, "PAGE_BLOCKED")
    assert rpc.sent == []


async def test_check_is_rate_limited_per_visitor(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.routes.check._rate", RateLimit(2))
    codes = [(await client.post("/v1/check", json=BODY)).status_code for _ in range(3)]
    assert codes == [402, 402, 429]


async def test_unfetchable_page_costs_nothing(
    client: httpx.AsyncClient, rpc: FakeRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(url: str, **_: object) -> bytes:
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr("unfret.routes.check.fetch_public", blocked)
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
    offered = decode_header((await client.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])["accepts"]
    assert {o["network"] for o in offered} == {discovery["network"]} == {"solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"}
    assert discovery["network_name"] == "solana-devnet"
    assert (discovery["assets"]["wSOL"]["decimals"], discovery["assets"]["USDC"]["decimals"]) == (9, 6)
    assert discovery["assets"]["wSOL"]["quote_tolerance_bps"] == 200
    assert "lamports (SOL) for every option, USDC included" in discovery["feeCap"]
    assert discovery["pricing"] == "canonical in USD"
    assert discovery["tiers"]["quick"]["price_usd_micros"] == 10_000
    assert discovery["tiers"]["deep"]["price_usd"] == "$0.20"
    text = (await client.get("/llms.txt")).text
    assert "quick = $0.01" in text
    assert "deep = $0.20" in text
    assert "| `SETTLEMENT_PENDING` | 402 | maybe |" in text
    assert "network solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1, i.e. solana-devnet" in text
    assert "re-fetch the 402 right before paying" in text
    assert "in lamports (SOL) on every option, USDC included" in text
    assert "`reason`" in text


async def test_every_error_has_an_error_code(client: httpx.AsyncClient) -> None:
    invalid = await client.post("/v1/check", json={"tier": "quick"})  # no url
    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "VALIDATION_ERROR"
    assert invalid.json()["errors"][0]["loc"] == ["body", "url"]
    missing = await client.get("/v1/nothing-here")
    assert (missing.status_code, missing.json()["error_code"]) == (404, "NOT_FOUND")
    unpaid = await client.post("/v1/check", json=BODY)
    assert unpaid.json()["error_code"] == "PAYMENT_REQUIRED"


async def test_the_402_states_what_a_payment_must_look_like(client: httpx.AsyncClient) -> None:
    required = decode_header((await client.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])
    constraints = required["accepts"][0]["extra"]["constraints"]
    assert constraints["maxPriorityFeeLamports"] == 5_000  # 5% of $0.01 at $100/SOL
    assert constraints["addressLookupTables"] is False
    assert "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf" in constraints["programs"]
    assert "memo" in constraints


def test_the_readme_lists_every_error_code() -> None:
    readme = (Path(__file__).parents[2] / "README.md").read_text()
    assert _errors_table() in readme  # regenerate the README table from ERRORS when this fails
