import base64
import json

import pytest
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from tests.unit.x402_helpers import expected, memo, payment_header, payment_instructions, sign_payment
from veridex.x402.payment import PaymentError, VerifiedPayment, encode_header, verify_payment
from veridex.x402.solana import MEMO_PROGRAM

HOT = Keypair()  # payTo and fee payer
CLIENT = Keypair()
LIMITS = {"max_compute_units": 400_000, "max_compute_unit_price": 50_000}


def verify(header: str, amount: int = 100_000) -> VerifiedPayment:
    return verify_payment(
        header, expected(HOT.pubkey(), amount), pay_to=HOT.pubkey(), fee_payer=HOT.pubkey(), **LIMITS
    )


def header_for(instructions: list[Instruction], *, fee_payer: Pubkey | None = None, sign: bool = True) -> str:
    tx = sign_payment(instructions, fee_payer or HOT.pubkey(), [CLIENT], sign=sign)
    return payment_header(tx, expected(HOT.pubkey()))


def reason(header: str) -> str:
    with pytest.raises(PaymentError) as info:
        verify(header)
    return info.value.reason


def test_valid_payment_verifies() -> None:
    payment = verify(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)))
    assert (payment.payer, payment.amount) == (CLIENT.pubkey(), 100_000)


def test_wrong_amount_is_rejected() -> None:
    assert reason(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 99_999))) == "wrong_amount"
    assert reason(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_001))) == "wrong_amount"


def test_wrong_mint_is_rejected() -> None:
    usdc = Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    assert reason(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, mint=usdc))) == "wrong_asset"


def test_wrong_destination_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, destination=Keypair().pubkey())
    assert reason(header_for(ixs)) == "wrong_recipient"


def test_paying_the_hot_wallet_itself_instead_of_its_wsol_account_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, destination=HOT.pubkey())
    with pytest.raises(PaymentError):
        verify(header_for(ixs))


def test_extra_instruction_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs.append(transfer(TransferParams(from_pubkey=CLIENT.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1)))
    assert reason(header_for(ixs)) == "unexpected_instruction_layout"


def test_missing_or_repeated_memo_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs[:3])) == "unexpected_instruction_layout"
    assert reason(header_for([*ixs, memo(b"another-nonce-0123456789")])) == "unexpected_instruction_layout"


def test_wrong_fee_payer_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs, fee_payer=Keypair().pubkey())) == "fee_payer_mismatch"


def test_hot_wallet_referenced_by_an_instruction_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[3] = Instruction(MEMO_PROGRAM, b"0123456789abcdef", [AccountMeta(HOT.pubkey(), True, False)])
    assert reason(header_for(ixs)) == "fee_payer_not_isolated"


def test_hot_wallet_as_transfer_source_is_rejected() -> None:
    # The hot wallet may never be the source of a payment, not even of its own wSOL.
    tx = sign_payment(payment_instructions(HOT.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [])
    with pytest.raises(PaymentError):
        verify(payment_header(tx, expected(HOT.pubkey())))


def test_unsigned_client_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    assert reason(header_for(ixs, sign=False)) == "invalid_client_signature"


def test_excessive_priority_fee_is_rejected() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, cu_price=50_001)
    assert reason(header_for(ixs)) == "compute_budget_too_high"


def test_third_signer_is_rejected() -> None:
    extra = Keypair()
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    ixs[3] = Instruction(MEMO_PROGRAM, b"0123456789abcdef", [AccountMeta(extra.pubkey(), True, False)])
    tx = sign_payment(ixs, HOT.pubkey(), [CLIENT, extra])
    assert reason(payment_header(tx, expected(HOT.pubkey()))) == "unexpected_signers"


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
        verify(header)
    assert info.value.malformed


def test_wrong_version_is_rejected() -> None:
    payload = json.loads(base64.b64decode(header_for(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))))
    payload["x402Version"] = 1
    assert reason(encode_header(payload)) == "unsupported_x402_version"


def test_verified_payment_cannot_be_forged() -> None:
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [CLIENT])
    with pytest.raises(TypeError):
        VerifiedPayment(transaction=tx, payer=CLIENT.pubkey(), amount=100_000, _token=object())
