"""/v1/check with Base next to Solana: the offer, the authorization flow, retries, and Solana clients unchanged."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from tests.unit.test_x402_route import BODY, FakeGraph, paid_header
from tests.unit.test_x402_route import COLD as SOL_COLD
from tests.unit.test_x402_route import HOT as SOL_HOT
from tests.unit.x402_base_helpers import (
    NETWORK,
    PAY_TO,
    PAYER,
    FakeBaseRpc,
    FakeFacilitator,
    base_header,
    base_payload,
    base_settings,
    make_base,
    settled_receipt,
)
from tests.unit.x402_helpers import FakeRpc, configured, make_x402
from unfret.config import Settings
from unfret.main import app
from unfret.x402.base import BaseX402
from unfret.x402.payment import decode_header, encode_header
from unfret.x402.solana import USDC_MINTS

TX = "0x" + "12" * 32


@pytest.fixture
def rpc() -> FakeBaseRpc:
    return FakeBaseRpc()


@pytest.fixture
def facilitator() -> FakeFacilitator:
    return FakeFacilitator()


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def base(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> BaseX402:
    return make_base(base_settings(), rpc, facilitator)


async def _client(monkeypatch: pytest.MonkeyPatch, solana: Any, base: BaseX402, graph: FakeGraph) -> httpx.AsyncClient:
    monkeypatch.setattr(app, "x402", solana)
    monkeypatch.setattr(app, "x402_base", base)
    monkeypatch.setattr(app, "quick_graph", graph)
    monkeypatch.setattr(app, "graph", graph)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def both(monkeypatch: pytest.MonkeyPatch, base: BaseX402, graph: FakeGraph) -> AsyncIterator[httpx.AsyncClient]:
    """Solana (test_x402_route's hot wallet) and Base, both on."""
    async with await _client(monkeypatch, make_x402(configured(SOL_HOT, SOL_COLD), FakeRpc()), base, graph) as c:
        yield c


@pytest.fixture
async def base_only(
    monkeypatch: pytest.MonkeyPatch, base: BaseX402, graph: FakeGraph
) -> AsyncIterator[httpx.AsyncClient]:
    """Solana off (no hot wallet), Base on."""
    async with await _client(monkeypatch, make_x402(Settings(), FakeRpc()), base, graph) as c:
        yield c


async def offered(client: httpx.AsyncClient, body: dict[str, Any] = BODY) -> list[dict[str, Any]]:
    response = await client.post("/v1/check", json=body)
    assert response.status_code == 402
    accepts: list[dict[str, Any]] = decode_header(response.headers["PAYMENT-REQUIRED"])["accepts"]
    return accepts


def landed(rpc: FakeBaseRpc, header: str) -> None:
    """Make Base show the settlement of this payment (the facilitator stub reports TX)."""
    auth = decode_header(header)["payload"]["authorization"]
    rpc.receipts[TX] = settled_receipt(auth["from"], PAY_TO.address, int(auth["value"]), auth["nonce"].lower())


# --- the offer -----------------------------------------------------------------------------------------------------


async def test_base_is_offered_after_solanas_options(both: httpx.AsyncClient) -> None:
    wsol, usdc, base = await offered(both)
    assert wsol["asset"] == "So11111111111111111111111111111111111111112"
    assert usdc["asset"] == str(USDC_MINTS["solana-devnet"])
    assert (base["network"], base["amount"], base["payTo"]) == (NETWORK, "10000", PAY_TO.address)
    assert base["amount"] == usdc["amount"]  # the same dollar price, exactly, in USDC on both networks


async def test_solana_only_clients_see_no_change_while_base_is_off(
    monkeypatch: pytest.MonkeyPatch, graph: FakeGraph
) -> None:
    solana = make_x402(configured(SOL_HOT, SOL_COLD), FakeRpc())
    async with await _client(monkeypatch, solana, BaseX402(Settings(), rpc=FakeBaseRpc()), graph) as c:
        response = await c.post("/v1/check", json=BODY)
        required = decode_header(response.headers["PAYMENT-REQUIRED"])
        assert [o["network"] for o in required["accepts"]] == ["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"] * 2
        assert set(required) == {"x402Version", "error", "error_code", "resource", "accepts"}
        paid = await c.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert paid.status_code == 200


async def test_base_alone_when_solana_is_off(base_only: httpx.AsyncClient) -> None:
    (option,) = await offered(base_only)
    assert option["network"] == NETWORK


async def test_nothing_on_offer_is_a_503(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> None:
    async with await _client(
        monkeypatch, make_x402(Settings(), FakeRpc()), BaseX402(Settings(), rpc=FakeBaseRpc()), graph
    ) as c:
        response = await c.post("/v1/check", json=BODY)
    assert (response.status_code, response.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")


async def test_a_paused_base_is_not_offered(both: httpx.AsyncClient, rpc: FakeBaseRpc, base: BaseX402) -> None:
    rpc.down = True
    base._balance = None
    assert [o["network"] for o in await offered(both)] == ["solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"] * 2


# --- paying on Base ------------------------------------------------------------------------------------------------


async def test_a_paid_check_on_base_is_verified_analysed_then_settled(
    base_only: httpx.AsyncClient, rpc: FakeBaseRpc, facilitator: FakeFacilitator, graph: FakeGraph
) -> None:
    (option,) = await offered(base_only)
    header = base_header(option)
    landed(rpc, header)
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})

    assert response.status_code == 200
    assert response.json()["action"] == "block"
    receipt = decode_header(response.headers["PAYMENT-RESPONSE"])
    assert receipt == {"success": True, "transaction": TX, "network": NETWORK, "payer": PAYER.address.lower()}
    assert [name for name, _, _ in facilitator.requests] == ["verify", "settle"]
    assert len(graph.calls) == 1
    sent = facilitator.calls("settle")[0]["paymentPayload"]
    assert sent["resource"]["url"].endswith("/v1/check")  # Unfret's resource, not what the client echoed


async def test_a_retry_with_the_same_authorization_is_answered_once(
    both: httpx.AsyncClient, rpc: FakeBaseRpc, facilitator: FakeFacilitator, graph: FakeGraph
) -> None:
    option = (await offered(both))[-1]
    header = base_header(option)
    landed(rpc, header)
    first = await both.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
    again = await both.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
    assert (again.status_code, again.json()) == (200, first.json())
    assert again.headers["PAYMENT-RESPONSE"] == first.headers["PAYMENT-RESPONSE"]
    assert len(facilitator.calls("settle")) == 1
    assert len(graph.calls) == 1


async def test_the_same_authorization_with_another_signature_is_still_one_payment(
    base_only: httpx.AsyncClient, rpc: FakeBaseRpc, facilitator: FakeFacilitator
) -> None:
    (option,) = await offered(base_only)
    payload = base_payload(option)
    landed(rpc, encode_header(payload))
    await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    payload["payload"]["signature"] = "0x" + "ee" * 65  # same payer and nonce
    replay = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": encode_header(payload)})
    assert replay.status_code == 200
    assert len(facilitator.calls("settle")) == 1


async def test_a_refused_verification_is_a_402_and_nothing_runs(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator, graph: FakeGraph
) -> None:
    (option,) = await offered(base_only)
    facilitator.verify = {"isValid": False, "invalidReason": "invalid_exact_evm_insufficient_balance"}
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-REQUIRED"])["error"] == "invalid_exact_evm_insufficient_balance"
    assert graph.calls == []
    assert facilitator.calls("settle") == []


async def test_a_local_refusal_never_reaches_the_facilitator(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator
) -> None:
    (option,) = await offered(base_only)
    response = await base_only.post(
        "/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option, value=1)}
    )
    assert response.status_code == 402
    assert facilitator.requests == []


async def test_the_facilitator_down_is_a_503(base_only: httpx.AsyncClient, facilitator: FakeFacilitator) -> None:
    (option,) = await offered(base_only)
    facilitator.verify = httpx.ConnectError("down")
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert (response.status_code, response.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")


@pytest.mark.parametrize(
    "case", [(RuntimeError("boom"), 500, "INTERNAL_ERROR"), (TimeoutError(), 504, "GATEWAY_TIMEOUT")]
)
async def test_a_failed_analysis_is_never_charged(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator, graph: FakeGraph, case: tuple[Exception, int, str]
) -> None:
    error, status, code = case
    (option,) = await offered(base_only)
    graph.error = error
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert (response.status_code, response.json()["error_code"]) == (status, code)
    assert "not charged" in response.json()["detail"]
    assert facilitator.calls("settle") == []


async def test_a_page_that_cant_be_assessed_is_not_settled(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator, graph: FakeGraph
) -> None:
    (option,) = await offered(base_only)
    graph.score = -1
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert response.status_code == 200
    assert response.json()["charged"] is False
    assert "PAYMENT-RESPONSE" not in response.headers
    assert facilitator.calls("settle") == []


async def test_a_refused_settlement_withholds_the_answer(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator, base: BaseX402
) -> None:
    (option,) = await offered(base_only)
    facilitator.settle = {"success": False, "errorReason": "invalid_exact_evm_insufficient_balance", "transaction": ""}
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert (response.status_code, response.json()["error_code"]) == (402, "SETTLEMENT_FAILED")
    assert "action" not in response.json()
    receipt = decode_header(response.headers["PAYMENT-RESPONSE"])
    assert (receipt["success"], receipt["errorReason"]) == (False, "invalid_exact_evm_insufficient_balance")
    assert base.breaker.global_failures == 1


async def test_a_pending_settlement_still_gets_its_answer(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator, base: BaseX402
) -> None:
    (option,) = await offered(base_only)
    facilitator.settle = {"success": False, "errorReason": "settlement_pending", "transaction": TX}
    header = base_header(option)
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
    assert response.status_code == 200
    assert response.json()["action"] == "block"
    receipt = decode_header(response.headers["PAYMENT-RESPONSE"])
    assert (receipt["success"], receipt["errorReason"], receipt["transaction"]) == (False, "settlement_pending", TX)
    again = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
    assert again.json() == response.json() and len(facilitator.calls("settle")) == 1
    assert base.breaker.global_failures == 1


async def test_a_settlement_base_doesnt_show_is_a_mismatch(
    base_only: httpx.AsyncClient, rpc: FakeBaseRpc, base: BaseX402, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("unfret.x402.base._RECEIPT_RETRIES", 1)
    (option,) = await offered(base_only)
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert (response.status_code, response.json()["error_code"]) == (402, "SETTLEMENT_MISMATCH")
    assert "post_settlement_mismatch" in (await base.health()).alerts


async def test_the_breaker_pauses_a_payer(
    base_only: httpx.AsyncClient, base: BaseX402, facilitator: FakeFacilitator
) -> None:
    (option,) = await offered(base_only)
    for _ in range(3):
        base.breaker.record_failure(PAYER.address.lower(), "203.0.113.9")
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert (response.status_code, response.json()["error_code"]) == (503, "SETTLEMENTS_PAUSED")
    assert facilitator.requests == []


async def test_a_base_payment_while_base_is_off_is_refused(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> None:
    option = make_base(base_settings(), FakeBaseRpc(), FakeFacilitator()).requirements("quick")
    solana = make_x402(configured(SOL_HOT, SOL_COLD), FakeRpc())
    async with await _client(monkeypatch, solana, BaseX402(Settings(), rpc=FakeBaseRpc()), graph) as c:
        response = await c.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-REQUIRED"])["error"] == "requirements_mismatch"
    assert graph.calls == []


async def test_a_solana_payment_while_only_base_is_on_is_refused(base_only: httpx.AsyncClient) -> None:
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": paid_header()})
    assert response.status_code == 402
    assert decode_header(response.headers["PAYMENT-REQUIRED"])["error"] == "requirements_mismatch"


async def test_an_unreachable_facilitator_at_settlement_is_a_503_not_a_free_answer(
    base_only: httpx.AsyncClient, facilitator: FakeFacilitator, base: BaseX402
) -> None:
    (option,) = await offered(base_only)
    facilitator.settle = httpx.ConnectError("refused")
    response = await base_only.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": base_header(option)})
    assert (response.status_code, response.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")
    assert "action" not in response.json()
    assert base.breaker.global_failures == 0  # Unfret's outage, not the payer's doing
