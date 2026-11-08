import base64
import json

import pytest
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from tests.unit.x402_helpers import (
    VAULT_PROGRAM,
    FakeRpc,
    cpi_payment_instructions,
    expected,
    memo,
    payment_header,
    payment_instructions,
    sign_payment,
)
from veridex.x402.payment import (
    PaymentError,
    Precheck,
    VerifiedPayment,
    confirm_by_simulation,
    encode_header,
    precheck,
)
from veridex.x402.solana import MEMO_PROGRAM

HOT = Keypair()  # payTo and fee payer
CLIENT = Keypair()
LIMITS = {"max_compute_units": 400_000, "max_compute_unit_price": 50_000}


def check(header: str, amount: int = 100_000) -> Precheck:
    return precheck(header, expected(HOT.pubkey(), amount), fee_payer=HOT.pubkey(), **LIMITS)


def header_for(
    instructions: list[Instruction], *, fee_payer: Pubkey | None = None, sign: bool = True, signers: list[Keypair] | None = None
) -> str:
    tx = sign_payment(instructions, fee_payer or HOT.pubkey(), signers or [CLIENT], sign=sign)
    return payment_header(tx, expected(HOT.pubkey()))


def reason(header: str) -> str:
    with pytest.raises(PaymentError) as info:
        check(header)
    return info.value.reason


# ── Stage 1: precheck (no network) ───────────────────────────────────────────


def test_standard_payment_passes_precheck() -> None:
    pre = check(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)))
    assert (pre.payer, pre.amount) == (CLIENT.pubkey(), 100_000)
    # Two signatures at 5,000 lamports plus 200,000 CU x 1,000 micro-lamports.
    assert pre.max_fee == 2 * 5_000 + 200


def test_smart_wallet_payment_passes_precheck() -> None:
    pre = check(header_for(cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)))
    assert pre.payer == CLIENT.pubkey()


def test_extra_instructions_are_fine_now() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs.append(transfer(TransferParams(from_pubkey=CLIENT.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1)))
    check(header_for(ixs))


def test_more_client_signers_are_fine_when_all_signed() -> None:
    cosigner = Keypair()
    ixs = cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[3] = Instruction(MEMO_PROGRAM, b"0123456789abcdef", [AccountMeta(cosigner.pubkey(), True, False)])
    pre = check(header_for(ixs, signers=[CLIENT, cosigner]))
    assert pre.max_fee == 3 * 5_000 + 200


def test_missing_cosigner_signature_is_rejected() -> None:
    cosigner = Keypair()
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[3] = Instruction(MEMO_PROGRAM, b"0123456789abcdef", [AccountMeta(cosigner.pubkey(), True, False)])
    tx = sign_payment(ixs, HOT.pubkey(), [CLIENT, cosigner])
    tx.signatures[2]  # present
    unsigned = sign_payment(ixs, HOT.pubkey(), [CLIENT, cosigner], sign=False)
    assert reason(payment_header(unsigned, expected(HOT.pubkey()))) == "invalid_client_signature"


def test_cpi_listing_the_hot_wallet_as_an_account_is_rejected() -> None:
    ixs = cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[2] = Instruction(VAULT_PROGRAM, bytes(ixs[2].data), [*ixs[2].accounts, AccountMeta(HOT.pubkey(), False, True)])
    assert reason(header_for(ixs)) == "fee_payer_not_isolated"


def test_hot_wallet_as_a_program_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[3] = Instruction(MEMO_PROGRAM, b"0123456789abcdef", [AccountMeta(HOT.pubkey(), True, False)])
    assert reason(header_for(ixs)) == "fee_payer_not_isolated"


def test_wrong_fee_payer_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs, fee_payer=Keypair().pubkey())) == "fee_payer_mismatch"


def test_unsigned_client_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs, sign=False)) == "invalid_client_signature"


def test_excessive_compute_price_is_rejected() -> None:
    ixs = cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, cu_price=50_001)
    assert reason(header_for(ixs)) == "compute_budget_too_high"


def test_excessive_compute_limit_is_rejected() -> None:
    from solders.compute_budget import set_compute_unit_limit

    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[0] = set_compute_unit_limit(400_001)
    assert reason(header_for(ixs)) == "compute_budget_too_high"


def test_priority_fee_without_explicit_limit_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs[1:])) == "compute_unit_limit_required"


def test_repeated_compute_budget_instruction_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for([ixs[0], *ixs])) == "invalid_compute_budget_instructions"


def test_missing_or_repeated_memo_is_rejected() -> None:
    ixs = cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs[:3])) == "memo_required"
    assert reason(header_for([*ixs, memo(b"another-nonce-0123456789")])) == "memo_required"


def test_signature_precompiles_are_rejected() -> None:
    ed25519 = Pubkey.from_string("Ed25519SigVerify111111111111111111111111111")
    ixs = [*payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), Instruction(ed25519, bytes([1, 0]), [])]
    assert reason(header_for(ixs)) == "precompile_not_allowed"


def test_oversized_transaction_is_rejected() -> None:
    padding = Instruction(Keypair().pubkey(), bytes(1_000), [])
    ixs = [*payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), padding]
    assert reason(header_for(ixs)) == "transaction_too_large"


def test_requirements_tampering_is_rejected() -> None:
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 1), HOT.pubkey(), [CLIENT])
    assert reason(payment_header(tx, expected(HOT.pubkey(), amount=1))) == "requirements_mismatch"


@pytest.mark.parametrize(
    "header",
    [
        "not base64!!",
        base64.b64encode(b"[1, 2]").decode(),
        encode_header(
            {
                "x402Version": 2,
                "accepted": expected(HOT.pubkey()),
                "payload": {"transaction": base64.b64encode(b"garbage").decode()},
            }
        ),
    ],
)
def test_malformed_headers_are_400s(header: str) -> None:
    with pytest.raises(PaymentError) as info:
        check(header)
    assert info.value.malformed


def test_wrong_version_is_rejected() -> None:
    payload = json.loads(base64.b64decode(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))))
    payload["x402Version"] = 1
    assert reason(encode_header(payload)) == "unsupported_x402_version"


# ── Stage 2: simulation ──────────────────────────────────────────────────────


async def simulate(rpc: FakeRpc, instructions: list[Instruction]) -> VerifiedPayment:
    return await confirm_by_simulation(check(header_for(instructions)), rpc=rpc, pay_to=HOT.pubkey())


async def test_simulation_confirms_a_standard_payment() -> None:
    payment = await simulate(FakeRpc(), payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))
    assert payment.amount == 100_000


async def test_simulation_confirms_a_smart_wallet_cpi_payment() -> None:
    rpc = FakeRpc()
    payment = await simulate(rpc, cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))
    assert payment.payer == CLIENT.pubkey()
    assert len(rpc.simulated) == 1


@pytest.mark.parametrize("delta", [99_999, 100_001, 0, 200_000])
async def test_wrong_amount_via_cpi_is_rejected(delta: int) -> None:
    rpc = FakeRpc()
    rpc.sim_delta = delta
    with pytest.raises(PaymentError, match="wrong_amount"):
        await simulate(rpc, cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))


async def test_extra_hot_wallet_debit_is_rejected() -> None:
    rpc = FakeRpc()
    rpc.sim_debit = 2 * 5_000 + 200 + 1  # one lamport more than the most the fee can be
    with pytest.raises(PaymentError, match="fee_payer_debited"):
        await simulate(rpc, cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))


async def test_failed_simulation_is_rejected() -> None:
    rpc = FakeRpc()
    rpc.sim_error = {"InstructionError": [2, "Custom"]}
    with pytest.raises(PaymentError, match="simulation_failed"):
        await simulate(rpc, cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))


async def test_missing_wsol_account_is_rejected() -> None:
    with pytest.raises(PaymentError, match="pay_to_account_not_ready"):
        await simulate(FakeRpc(wsol=None), payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))


def test_verified_payment_cannot_be_forged() -> None:
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [CLIENT])
    with pytest.raises(TypeError):
        VerifiedPayment(transaction=tx, payer=CLIENT.pubkey(), amount=100_000, max_fee=0, _token=object())
