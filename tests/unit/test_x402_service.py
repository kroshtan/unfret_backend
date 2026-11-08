from typing import Any

import pytest
from pydantic import SecretStr
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

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
from veridex.config import Settings
from veridex.x402.payment import PaymentError
from veridex.x402.service import X402, DuplicateSettlementError, SettlementMismatchError
from veridex.x402.solana import USDC_MINTS, TransactionFailedError, associated_token_address
from veridex.x402.wallet import SYSTEM_PROGRAM, check_sweep

HOT = Keypair()
COLD = Keypair().pubkey()
CLIENT = Keypair()


def service(rpc: FakeRpc | None = None, **overrides: Any) -> X402:
    return make_x402(configured(HOT, COLD, **overrides), rpc or FakeRpc())


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
    requirements = await service().requirements("deep")
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


def prechecked(x402: X402, instructions: list[Any] | None = None) -> Any:
    ixs = instructions or payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    tx = sign_payment(ixs, HOT.pubkey(), [CLIENT])
    accepts = [expected(HOT.pubkey())]
    return x402.precheck(payment_header(tx, accepts[0]), "quick", accepts)


async def test_settles_once_and_rejects_duplicates() -> None:
    rpc = FakeRpc()
    x402 = service(rpc)
    pre = prechecked(x402)

    settlement = await x402.settle(pre)

    assert (settlement.payer, settlement.amount) == (CLIENT.pubkey(), 100_000)
    assert len(rpc.simulated) == len(rpc.sent) == 1
    with pytest.raises(DuplicateSettlementError):
        await x402.settle(pre)


async def test_settles_a_smart_wallet_cpi_payment() -> None:
    rpc = FakeRpc()
    x402 = service(rpc)
    settlement = await x402.settle(prechecked(x402, cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)))
    assert settlement.signature
    assert len(rpc.sent) == 1


async def test_simulation_showing_another_outcome_submits_nothing() -> None:
    rpc = FakeRpc()
    rpc.sim_delta = 99_999
    x402 = service(rpc)
    with pytest.raises(PaymentError, match="wrong_amount"):
        await x402.settle(prechecked(x402))
    assert rpc.sent == []


async def test_landed_amount_different_from_simulation_alerts() -> None:
    rpc = FakeRpc()
    rpc.landed_delta = 0  # the simulation promised the price; the real transaction moved nothing
    x402 = service(rpc)

    with pytest.raises(SettlementMismatchError):
        await x402.settle(prechecked(x402))

    assert "post_settlement_mismatch" in (await x402.health()).alerts


async def test_landed_fee_above_the_allowed_maximum_alerts() -> None:
    rpc = FakeRpc()
    rpc.landed_fee = 2 * 5_000 + 200 + 1
    x402 = service(rpc)
    with pytest.raises(SettlementMismatchError):
        await x402.settle(prechecked(x402))


async def test_an_unindexed_settlement_stands_on_its_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fake's wSOL balance never moves; the balance used to decide this, but credit purchases land in the same
    # account at any time, so only the landed transaction's own metadata counts (#10).
    class Unindexed(FakeRpc):
        async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
            return None

    monkeypatch.setattr("veridex.x402.service._META_RETRIES", 1)
    x402 = service(Unindexed())
    assert (await x402.settle(prechecked(x402))).amount == 100_000


async def test_a_payment_that_already_landed_is_not_sent_again() -> None:
    rpc = FakeRpc()
    x402 = service(rpc)
    pre = prechecked(x402)
    # The hot wallet's signature is deterministic, so the landed transaction's signature is known before sending.
    message = pre.transaction.message
    signature = str(HOT.sign_message(to_bytes_versioned(message)))
    rpc.statuses[signature] = {"err": None, "confirmationStatus": "finalized"}
    assert (await x402.settle(pre)).signature == signature
    assert rpc.sent == []


async def test_a_payment_that_already_failed_is_not_sent_again() -> None:
    rpc = FakeRpc()
    x402 = service(rpc)
    pre = prechecked(x402)
    signature = str(HOT.sign_message(to_bytes_versioned(pre.transaction.message)))
    rpc.statuses[signature] = {"err": {"InstructionError": [2, "Custom"]}, "confirmationStatus": "confirmed"}
    with pytest.raises(TransactionFailedError):
        await x402.settle(pre)
    assert rpc.sent == []


async def test_every_client_signature_is_kept() -> None:
    second = Keypair()
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    transfer = ixs[2]
    ixs[2] = Instruction(
        transfer.program_id,
        transfer.data,
        [*transfer.accounts, AccountMeta(second.pubkey(), is_signer=True, is_writable=False)],
    )
    rpc = FakeRpc()
    x402 = service(rpc)
    tx = sign_payment(ixs, HOT.pubkey(), [CLIENT, second])
    accepts = [expected(HOT.pubkey())]
    await x402.settle(x402.precheck(payment_header(tx, accepts[0]), "quick", accepts))
    (raw,) = rpc.sent
    landed = VersionedTransaction.from_bytes(raw)
    assert len(landed.signatures) == 3
    assert list(landed.signatures[1:]) == list(tx.signatures[1:])
    signed = to_bytes_versioned(landed.message)
    assert all(s.verify(k, signed) for s, k in zip(landed.signatures, landed.message.account_keys, strict=False))


def sent_sweep(rpc: FakeRpc) -> VersionedTransaction:
    (raw,) = rpc.sent
    tx = VersionedTransaction.from_bytes(raw)
    check_sweep(tx.message, HOT.pubkey(), COLD, USDC_MINTS["solana-devnet"])
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
            "message": {
                "accountKeys": [{"pubkey": str(HOT.pubkey()), "signer": True}],
                "instructions": [{"program": "system", "parsed": {"type": "transfer", "info": info}}],
            }
        },
        "meta": {"innerInstructions": []},
    }
    x402 = service(rpc)

    assert await x402.audit_outflows()
    health = await x402.health()

    assert (health.status, "outflow_to_non_cold" in health.alerts) == ("compromise_suspected", True)
    assert await x402.audit_outflows() == []  # each transaction is checked once
