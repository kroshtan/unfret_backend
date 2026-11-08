from typing import Any

import pytest
from pydantic import SecretStr
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import FakeRpc, configured, payment_header, payment_instructions, sign_payment
from veridex.config import Settings
from veridex.x402.payment import verify_payment
from veridex.x402.service import X402, DuplicateSettlementError
from veridex.x402.solana import associated_token_address
from veridex.x402.wallet import SYSTEM_PROGRAM, check_sweep

HOT = Keypair()
COLD = Keypair().pubkey()
CLIENT = Keypair()


def service(rpc: FakeRpc | None = None, **overrides: Any) -> X402:
    return X402(configured(HOT, COLD, **overrides), rpc=rpc or FakeRpc())


async def test_off_without_configuration() -> None:
    assert (await X402(Settings(), rpc=FakeRpc()).health()).status == "off"
    assert (await X402(Settings(cold_address=str(COLD)), rpc=FakeRpc()).health()).status == "off"


async def test_mainnet_refused_unless_allowed() -> None:
    assert (await service(solana_network="solana-mainnet").health()).status == "mainnet_refused"
    assert (await service(solana_network="solana-mainnet", x402_allow_mainnet=True).health()).status == "ok"


async def test_invalid_key_or_cold_address_is_misconfigured() -> None:
    bad_cold = Settings(hot_wallet_key=SecretStr(str(HOT)), cold_address="nope")
    bad_key = Settings(hot_wallet_key=SecretStr("nope"), cold_address=str(COLD))
    assert (await X402(bad_cold, rpc=FakeRpc()).health()).status == "misconfigured"
    assert (await X402(bad_key, rpc=FakeRpc()).health()).status == "misconfigured"


async def test_hot_key_equal_to_cold_address_is_misconfigured() -> None:
    same = Settings(hot_wallet_key=SecretStr(str(HOT)), cold_address=str(HOT.pubkey()))
    assert (await X402(same, rpc=FakeRpc()).health()).status == "misconfigured"


async def test_payto_and_fee_payer_are_the_hot_wallet() -> None:
    requirements = service().requirements("deep")
    assert requirements["payTo"] == requirements["extra"]["feePayer"] == str(HOT.pubkey())
    assert requirements["amount"] == "1500000"
    assert requirements["extra"]["paymentFlow"] == "upfront"


async def test_low_fee_reserve_pauses_payments_and_alerts() -> None:
    health = await service(FakeRpc(sol=1_999_999)).health()
    assert (health.status, health.sol_lamports) == ("low_balance", 1_999_999)
    assert "fee_reserve_low" in health.alerts


async def test_healthy_reports_balances() -> None:
    health = await service(FakeRpc(sol=20_000_000, wsol=300_000)).health()
    assert (health.status, health.sol_lamports, health.wsol_lamports, health.alerts) == ("ok", 20_000_000, 300_000, ())


async def test_value_stuck_above_threshold_alerts_sweep_failing() -> None:
    health = await service(FakeRpc(sol=20_000_000, wsol=140_000_001)).health()
    assert "sweep_failing" in health.alerts
    assert health.status == "ok"  # payments continue; the money is still safe in the hot wallet


async def test_rpc_failure_pauses_payments() -> None:
    class Down(FakeRpc):
        async def get_balance(self, address: Pubkey) -> int:
            raise ConnectionError

    assert (await service(Down()).health()).status == "rpc_unavailable"


async def test_settles_once_and_rejects_duplicates() -> None:
    rpc = FakeRpc()
    x402 = service(rpc)
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [CLIENT])
    payment = verify_payment(
        payment_header(tx, x402.requirements("quick")),
        x402.requirements("quick"),
        pay_to=HOT.pubkey(),
        fee_payer=HOT.pubkey(),
        max_compute_units=400_000,
        max_compute_unit_price=50_000,
    )

    assert await x402.settle(payment)
    assert len(rpc.simulated) == len(rpc.sent) == 1
    with pytest.raises(DuplicateSettlementError):
        await x402.settle(payment)


def sent_sweep(rpc: FakeRpc) -> VersionedTransaction:
    (raw,) = rpc.sent
    tx = VersionedTransaction.from_bytes(raw)
    check_sweep(tx.message, HOT.pubkey(), COLD)
    return tx


def to_cold(tx: VersionedTransaction) -> int:
    keys = tx.message.account_keys
    for ix in tx.message.instructions:
        if keys[ix.program_id_index] == SYSTEM_PROGRAM:
            assert [keys[i] for i in bytes(ix.accounts)] == [HOT.pubkey(), COLD]
            return int.from_bytes(bytes(ix.data)[4:12], "little")
    return 0


async def test_sweeps_everything_above_the_reserve_to_cold() -> None:
    rpc = FakeRpc(sol=12_000_000, wsol=150_000_000)
    assert await service(rpc).sweep_once()
    # SOL + wSOL - sweep fee buffer - reserve; the wSOL account is closed and recreated in the same transaction.
    assert to_cold(sent_sweep(rpc)) == 12_000_000 + 150_000_000 - 10_000 - 10_000_000


async def test_no_sweep_below_threshold() -> None:
    rpc = FakeRpc(sol=12_000_000, wsol=50_000_000)
    assert await service(rpc).sweep_once() is None
    assert rpc.sent == []


async def test_missing_wsol_account_is_created_without_sending_anything() -> None:
    rpc = FakeRpc(sol=12_000_000, wsol=None)
    assert await service(rpc).sweep_once()
    tx = sent_sweep(rpc)
    assert to_cold(tx) == 0
    assert associated_token_address(HOT.pubkey()) in tx.message.account_keys


async def test_low_fee_reserve_is_refilled_by_unwrapping() -> None:
    rpc = FakeRpc(sol=3_000_000, wsol=20_000_000)
    assert await service(rpc).sweep_once()
    assert to_cold(sent_sweep(rpc)) == 0


async def test_outflow_to_non_cold_alerts_and_pauses_payments() -> None:
    rpc = FakeRpc()
    rpc.signatures = ["sig1"]
    info = {"source": str(HOT.pubkey()), "destination": str(Keypair().pubkey())}
    rpc.transactions["sig1"] = {
        "transaction": {
            "message": {"instructions": [{"program": "system", "parsed": {"type": "transfer", "info": info}}]}
        },
        "meta": {"innerInstructions": []},
    }
    x402 = service(rpc)

    assert await x402.audit_outflows()
    health = await x402.health()

    assert (health.status, "outflow_to_non_cold" in health.alerts) == ("compromise_suspected", True)
    assert await x402.audit_outflows() == []  # each transaction is checked once
