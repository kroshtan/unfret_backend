"""The outflow audit (M1, L7): pages back to the last audited transaction, skips failures, the operator clears it."""

from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from tests.unit.x402_helpers import FakeRpc, configured, make_x402
from unfret.config import settings
from unfret.main import app
from unfret.x402.service import X402

HOT = Keypair()
COLD = Keypair().pubkey()
THIEF = str(Keypair().pubkey())


def transfer(destination: str) -> dict[str, Any]:
    info = {"source": str(HOT.pubkey()), "destination": destination, "lamports": 1}
    return {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": str(HOT.pubkey()), "signer": True}],
                "instructions": [{"program": "system", "parsed": {"type": "transfer", "info": info}}],
            }
        },
        "meta": {"err": None, "innerInstructions": []},
    }


class CountingRpc(FakeRpc):
    def __init__(self) -> None:
        super().__init__()
        self.fetched: list[str] = []

    async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
        self.fetched.append(signature)
        return self.transactions.get(signature)


def service(rpc: FakeRpc) -> X402:
    return make_x402(configured(HOT, COLD), rpc)


async def test_a_burst_of_transactions_cant_hide_an_outflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.x402.service._AUDIT_PAGE", 3)
    rpc = CountingRpc()
    rpc.signatures = ["s0"]
    rpc.transactions["s0"] = transfer(str(COLD))
    x402 = service(rpc)
    assert await x402.audit_outflows() == []  # first run: the newest page, then a cursor

    # Then: the theft, buried under seven newer transactions (more than two pages).
    rpc.signatures = [f"n{i}" for i in range(7, 0, -1)] + ["theft", "s0"]
    for signature in rpc.signatures[:-1]:
        rpc.transactions[signature] = transfer(str(COLD))
    rpc.transactions["theft"] = transfer(THIEF)
    assert await x402.audit_outflows() == [f"system transfer to {THIEF}"]
    assert rpc.fetched.count("s0") == 1  # nothing before the cursor is fetched again
    assert (await x402.health()).status == "compromise_suspected"


async def test_failed_transactions_are_skipped_without_fetching_them() -> None:
    rpc = CountingRpc()
    rpc.signatures = ["fail", "ok"]
    rpc.failed = {"fail"}
    rpc.transactions["ok"] = transfer(str(COLD))
    assert await service(rpc).audit_outflows() == []
    assert rpc.fetched == ["ok"]


async def test_an_unindexed_transaction_holds_the_cursor_back() -> None:
    rpc = CountingRpc()
    rpc.signatures = ["new", "pending", "old"]
    rpc.transactions.update(new=transfer(str(COLD)), old=transfer(str(COLD)))
    x402 = service(rpc)
    await x402.audit_outflows()
    assert x402._audit_cursor == "old"  # "pending" isn't indexed yet: it is looked at again next time
    rpc.transactions["pending"] = transfer(THIEF)
    assert await x402.audit_outflows() == [f"system transfer to {THIEF}"]
    assert x402._audit_cursor == "new"
    assert rpc.fetched.count("new") == 1  # audited once, though the cursor stayed behind it


async def test_the_seen_set_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.x402.service._SEEN_MAX", 5)
    x402 = service(FakeRpc())
    for i in range(12):
        x402._mark_seen(f"s{i}")
    assert list(x402._seen) == [f"s{i}" for i in range(7, 12)]


async def test_the_operator_clears_a_suspected_compromise(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc = CountingRpc()
    rpc.signatures = ["theft"]
    rpc.transactions["theft"] = transfer(THIEF)
    x402 = service(rpc)
    await x402.audit_outflows()
    monkeypatch.setattr(app, "x402", x402)
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/admin/x402/clear-alerts")).status_code == 401
        wrong = {"Authorization": "Bearer nope"}
        assert (await client.post("/v1/admin/x402/clear-alerts", headers=wrong)).status_code == 401
        ok = {"Authorization": "Bearer operator-token"}
        response = await client.post("/v1/admin/x402/clear-alerts", headers=ok)
    assert response.status_code == 200
    assert response.json() == {"cleared": [f"system transfer to {THIEF}"], "x402": "ok"}


async def test_admin_endpoints_are_off_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr(""))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/admin/x402/clear-alerts", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


async def test_an_outflow_is_caught_before_it_finalizes() -> None:
    # The audit reads at "confirmed" commitment: a theft pauses payments seconds after it lands, not after finality.
    rpc = CountingRpc()
    x402 = service(rpc)
    assert await x402.audit_outflows() == []
    rpc.signatures = ["theft"]
    rpc.unfinalized = {"theft"}
    rpc.transactions["theft"] = transfer(THIEF)
    assert await x402.audit_outflows() == [f"system transfer to {THIEF}"]
