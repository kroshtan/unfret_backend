"""The hot wallet's single signing choke point: verified payments (fee only) and sweeps to cold, nothing else."""

from typing import Any

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import BLOCKHASH, expected, memo, payment_header, payment_instructions, sign_payment
from veridex.x402.payment import verify_payment
from veridex.x402.solana import TOKEN_PROGRAM, WSOL_MINT, associated_token_address
from veridex.x402.wallet import (
    HotWallet,
    Sweep,
    build_sweep,
    check_sweep,
    close_account,
    create_ata_idempotent,
    unexpected_outflows,
)

HOT = Keypair()
COLD = Keypair().pubkey()
CLIENT = Keypair()
WALLET = HotWallet(HOT, COLD)


def compile_message(instructions: list[Instruction], payer: Pubkey | None = None) -> MessageV0:
    return MessageV0.try_compile(payer or HOT.pubkey(), instructions, [], Hash.default())


def verified_payment() -> Any:
    tx = sign_payment(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), HOT.pubkey(), [CLIENT])
    return verify_payment(
        payment_header(tx, expected(HOT.pubkey())),
        expected(HOT.pubkey()),
        pay_to=HOT.pubkey(),
        fee_payer=HOT.pubkey(),
        max_compute_units=400_000,
        max_compute_unit_price=50_000,
    )


def test_cosigns_a_verified_payment_as_fee_payer_only() -> None:
    signed = VersionedTransaction.from_bytes(WALLET.sign(verified_payment()))
    message = to_bytes_versioned(signed.message)
    assert signed.signatures[0].verify(HOT.pubkey(), message)
    assert signed.signatures[1].verify(CLIENT.pubkey(), message)


def test_refuses_anything_that_is_not_a_verified_payment_or_a_sweep() -> None:
    payment = verified_payment()
    raw_transfer = compile_message([transfer(TransferParams(from_pubkey=HOT.pubkey(), to_pubkey=COLD, lamports=1))])
    for item in (payment.transaction, payment.transaction.message, raw_transfer, b"bytes", None):
        with pytest.raises(PermissionError):
            WALLET.sign(item)  # type: ignore[arg-type]


def test_signs_a_sweep_to_cold() -> None:
    sweep = build_sweep(HOT.pubkey(), COLD, ata_exists=True, to_cold=50_000_000, blockhash=BLOCKHASH)
    signed = VersionedTransaction.from_bytes(WALLET.sign(sweep))
    assert signed.signatures[0].verify(HOT.pubkey(), to_bytes_versioned(signed.message))


@pytest.mark.parametrize(("ata_exists", "to_cold"), [(True, 1), (True, 0), (False, 1), (False, 0)])
def test_every_sweep_shape_passes_its_own_check(ata_exists: bool, to_cold: int) -> None:
    sweep = build_sweep(HOT.pubkey(), COLD, ata_exists=ata_exists, to_cold=to_cold, blockhash=BLOCKHASH)
    check_sweep(sweep.message, HOT.pubkey(), COLD)


def test_sweep_cannot_be_forged() -> None:
    with pytest.raises(TypeError):
        Sweep(message=compile_message([create_ata_idempotent(HOT.pubkey(), HOT.pubkey())]), to_cold=0, _token=object())


ATA = associated_token_address(HOT.pubkey())
ELSEWHERE = Keypair().pubkey()
BAD_SWEEPS = {
    "transfer to another address": [
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey()),
        transfer(TransferParams(from_pubkey=HOT.pubkey(), to_pubkey=ELSEWHERE, lamports=1)),
    ],
    "two transfers": [
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey()),
        transfer(TransferParams(from_pubkey=HOT.pubkey(), to_pubkey=COLD, lamports=1)),
        transfer(TransferParams(from_pubkey=HOT.pubkey(), to_pubkey=COLD, lamports=1)),
    ],
    "close into another address": [
        close_account(ATA, ELSEWHERE, HOT.pubkey()),
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey()),
    ],
    "account for another owner": [create_ata_idempotent(HOT.pubkey(), ELSEWHERE)],
    "token transfer": [
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey()),
        Instruction(
            TOKEN_PROGRAM,
            bytes([12]) + (1).to_bytes(8, "little") + bytes([9]),
            [
                AccountMeta(ATA, False, True),
                AccountMeta(WSOL_MINT, False, False),
                AccountMeta(HOT.pubkey(), True, False),
            ],
        ),
    ],
    "memo": [create_ata_idempotent(HOT.pubkey(), HOT.pubkey()), memo()],
    "no recreate": [close_account(ATA, HOT.pubkey(), HOT.pubkey())],
    "transfer only": [transfer(TransferParams(from_pubkey=HOT.pubkey(), to_pubkey=COLD, lamports=1))],
}


@pytest.mark.parametrize("name", BAD_SWEEPS)
def test_check_sweep_refuses_other_instructions_and_destinations(name: str) -> None:
    with pytest.raises(PermissionError):
        check_sweep(compile_message(BAD_SWEEPS[name]), HOT.pubkey(), COLD)


def test_sweep_paid_by_someone_else_or_with_extra_signer_is_refused() -> None:
    other = Keypair().pubkey()
    with pytest.raises(PermissionError):
        check_sweep(
            compile_message([create_ata_idempotent(HOT.pubkey(), HOT.pubkey())], payer=other), HOT.pubkey(), COLD
        )
    extra_signer = Instruction(
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey()).program_id,
        bytes([1]),
        [AccountMeta(other, True, True)],
    )
    with pytest.raises(PermissionError):
        check_sweep(compile_message([extra_signer]), HOT.pubkey(), COLD)


def test_signer_rechecks_a_tampered_sweep() -> None:
    sweep = build_sweep(HOT.pubkey(), COLD, ata_exists=True, to_cold=1, blockhash=BLOCKHASH)
    evil = compile_message(BAD_SWEEPS["transfer to another address"])
    object.__setattr__(sweep, "message", evil)  # bypass frozen: the signer must still refuse
    with pytest.raises(PermissionError):
        WALLET.sign(sweep)


def test_hot_wallet_must_not_be_the_cold_address() -> None:
    with pytest.raises(ValueError, match="cold"):
        HotWallet(HOT, HOT.pubkey())


def test_repr_hides_the_secret() -> None:
    assert str(HOT) not in repr(WALLET)


def parsed(*instructions: dict[str, Any], inner: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "transaction": {"message": {"instructions": list(instructions)}},
        "meta": {"innerInstructions": [{"instructions": inner or []}]},
    }


def system_transfer(source: Pubkey, destination: Pubkey) -> dict[str, Any]:
    info = {"source": str(source), "destination": str(destination), "lamports": 1}
    return {"program": "system", "parsed": {"type": "transfer", "info": info}}


def test_outflow_audit_accepts_sweeps_and_payments() -> None:
    sweep = parsed(
        {
            "program": "spl-token",
            "parsed": {
                "type": "closeAccount",
                "info": {"account": str(ATA), "destination": str(HOT.pubkey()), "owner": str(HOT.pubkey())},
            },
        },
        system_transfer(HOT.pubkey(), COLD),
        inner=[
            {
                "program": "system",
                "parsed": {"type": "createAccount", "info": {"source": str(HOT.pubkey()), "newAccount": str(ATA)}},
            }
        ],
    )
    payment = parsed(
        {
            "program": "spl-token",
            "parsed": {
                "type": "transferChecked",
                "info": {"authority": str(CLIENT.pubkey()), "destination": str(ATA)},
            },
        }
    )
    assert unexpected_outflows(sweep, HOT.pubkey(), COLD) == []
    assert unexpected_outflows(payment, HOT.pubkey(), COLD) == []


def test_outflow_audit_flags_transfers_elsewhere() -> None:
    sol_out = parsed(system_transfer(HOT.pubkey(), ELSEWHERE))
    token_out = parsed(
        {
            "program": "spl-token",
            "parsed": {"type": "transfer", "info": {"authority": str(HOT.pubkey()), "destination": str(ELSEWHERE)}},
        }
    )
    inner_out = parsed(inner=[system_transfer(HOT.pubkey(), ELSEWHERE)])
    for tx in (sol_out, token_out, inner_out):
        assert unexpected_outflows(tx, HOT.pubkey(), COLD)
