"""
x402 v2, ``exact`` scheme on Solana: what we ask for, how a payment is checked, and the only place the fee payer signs.

The client builds and signs a wSOL ``TransferChecked`` to the merchant's associated token account; Veridex verifies
it, co-signs as fee payer (sponsor) and submits it. Verification follows the spec's static layout (Path 1, standard
wallets) with the amount matched exactly. Smart-wallet payments (Path 2, simulation-based) are not accepted.
"""

import base64
import binascii
import json
import struct
from dataclasses import dataclass, field
from typing import Any

from solders.instruction import CompiledInstruction
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from veridex.x402.solana import (
    COMPUTE_BUDGET_PROGRAM,
    LIGHTHOUSE_PROGRAM,
    MEMO_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_DECIMALS,
    WSOL_MINT,
    associated_token_address,
)

X402_VERSION = 2
_SET_COMPUTE_UNIT_LIMIT = 2
_SET_COMPUTE_UNIT_PRICE = 3
_TRANSFER_CHECKED = 12
_MAX_MEMO_BYTES = 256
_MAX_LIGHTHOUSE = 3


class PaymentError(ValueError):
    """A payment payload was malformed or does not satisfy the requirements."""

    def __init__(self, reason: str, *, malformed: bool = False) -> None:
        """
        Record why the payment was refused.

        :param reason: Machine-readable reason, returned to the client.
        :param malformed: Whether the payload could not even be decoded (HTTP 400 rather than 402).
        """
        super().__init__(reason)
        self.reason = reason
        self.malformed = malformed


def encode_header(value: dict[str, Any]) -> str:
    """
    Encode an x402 object for an HTTP header (base64 of compact JSON).

    :param value: The object.
    :return: The header value.
    """
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def decode_header(value: str) -> dict[str, Any]:
    """
    Decode an x402 HTTP header back into an object.

    :param value: The base64 header value.
    :return: The decoded object.
    :raises PaymentError: If it is not base64-encoded JSON object.
    """
    try:
        decoded = json.loads(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise PaymentError("invalid_payload_encoding", malformed=True) from exc
    if not isinstance(decoded, dict):
        raise PaymentError("invalid_payload_encoding", malformed=True)
    return decoded


def requirements(
    *, network: str, amount: int, pay_to: Pubkey, fee_payer: Pubkey, timeout_s: int = 60
) -> dict[str, Any]:
    """
    Build the ``PaymentRequirements`` for one check.

    :param network: The CAIP-2 network identifier.
    :param amount: The price in lamports of wSOL.
    :param pay_to: The merchant wallet (``payTo``); the payment goes to its wSOL associated token account.
    :param fee_payer: Our sponsor key, which pays the network fee.
    :param timeout_s: How long the client has to complete the payment.
    :return: The requirements object.
    """
    return {
        "scheme": "exact",
        "network": network,
        "amount": str(amount),
        "asset": str(WSOL_MINT),
        "payTo": str(pay_to),
        "maxTimeoutSeconds": timeout_s,
        # Settlement happens before the analysis runs: it can take longer than a blockhash lives.
        "extra": {"feePayer": str(fee_payer), "paymentFlow": "upfront"},
    }


_VERIFIED = object()  # only verify_payment can mint a VerifiedPayment


@dataclass(frozen=True)
class VerifiedPayment:
    """A payment transaction that passed :func:`verify_payment`; the only thing the hot wallet co-signs."""

    transaction: VersionedTransaction
    payer: Pubkey
    amount: int
    _token: object = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse construction anywhere but :func:`verify_payment`."""
        if self._token is not _VERIFIED:
            raise TypeError("VerifiedPayment can only be created by verify_payment")


def is_verified(item: object) -> bool:
    """
    Whether ``item`` is a payment minted by :func:`verify_payment` (not constructed or copied around it).

    :param item: Anything.
    :return: ``True`` only for a genuine :class:`VerifiedPayment`.
    """
    return isinstance(item, VerifiedPayment) and item._token is _VERIFIED


def references(message: Message | MessageV0, index: int) -> bool:
    """
    Whether any instruction uses the account at ``index`` as its program or as one of its accounts.

    :param message: The transaction message.
    :param index: The account index to look for.
    :return: ``True`` if the account is referenced.
    """
    return any(ix.program_id_index == index or index in bytes(ix.accounts) for ix in message.instructions)


def _check_compute_budget(program: Pubkey, data: bytes, kind: int, max_value: int) -> None:
    """
    Check one positional compute budget instruction.

    :param program: The instruction's program.
    :param data: The instruction data.
    :param kind: The expected instruction discriminator.
    :param max_value: The largest accepted limit or price.
    :raises PaymentError: If the instruction is not the expected one or exceeds ``max_value``.
    """
    size = 5 if kind == _SET_COMPUTE_UNIT_LIMIT else 9
    if program != COMPUTE_BUDGET_PROGRAM or len(data) != size or data[0] != kind:
        raise PaymentError("invalid_compute_budget_instructions")
    value = struct.unpack("<I" if size == 5 else "<Q", data[1:])[0]
    if value > max_value:
        raise PaymentError("compute_budget_too_high")


def _check_transfer(
    message: Message | MessageV0, transfer: CompiledInstruction, *, expected: int, pay_to: Pubkey, client: Pubkey
) -> int:
    """
    Check the positional wSOL ``TransferChecked``: asset, recipient, exact amount and authority.

    :param message: The transaction message (for account resolution).
    :param transfer: The third instruction.
    :param expected: The price in lamports.
    :param pay_to: The merchant wallet (``payTo``).
    :param client: The paying signer, which must be the transfer authority.
    :return: The transferred amount.
    :raises PaymentError: If any part does not match.
    """
    keys = message.account_keys
    data = bytes(transfer.data)
    accounts = [keys[i] for i in bytes(transfer.accounts)]
    if (
        keys[transfer.program_id_index] != TOKEN_PROGRAM
        or len(data) != 10  # noqa: PLR2004
        or data[0] != _TRANSFER_CHECKED
        or len(accounts) != 4  # noqa: PLR2004
    ):
        raise PaymentError("transfer_not_found")
    amount: int = struct.unpack("<Q", data[1:9])[0]
    source, mint, destination, authority = accounts
    if mint != WSOL_MINT or data[9] != WSOL_DECIMALS:
        raise PaymentError("wrong_asset")
    if destination != associated_token_address(pay_to):
        raise PaymentError("wrong_recipient")
    if amount != expected:
        raise PaymentError("wrong_amount")
    if authority != client or source == destination:
        raise PaymentError("wrong_transfer_authority")
    return amount


def _check_trailing(programs: list[Pubkey], data: list[bytes]) -> None:
    """
    Check the instructions after the transfer: exactly one Memo, and at most three wallet Lighthouse assertions.

    :param programs: Their programs, in order.
    :param data: Their data, in order.
    :raises PaymentError: If anything else appears, or the memo is missing, repeated or oversized.
    """
    memos = [d for p, d in zip(programs, data, strict=True) if p == MEMO_PROGRAM]
    lighthouse = sum(1 for p in programs if p == LIGHTHOUSE_PROGRAM)
    if len(memos) + lighthouse != len(programs) or len(memos) != 1 or lighthouse > _MAX_LIGHTHOUSE:
        raise PaymentError("unexpected_instruction_layout")
    if not 0 < len(memos[0]) <= _MAX_MEMO_BYTES:
        raise PaymentError("invalid_memo")


def verify_payment(  # noqa: C901
    header_value: str,
    expected: dict[str, Any],
    *,
    pay_to: Pubkey,
    fee_payer: Pubkey,
    max_compute_units: int,
    max_compute_unit_price: int,
) -> VerifiedPayment:
    """
    Check a ``PAYMENT-SIGNATURE`` header against the requirements, strictly and without touching the network.

    Accepted layout: SetComputeUnitLimit, SetComputeUnitPrice, a wSOL ``TransferChecked`` of exactly the price to
    the merchant's associated token account, then exactly one Memo and up to three wallet Lighthouse assertions.
    Exactly two signers (the fee payer, then the client), the client's signature present and valid, and the fee
    payer referenced by no instruction at all, so its signature can only ever pay the network fee.

    :param header_value: The base64 ``PaymentPayload``.
    :param expected: The ``PaymentRequirements`` we issued for this request.
    :param pay_to: The merchant wallet (``payTo``).
    :param fee_payer: Our sponsor key.
    :param max_compute_units: Largest accepted compute unit limit.
    :param max_compute_unit_price: Largest accepted compute unit price (micro-lamports).
    :return: The verified payment.
    :raises PaymentError: With a reason, if anything does not match.
    """
    payload = decode_header(header_value)
    if payload.get("x402Version") != X402_VERSION:
        raise PaymentError("unsupported_x402_version")
    accepted = payload.get("accepted")
    if not isinstance(accepted, dict) or any(
        accepted.get(key) != expected[key] for key in ("scheme", "network", "amount", "asset", "payTo")
    ):
        raise PaymentError("requirements_mismatch")
    if (accepted.get("extra") or {}).get("feePayer") != str(fee_payer):
        raise PaymentError("fee_payer_mismatch")

    try:
        raw = base64.b64decode(payload["payload"]["transaction"], validate=True)
        tx = VersionedTransaction.from_bytes(raw)
    except (KeyError, TypeError, binascii.Error, ValueError) as exc:
        raise PaymentError("invalid_transaction", malformed=True) from exc

    message = tx.message
    if not isinstance(message, Message | MessageV0):
        raise PaymentError("unsupported_message_version")
    if isinstance(message, MessageV0) and message.address_table_lookups:
        raise PaymentError("address_lookup_tables_not_supported")
    keys = message.account_keys
    if message.header.num_required_signatures != 2 or len(tx.signatures) != 2:  # noqa: PLR2004
        raise PaymentError("unexpected_signers")
    if keys[0] != fee_payer:
        raise PaymentError("fee_payer_mismatch")
    client = keys[1]
    if client == fee_payer or not tx.signatures[1].verify(client, to_bytes_versioned(message)):
        raise PaymentError("invalid_client_signature")
    if references(message, 0):
        raise PaymentError("fee_payer_not_isolated")

    instructions = message.instructions
    if not 4 <= len(instructions) <= 3 + 1 + _MAX_LIGHTHOUSE:  # noqa: PLR2004
        raise PaymentError("unexpected_instruction_layout")

    def program(i: int) -> Pubkey:
        return keys[instructions[i].program_id_index]

    _check_compute_budget(program(0), bytes(instructions[0].data), _SET_COMPUTE_UNIT_LIMIT, max_compute_units)
    _check_compute_budget(program(1), bytes(instructions[1].data), _SET_COMPUTE_UNIT_PRICE, max_compute_unit_price)

    amount = _check_transfer(message, instructions[2], expected=int(expected["amount"]), pay_to=pay_to, client=client)
    _check_trailing([program(i) for i in range(3, len(instructions))], [bytes(ix.data) for ix in instructions[3:]])
    return VerifiedPayment(transaction=tx, payer=client, amount=amount, _token=_VERIFIED)
