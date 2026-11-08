"""
x402 v2, ``exact`` scheme on Solana: what we ask for and how a payment is checked.

The client builds and signs a transaction that pays the price in wSOL or USDC into the hot wallet's token account
for that mint:
a plain ``TransferChecked`` from a standard wallet, or any program instructions that achieve it, such as a smart
wallet (Squads and other program-controlled wallets) paying by CPI. Veridex verifies it, co-signs as fee payer and
submits it. Verification is behaviour-based, in two stages:

1. :func:`precheck` (no network): the hot wallet appears in no instruction, so its signature can only pay the fee;
   every other required signature is present and valid; compute budget and priority fee are capped; exactly one
   memo (the spec's uniqueness nonce); no signature-verification precompiles (they add fee-paid signatures); size.
2. :func:`confirm_by_simulation`: a simulation shows the hot wallet's token account for the paid mint growing by
   exactly the accepted amount and the hot wallet losing no more than the computed fee.

USDC amounts are converted from the SOL price list at quote time, so an accepted USDC amount may differ from the
current quote by up to a tolerance (see :func:`precheck`); wSOL amounts must match exactly.

After settlement the service checks the real on-chain delta again (``X402.settle``).
"""

import base64
import binascii
import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any

from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from veridex.x402.solana import COMPUTE_BUDGET_PROGRAM, MEMO_PROGRAM, WSOL_MINT, SolanaRpc, associated_token_address

X402_VERSION = 2
LAMPORTS_PER_SIGNATURE = 5_000
MAX_TRANSACTION_BYTES = 1_232  # Solana's packet limit for a serialised transaction
_REQUEST_HEAP_FRAME, _SET_COMPUTE_UNIT_LIMIT, _SET_COMPUTE_UNIT_PRICE, _SET_LOADED_ACCOUNTS_DATA_SIZE = 1, 2, 3, 4
_MAX_MEMO_BYTES = 256
# Each signature these programs verify is charged to the fee payer like a transaction signature.
PRECOMPILES = frozenset(
    Pubkey.from_string(p)
    for p in (
        "Ed25519SigVerify111111111111111111111111111",
        "KeccakSecp256k11111111111111111111111111111",
        "Secp256r1SigVerify1111111111111111111111111",
    )
)


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
    *,
    network: str,
    amount: int,
    pay_to: Pubkey,
    fee_payer: Pubkey,
    asset: Pubkey = WSOL_MINT,
    timeout_s: int = 60,
) -> dict[str, Any]:
    """
    Build the ``PaymentRequirements`` for one check in one asset.

    :param network: The CAIP-2 network identifier.
    :param amount: The price in the asset's base units (lamports of wSOL, or USDC with 6 decimals).
    :param pay_to: The merchant wallet (``payTo``); the payment goes to its associated token account for ``asset``.
    :param fee_payer: Our sponsor key, which pays the network fee.
    :param asset: The mint to pay in (wSOL by default).
    :param timeout_s: How long the client has to complete the payment.
    :return: The requirements object.
    """
    return {
        "scheme": "exact",
        "network": network,
        "amount": str(amount),
        "asset": str(asset),
        "payTo": str(pay_to),
        "maxTimeoutSeconds": timeout_s,
        # Settlement happens before the analysis runs: it can take longer than a blockhash lives.
        "extra": {"feePayer": str(fee_payer), "paymentFlow": "upfront"},
    }


@dataclass(frozen=True)
class Precheck:
    """A payment that passed the static checks, waiting for :func:`confirm_by_simulation`."""

    transaction: VersionedTransaction
    payer: Pubkey
    amount: int
    max_fee: int
    mint: Pubkey = WSOL_MINT


_VERIFIED = object()  # only confirm_by_simulation can mint a VerifiedPayment


@dataclass(frozen=True)
class VerifiedPayment:
    """A payment that passed both verification stages; the only kind of payment the hot wallet co-signs."""

    transaction: VersionedTransaction
    payer: Pubkey
    amount: int
    max_fee: int
    mint: Pubkey
    _token: object = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse construction anywhere but :func:`confirm_by_simulation`."""
        if self._token is not _VERIFIED:
            raise TypeError("VerifiedPayment can only be created by confirm_by_simulation")


def is_verified(item: object) -> bool:
    """
    Whether ``item`` is a payment minted by :func:`confirm_by_simulation` (not constructed or copied around it).

    :param item: Anything.
    :return: ``True`` only for a genuine :class:`VerifiedPayment`.
    """
    return isinstance(item, VerifiedPayment) and item._token is _VERIFIED


def references(message: Message | MessageV0, index: int) -> bool:
    """
    Whether any top-level instruction uses the account at ``index`` as its program or as one of its accounts.

    Programs can only pass on (via CPI) accounts they were given, so an account that no top-level instruction
    references can't be touched by any inner instruction either.

    :param message: The transaction message.
    :param index: The account index to look for.
    :return: ``True`` if the account is referenced.
    """
    return any(ix.program_id_index == index or index in bytes(ix.accounts) for ix in message.instructions)


def compute_budget(message: Message | MessageV0, max_units: int, max_price: int) -> int:
    """
    Check the compute budget instructions and return the largest priority fee they allow, in lamports.

    :param message: The transaction message.
    :param max_units: Largest accepted compute unit limit.
    :param max_price: Largest accepted compute unit price (micro-lamports).
    :return: The maximum priority fee.
    :raises PaymentError: If an instruction is unknown, repeated, malformed or above a cap.
    """
    seen: dict[int, int] = {}
    for ix in message.instructions:
        if message.account_keys[ix.program_id_index] != COMPUTE_BUDGET_PROGRAM:
            continue
        data = bytes(ix.data)
        kind = data[0] if data else -1
        sizes = {
            _REQUEST_HEAP_FRAME: 5,
            _SET_COMPUTE_UNIT_LIMIT: 5,
            _SET_COMPUTE_UNIT_PRICE: 9,
            _SET_LOADED_ACCOUNTS_DATA_SIZE: 5,
        }
        if kind not in sizes or len(data) != sizes[kind] or kind in seen:
            raise PaymentError("invalid_compute_budget_instructions")
        seen[kind] = struct.unpack("<I" if sizes[kind] == 5 else "<Q", data[1:])[0]  # noqa: PLR2004
    limit, price = seen.get(_SET_COMPUTE_UNIT_LIMIT), seen.get(_SET_COMPUTE_UNIT_PRICE, 0)
    if price > max_price or (limit is not None and limit > max_units):
        raise PaymentError("compute_budget_too_high")
    if price and limit is None:
        # Without an explicit limit the runtime's default (up to 1.4M units) would set our priority fee.
        raise PaymentError("compute_unit_limit_required")
    return math.ceil((limit or 0) * price / 1_000_000)


def matching_option(
    accepted: dict[str, Any], options: list[dict[str, Any]], tolerance_bps: dict[str, int]
) -> dict[str, Any]:
    """
    Find our option the client accepted.

    Same scheme, network, asset and payTo, and the same amount; for an asset priced by conversion, an amount within
    its tolerance of the current quote.

    :param accepted: The client's ``accepted`` requirements.
    :param options: The ``accepts`` we offer right now.
    :param tolerance_bps: Allowed amount deviation per asset (base58 mint), in basis points; missing means exact.
    :return: Our matching option.
    :raises PaymentError: If none matches.
    """
    for option in options:
        if any(accepted.get(key) != option[key] for key in ("scheme", "network", "asset", "payTo")):
            continue
        try:
            paid, quoted = int(str(accepted.get("amount"))), int(option["amount"])
        except ValueError:
            break
        if paid > 0 and abs(paid - quoted) * 10_000 <= quoted * tolerance_bps.get(option["asset"], 0):
            return option
    raise PaymentError("requirements_mismatch")


def precheck(  # noqa: C901, PLR0912
    header_value: str,
    expected: dict[str, Any] | list[dict[str, Any]],
    *,
    fee_payer: Pubkey,
    max_compute_units: int,
    max_compute_unit_price: int,
    tolerance_bps: dict[str, int] | None = None,
) -> Precheck:
    """
    Run every check that needs no network on a ``PAYMENT-SIGNATURE`` header.

    :param header_value: The base64 ``PaymentPayload``.
    :param expected: The ``PaymentRequirements`` we offer for this request (one, or the whole ``accepts`` list).
    :param fee_payer: Our sponsor key (the hot wallet).
    :param max_compute_units: Largest accepted compute unit limit.
    :param max_compute_unit_price: Largest accepted compute unit price (micro-lamports).
    :param tolerance_bps: How far an accepted amount may be from our current quote, per asset (default: exact).
    :return: The prechecked payment.
    :raises PaymentError: With a reason, if anything does not match.
    """
    payload = decode_header(header_value)
    if payload.get("x402Version") != X402_VERSION:
        raise PaymentError("unsupported_x402_version")
    accepted = payload.get("accepted")
    if not isinstance(accepted, dict):
        raise PaymentError("requirements_mismatch")
    options = expected if isinstance(expected, list) else [expected]
    option = matching_option(accepted, options, tolerance_bps or {})
    if (accepted.get("extra") or {}).get("feePayer") != str(fee_payer):
        raise PaymentError("fee_payer_mismatch")

    try:
        raw = base64.b64decode(payload["payload"]["transaction"], validate=True)
        tx = VersionedTransaction.from_bytes(raw)
    except (KeyError, TypeError, binascii.Error, ValueError) as exc:
        raise PaymentError("invalid_transaction", malformed=True) from exc
    if len(raw) > MAX_TRANSACTION_BYTES:
        raise PaymentError("transaction_too_large")

    message = tx.message
    if not isinstance(message, Message | MessageV0):
        raise PaymentError("unsupported_message_version")
    keys = message.account_keys
    signers = message.header.num_required_signatures
    if keys[0] != fee_payer:
        raise PaymentError("fee_payer_mismatch")
    if signers < 2 or len(tx.signatures) != signers:  # noqa: PLR2004
        raise PaymentError("unexpected_signers")
    signed = to_bytes_versioned(message)
    if not all(tx.signatures[i].verify(keys[i], signed) for i in range(1, signers)):
        raise PaymentError("invalid_client_signature")
    if references(message, 0):
        raise PaymentError("fee_payer_not_isolated")
    if any(keys[ix.program_id_index] in PRECOMPILES for ix in message.instructions):
        raise PaymentError("precompile_not_allowed")

    priority_fee = compute_budget(message, max_compute_units, max_compute_unit_price)
    memos = [bytes(ix.data) for ix in message.instructions if keys[ix.program_id_index] == MEMO_PROGRAM]
    if len(memos) != 1 or not 0 < len(memos[0]) <= _MAX_MEMO_BYTES:
        raise PaymentError("memo_required")
    if (required_memo := (option.get("extra") or {}).get("memo")) and memos[0] != required_memo.encode():
        raise PaymentError("memo_mismatch")

    return Precheck(
        transaction=tx,
        payer=keys[1],
        amount=int(str(accepted["amount"])),
        max_fee=LAMPORTS_PER_SIGNATURE * signers + priority_fee,
        mint=Pubkey.from_string(option["asset"]),
    )


async def confirm_by_simulation(pre: Precheck, *, rpc: SolanaRpc, pay_to: Pubkey) -> VerifiedPayment:
    """
    Simulate the payment: exactly the amount into ``pay_to``'s account for the paid mint, no debit beyond the fee.

    Call it with settlements serialised (``X402`` holds a lock), so no payment of ours lands between the balance
    read and the simulation.

    :param pre: A payment that passed :func:`precheck`.
    :param rpc: The Solana RPC.
    :param pay_to: The hot wallet (``payTo`` and fee payer).
    :return: The verified payment.
    :raises PaymentError: If the simulation fails or shows any other outcome.
    """
    account = associated_token_address(pay_to, pre.mint)
    before_token = await rpc.token_balance(account)
    if before_token is None:
        raise PaymentError("pay_to_account_not_ready")
    before_sol = await rpc.get_balance(pay_to)
    error, (after_account, after_payer) = await rpc.simulate_accounts(bytes(pre.transaction), [account, pay_to])
    if error is not None or after_account is None or after_payer is None:
        raise PaymentError("simulation_failed")
    after_token = int(after_account["data"]["parsed"]["info"]["tokenAmount"]["amount"])
    if after_token - before_token != pre.amount:
        raise PaymentError("wrong_amount")
    if before_sol - int(after_payer["lamports"]) > pre.max_fee:
        raise PaymentError("fee_payer_debited")
    return VerifiedPayment(
        transaction=pre.transaction,
        payer=pre.payer,
        amount=pre.amount,
        max_fee=pre.max_fee,
        mint=pre.mint,
        _token=_VERIFIED,
    )
