"""
Former cold addresses: after the cold address changes, the old sweeps to it are not a compromise, anything later is.

Built on two real devnet transactions of dev's hot wallet (tests/fixtures/x402), which dev's audit flagged after every
restart once its cold address had moved on from EE6X… (16 and 17 Nov 2026).
"""

import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from solders.keypair import Keypair

from tests.unit.x402_helpers import (
    DEV_FORMER_COLD,
    DEV_HOT,
    DEVNET_USDC,
    FakeRpc,
    configured,
    devnet_tx,
    make_x402,
)
from unfret.config import Settings, settings
from unfret.main import app
from unfret.x402.service import X402
from unfret.x402.solana import associated_token_address
from unfret.x402.wallet import unexpected_outflows

HOT = Keypair()
COLD = Keypair().pubkey()  # the cold address now
THIEF = str(Keypair().pubkey())
FORMER_USDC = str(associated_token_address(DEV_FORMER_COLD, DEVNET_USDC))
SOL_SWEEP, USDC_SWEEP = "sol_sweep_to_former_cold", "usdc_sweep_to_former_cold"
SOL_SIG = "51h53MNUycVkH2GrKDBJJPMiVEq5jmSnJazzgEKfTwrdAnGQAr9e2Dt5eey7QZrQGbs82turt2XJcYu1Edm7hrLx"
USDC_SIG = "3EPiHdDc21Y2wGsnGQuyQTaegZqXSBqV5uYexnqDfGn3xCRCS7EuLcQ21t6kSJdnRkkXYpuuqQFirXpaojr2iWiC"
CHANGED = "2026-11-16T06:00:00Z"  # after the last sweep to EE6X… (16 Nov 05:50:05 UTC)


def service(rpc: FakeRpc, **overrides: Any) -> X402:
    return make_x402(configured(HOT, COLD, **overrides), rpc)


def former(until: str = CHANGED) -> dict[str, Any]:
    return {"former_cold_addresses": [str(DEV_FORMER_COLD)], "former_cold_until": until}


def history(rpc: FakeRpc, **txs: dict[str, Any]) -> None:
    """The hot wallet's history, newest first, as getSignaturesForAddress lists it."""
    rpc.signatures = list(txs)
    rpc.transactions.update(txs)


def test_the_fixtures_are_dev_s_real_sweeps() -> None:
    sol, usdc = devnet_tx(SOL_SWEEP), devnet_tx(USDC_SWEEP)
    assert sol["transaction"]["signatures"] == [SOL_SIG] and usdc["transaction"]["signatures"] == [USDC_SIG]
    assert FORMER_USDC == "62rwX6v6hrvVB7Z1uNxQDudQj7N2vAeMvQ8qHPvayA3T"
    # What dev logged after its restarts: both sweeps look like theft once EE6X… is no longer the cold address.
    assert unexpected_outflows(sol, DEV_HOT, COLD, DEVNET_USDC) == [f"system transfer to {DEV_FORMER_COLD}"]
    assert unexpected_outflows(usdc, DEV_HOT, COLD, DEVNET_USDC) == [
        f"token transferChecked by the hot wallet to {FORMER_USDC}"
    ]


def test_a_former_cold_address_is_accepted_like_cold() -> None:
    for name in (SOL_SWEEP, USDC_SWEEP):
        assert unexpected_outflows(devnet_tx(name), DEV_HOT, COLD, DEVNET_USDC, [DEV_FORMER_COLD]) == []
    # The same transactions with the destination changed are still caught.
    stolen = devnet_tx(SOL_SWEEP, replace={str(DEV_FORMER_COLD): THIEF})
    assert unexpected_outflows(stolen, DEV_HOT, COLD, DEVNET_USDC, [DEV_FORMER_COLD]) == [
        f"system transfer to {THIEF}"
    ]


async def test_old_sweeps_to_a_former_cold_wallet_raise_no_alarm_after_a_restart() -> None:
    rpc = FakeRpc()
    history(rpc, **{SOL_SIG: devnet_tx(SOL_SWEEP, HOT.pubkey()), USDC_SIG: devnet_tx(USDC_SWEEP, HOT.pubkey())})
    x402 = service(rpc, **former())
    assert await x402.audit_outflows() == []
    assert (await x402.health()).status == "ok"


async def test_without_the_setting_the_old_sweeps_still_alarm() -> None:
    rpc = FakeRpc()
    history(rpc, **{SOL_SIG: devnet_tx(SOL_SWEEP, HOT.pubkey()), USDC_SIG: devnet_tx(USDC_SWEEP, HOT.pubkey())})
    x402 = service(rpc)
    assert sorted(await x402.audit_outflows()) == [
        f"system transfer to {DEV_FORMER_COLD}",
        f"token transferChecked by the hot wallet to {FORMER_USDC}",
    ]
    assert (await x402.health()).status == "compromise_suspected"


async def test_an_outflow_to_an_unknown_address_is_flagged() -> None:
    rpc = FakeRpc()
    stolen_sol = devnet_tx(SOL_SWEEP, HOT.pubkey(), {str(DEV_FORMER_COLD): THIEF})
    thief_usdc = str(associated_token_address(Keypair().pubkey(), DEVNET_USDC))
    stolen_usdc = devnet_tx(USDC_SWEEP, HOT.pubkey(), {FORMER_USDC: thief_usdc})
    history(rpc, theft_sol=stolen_sol, theft_usdc=stolen_usdc)
    x402 = service(rpc, **former())
    assert sorted(await x402.audit_outflows()) == [
        f"system transfer to {THIEF}",
        f"token transferChecked by the hot wallet to {thief_usdc}",
    ]
    assert (await x402.health()).status == "compromise_suspected"
    assert {(f.signature, f.reason) for f in x402.flagged()} == {
        ("theft_sol", f"system transfer to {THIEF}"),
        ("theft_usdc", f"token transferChecked by the hot wallet to {thief_usdc}"),
    }


async def test_a_sweep_to_a_former_cold_wallet_after_its_cut_off_is_flagged() -> None:
    rpc = FakeRpc()
    history(rpc, **{SOL_SIG: devnet_tx(SOL_SWEEP, HOT.pubkey())})
    x402 = service(rpc, **former(until="2026-11-15T15:55:00Z"))  # ten seconds before this sweep
    assert await x402.audit_outflows() == [
        f"system transfer to {DEV_FORMER_COLD} (a former cold address, after its cut-off)"
    ]


@pytest.mark.parametrize("block_time", ["after_start", None])
async def test_a_former_cold_outflow_after_the_process_started_or_undated_is_flagged(block_time: str | None) -> None:
    # Everything this process signs goes to the current cold address: a later outflow to a former one is not ours,
    # whatever the configured cut-off says.
    rpc = FakeRpc()
    tx = devnet_tx(USDC_SWEEP, HOT.pubkey())
    tx["blockTime"] = int(time.time()) + 60 if block_time else None
    history(rpc, drain=tx)
    x402 = service(rpc, **former(until="2099-01-01T00:00:00Z"))
    assert await x402.audit_outflows() == [
        f"token transferChecked by the hot wallet to {FORMER_USDC} (a former cold address, after its cut-off)"
    ]


def test_former_cold_addresses_come_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    other = str(Keypair().pubkey())
    monkeypatch.setenv("UNFRET_FORMER_COLD_ADDRESSES", f" {DEV_FORMER_COLD}, {other},,{DEV_FORMER_COLD}")
    monkeypatch.setenv("UNFRET_FORMER_COLD_UNTIL", CHANGED)
    s = Settings()
    assert s.former_cold_addresses == [str(DEV_FORMER_COLD), other]
    assert s.former_cold_until == datetime(2026, 11, 16, 6, tzinfo=UTC)
    monkeypatch.setenv("UNFRET_FORMER_COLD_ADDRESSES", "")
    monkeypatch.setenv("UNFRET_FORMER_COLD_UNTIL", "")
    assert (Settings().former_cold_addresses, Settings().former_cold_until) == ([], None)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"former_cold_addresses": "not-an-address"}, "is not a Solana address"),
        ({"former_cold_addresses": str(DEV_FORMER_COLD)}, "needs former_cold_until"),
        ({**former(), "former_cold_until": "2026-11-16T06:00:00"}, "needs a time zone"),
        ({**former(), "cold_address": str(DEV_FORMER_COLD)}, "can't also be a former cold address"),
    ],
)
def test_former_cold_settings_are_checked_at_start_up(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(**overrides)


async def test_the_hot_wallet_cant_be_a_former_cold_address() -> None:
    x402 = service(FakeRpc(), former_cold_addresses=[str(HOT.pubkey())], former_cold_until=CHANGED)
    assert not x402.configured
    assert (await x402.health()).status == "misconfigured"


async def test_the_operator_sees_each_flagged_transaction_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc = FakeRpc()
    history(rpc, theft=devnet_tx(SOL_SWEEP, HOT.pubkey(), {str(DEV_FORMER_COLD): THIEF}))
    x402 = service(rpc, **former())
    await x402.audit_outflows()
    monkeypatch.setattr(app, "x402", x402)
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/admin/x402/alerts")).status_code == 401
        response = await client.get("/v1/admin/x402/alerts", headers={"Authorization": "Bearer operator-token"})
    assert response.status_code == 200
    body = response.json()
    assert (body["x402"], body["alerts"]) == ("compromise_suspected", ["outflow_to_non_cold"])
    [flag] = body["flagged"]
    assert (flag["signature"], flag["reason"]) == ("theft", f"system transfer to {THIEF}")
    assert datetime.fromisoformat(flag["found_at"]).tzinfo is not None
