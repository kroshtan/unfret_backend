"""
The hot wallet: receives x402 payments (its wSOL and USDC accounts), pays their fees, and sweeps both to cold.

Every use of its key goes through one function, :meth:`HotWallet.sign`, which accepts exactly two things:

* a :class:`~veridex.x402.payment.VerifiedPayment`, co-signed only as fee payer: the hot wallet may not appear in
  any instruction, so its signature can only pay the network fee;
* a :class:`Sweep`, re-validated here instruction by instruction, in one of two shapes. A SOL sweep: optionally close
  the hot wallet's wSOL account back into the hot wallet (unwrapping), recreate that account, create its USDC account
  if missing, and optionally transfer SOL to the cold address. A USDC sweep: optionally create the cold address's
  USDC account (idempotent, rent paid by the hot wallet), then ``TransferChecked`` USDC from the hot wallet's USDC
  account into it. Nothing else, and no other destination.

The product never swaps: USDC goes to cold as USDC, and converting it is the operator's business. Credit purchases
also pay into the hot wallet; their scanner (veridex.credits.scanner) counts only INCOMING transfers, ignoring the
sweeps and fee payments this module signs.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message, MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from veridex.x402.payment import VerifiedPayment, is_verified, references
from veridex.x402.solana import (
    ASSOCIATED_TOKEN_PROGRAM,
    TOKEN_PROGRAM,
    USDC_DECIMALS,
    WSOL_MINT,
    associated_token_address,
    transfer_checked,
)

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
_CLOSE_ACCOUNT = 9
_CREATE_IDEMPOTENT = 1
_SYSTEM_TRANSFER = 2
_TRANSFER_CHECKED = 12


def load_keypair(secret: str) -> Keypair:
    """
    Load a keypair from a base58 secret key or a Solana CLI JSON byte array.

    :param secret: The secret, in either format.
    :return: The keypair.
    :raises ValueError: If the secret is in neither format.
    """
    secret = secret.strip()
    try:
        if secret.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(secret)))
        return Keypair.from_base58_string(secret)
    except ValueError, TypeError, OverflowError:  # JSON, byte values, the key itself
        # Deliberately unchained and without detail: parsing errors can echo part of the secret.
        raise ValueError("invalid keypair") from None


_SWEEP = object()  # only build_sweep can mint a Sweep


@dataclass(frozen=True)
class Sweep:
    """An unsigned sweep transaction from :func:`build_sweep`."""

    message: MessageV0
    to_cold: int  # lamports
    _token: object = field(repr=False)
    usdc_to_cold: int = 0  # USDC base units

    def __post_init__(self) -> None:
        """Refuse construction anywhere but :func:`build_sweep`."""
        if self._token is not _SWEEP:
            raise TypeError("Sweep can only be created by build_sweep")


def close_account(account: Pubkey, destination: Pubkey, owner: Pubkey) -> Instruction:
    """
    SPL ``CloseAccount``: close a token account, sending its lamports (for wSOL: the wrapped SOL) to ``destination``.

    :param account: The token account.
    :param destination: Receives the lamports.
    :param owner: The account owner (signs).
    :return: The instruction.
    """
    return Instruction(
        TOKEN_PROGRAM,
        bytes([_CLOSE_ACCOUNT]),
        [
            AccountMeta(account, is_signer=False, is_writable=True),
            AccountMeta(destination, is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=True, is_writable=False),
        ],
    )


def create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey = WSOL_MINT) -> Instruction:
    """
    Create ``owner``'s associated token account for ``mint`` if it doesn't exist.

    :param payer: Pays the rent.
    :param owner: The account owner.
    :param mint: The token mint (wSOL by default).
    :return: The instruction.
    """
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        bytes([_CREATE_IDEMPOTENT]),
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(associated_token_address(owner, mint), is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ],
    )


def build_sweep(
    hot: Pubkey,
    cold: Pubkey,
    *,
    ata_exists: bool,
    to_cold: int,
    blockhash: str,
    usdc_mint: Pubkey | None = None,
) -> Sweep:
    """
    Build one atomic sweep: unwrap the hot wallet's wSOL, recreate its wSOL account, send SOL to cold.

    :param hot: The hot wallet (fee payer, owner of the token accounts).
    :param cold: The cold address, the only allowed transfer destination.
    :param ata_exists: Whether the hot wallet's wSOL account exists (otherwise there is nothing to close).
    :param to_cold: Lamports to send to the cold address; 0 to only unwrap (refilling the fee reserve).
    :param blockhash: A recent blockhash.
    :param usdc_mint: Also create the hot wallet's USDC account for this mint (when it is missing).
    :return: The unsigned sweep.
    """
    ata = associated_token_address(hot)
    instructions = []
    if ata_exists:
        instructions.append(close_account(ata, hot, hot))
    # Recreated in the same transaction, so a payment never meets a missing account.
    instructions.append(create_ata_idempotent(hot, hot))
    if usdc_mint is not None:
        instructions.append(create_ata_idempotent(hot, hot, usdc_mint))
    if to_cold > 0:
        instructions.append(transfer(TransferParams(from_pubkey=hot, to_pubkey=cold, lamports=to_cold)))
    message = MessageV0.try_compile(hot, instructions, [], Hash.from_string(blockhash))
    return Sweep(message=message, to_cold=to_cold, _token=_SWEEP)


def build_usdc_sweep(hot: Pubkey, cold: Pubkey, usdc_mint: Pubkey, *, amount: int, blockhash: str) -> Sweep:
    """
    Build one atomic USDC sweep: create the cold address's USDC account if missing, then move ``amount`` into it.

    The create is idempotent (a no-op once the account exists), so it is always included; when it does create the
    account, the hot wallet pays the rent.

    :param hot: The hot wallet (fee payer, owner of the source account).
    :param cold: The cold address, owner of the destination account.
    :param usdc_mint: The USDC mint.
    :param amount: USDC base units to send (more than 0).
    :param blockhash: A recent blockhash.
    :return: The unsigned sweep.
    :raises ValueError: If ``amount`` is not positive.
    """
    if amount <= 0:
        raise ValueError("a USDC sweep needs a positive amount")
    source, destination = associated_token_address(hot, usdc_mint), associated_token_address(cold, usdc_mint)
    instructions = [
        create_ata_idempotent(hot, cold, usdc_mint),
        transfer_checked(source, usdc_mint, destination, hot, amount),
    ]
    message = MessageV0.try_compile(hot, instructions, [], Hash.from_string(blockhash))
    return Sweep(message=message, to_cold=0, _token=_SWEEP, usdc_to_cold=amount)


def check_sweep(message: object, hot: Pubkey, cold: Pubkey, usdc_mint: Pubkey | None = None) -> None:  # noqa: C901
    """
    Validate a sweep's instructions independently of how it was built.

    Allowed, in order, either a SOL sweep: an optional ``CloseAccount`` of the hot wallet's wSOL account into the hot
    wallet, one ``CreateIdempotent`` of that account paid by the hot wallet, an optional ``CreateIdempotent`` of its
    USDC account (for ``usdc_mint`` only), and an optional System transfer from the hot wallet to the cold address;
    or a USDC sweep: an optional ``CreateIdempotent`` of the cold address's USDC account paid by the hot wallet, then
    one ``TransferChecked`` of a positive amount of ``usdc_mint`` from the hot wallet's USDC account into the cold
    address's. The hot wallet must be the fee payer and the only signer.

    :param message: The sweep message (anything but a v0 message without lookup tables is refused).
    :param hot: The hot wallet.
    :param cold: The cold address.
    :param usdc_mint: The USDC mint whose account the sweep may create.
    :raises PermissionError: If anything else appears.
    """
    if not isinstance(message, MessageV0) or message.address_table_lookups:
        raise PermissionError("sweep: unsupported message")
    keys = message.account_keys
    if message.header.num_required_signatures != 1 or keys[0] != hot:
        raise PermissionError("sweep: the hot wallet must be the only signer and the fee payer")
    ata = associated_token_address(hot)
    hot_usdc = cold_usdc = None
    if usdc_mint is not None:
        hot_usdc, cold_usdc = associated_token_address(hot, usdc_mint), associated_token_address(cold, usdc_mint)
    steps: list[str] = []
    for ix in message.instructions:
        program = keys[ix.program_id_index]
        accounts = [keys[i] for i in bytes(ix.accounts)]
        data = bytes(ix.data)
        if program == TOKEN_PROGRAM and data == bytes([_CLOSE_ACCOUNT]) and accounts == [ata, hot, hot]:
            steps.append("close")
        elif (
            program == ASSOCIATED_TOKEN_PROGRAM
            and data == bytes([_CREATE_IDEMPOTENT])
            and accounts == [hot, ata, hot, WSOL_MINT, SYSTEM_PROGRAM, TOKEN_PROGRAM]
        ):
            steps.append("create")
        elif (
            usdc_mint is not None
            and program == ASSOCIATED_TOKEN_PROGRAM
            and data == bytes([_CREATE_IDEMPOTENT])
            and accounts
            == [hot, associated_token_address(hot, usdc_mint), hot, usdc_mint, SYSTEM_PROGRAM, TOKEN_PROGRAM]
        ):
            steps.append("create_usdc")
        elif (
            usdc_mint is not None
            and program == ASSOCIATED_TOKEN_PROGRAM
            and data == bytes([_CREATE_IDEMPOTENT])
            and accounts == [hot, cold_usdc, cold, usdc_mint, SYSTEM_PROGRAM, TOKEN_PROGRAM]
        ):
            steps.append("create_cold_usdc")
        elif (
            usdc_mint is not None
            and program == TOKEN_PROGRAM
            and len(data) == 10
            and data[0] == _TRANSFER_CHECKED
            and int.from_bytes(data[1:9], "little") > 0
            and data[9] == USDC_DECIMALS
            and accounts == [hot_usdc, usdc_mint, cold_usdc, hot]
        ):
            steps.append("usdc_to_cold")
        elif (
            program == SYSTEM_PROGRAM
            and len(data) == 12
            and int.from_bytes(data[:4], "little") == _SYSTEM_TRANSFER
            and accounts == [hot, cold]
        ):
            steps.append("transfer")
        else:
            raise PermissionError("sweep: instruction not allowed")
    allowed = [
        [*close, "create", *usdc, *send]
        for close in ([], ["close"])
        for usdc in ([], ["create_usdc"])
        for send in ([], ["transfer"])
    ] + [["create_cold_usdc", "usdc_to_cold"], ["usdc_to_cold"]]
    if steps not in allowed:
        raise PermissionError("sweep: unexpected instruction sequence")


class HotWallet:
    """The hot key. It never leaves this class, and :meth:`sign` is the only code path that uses it."""

    def __init__(self, keypair: Keypair, cold: Pubkey, usdc_mint: Pubkey | None = None) -> None:
        """
        Wrap the hot keypair.

        :param keypair: The hot wallet keypair.
        :param cold: The cold address (sweep destination), which must be a different key.
        :param usdc_mint: The USDC mint the hot wallet accepts (its account may be created by a sweep).
        :raises ValueError: If the hot wallet is the cold address.
        """
        if keypair.pubkey() == cold:
            raise ValueError("the hot wallet must not be the cold address")
        self._keypair = keypair
        self.pubkey = keypair.pubkey()
        self.cold = cold
        self.usdc_mint = usdc_mint

    def __repr__(self) -> str:
        """Show the public key only."""
        return f"HotWallet({self.pubkey})"

    def sign(self, item: VerifiedPayment | Sweep) -> bytes:
        """
        Sign a verified payment (as fee payer only) or a sweep to the cold address; nothing else.

        :param item: A payment from :func:`~veridex.x402.payment.confirm_by_simulation`, or a sweep from
            :func:`build_sweep` or :func:`build_usdc_sweep`.
        :return: The fully signed, serialised transaction.
        :raises PermissionError: If the item is anything else, or fails the re-checks.
        """
        if is_verified(item):
            assert isinstance(item, VerifiedPayment)  # noqa: S101 (narrowing for mypy)
            message = item.transaction.message
            if not isinstance(message, Message | MessageV0):
                raise PermissionError("refusing to sign an unsupported message version")
            if message.account_keys[0] != self.pubkey or references(message, 0):
                raise PermissionError("refusing to sign: as a payment co-signer the hot wallet may only pay the fee")
            signature = self._keypair.sign_message(to_bytes_versioned(message))
            # Every client signature, not just the first: a payment may need several (a multisig member set).
            return bytes(VersionedTransaction.populate(message, [signature, *item.transaction.signatures[1:]]))
        if isinstance(item, Sweep):
            check_sweep(item.message, self.pubkey, self.cold, self.usdc_mint)
            return bytes(VersionedTransaction(item.message, [self._keypair]))
        raise PermissionError("refusing to sign: only verified payments and sweeps to cold")


def unexpected_outflows(  # noqa: C901
    tx: dict[str, Any], hot: Pubkey, cold: Pubkey, usdc_mint: Pubkey | None = None
) -> list[str]:
    """
    Find outgoing transfers from the hot wallet to anywhere but cold (or its own token accounts) in a transaction.

    Used on the hot wallet's recent signatures as a compromise signal: everything this service signs moves value only
    to the cold address (SOL sweeps), into the cold address's USDC account (USDC sweeps, including the rent when one
    creates that account), or into the hot wallet's own token accounts (creating them). A token transfer by the hot
    wallet is legitimate only as a ``transferChecked`` of USDC into the cold address's USDC account. Anything else is
    flagged: a swap (the product never swaps), approvals, authority changes and closing to a foreign account.

    A failed transaction moved nothing but its fee, and one the hot wallet didn't sign can't have moved its value
    (it is a key pair: it can only authorise as a header signer), so neither is an outflow.

    :param tx: A ``getTransaction`` result in ``jsonParsed`` encoding.
    :param hot: The hot wallet.
    :param cold: The cold address.
    :param usdc_mint: The USDC mint, if the hot wallet accepts USDC.
    :return: Human-readable descriptions of each unexpected outflow (empty if none).
    """
    hot_s, allowed = str(hot), {str(cold), str(associated_token_address(hot))}
    if (tx.get("meta") or {}).get("err") is not None:
        return []
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys") or []
    if not any(isinstance(k, dict) and k.get("pubkey") == hot_s and k.get("signer") for k in keys):
        return []
    cold_usdc = ""
    if usdc_mint is not None:
        cold_usdc = str(associated_token_address(cold, usdc_mint))
        # The cold address's USDC account receives rent when a USDC sweep creates it; it belongs to cold.
        allowed |= {str(associated_token_address(hot, usdc_mint)), cold_usdc}
    instructions = list(tx.get("transaction", {}).get("message", {}).get("instructions", []))
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(inner.get("instructions", []))
    found: list[str] = []
    for ix in instructions:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        kind, info = parsed.get("type", ""), parsed.get("info") or {}
        program = ix.get("program", "")
        if program == "system" and info.get("source") == hot_s:
            destination = info.get("destination") or info.get("newAccount") or ""
            if destination not in allowed:
                found.append(f"system {kind} to {destination}")
        elif program in {"spl-token", "spl-token-2022"}:
            authority = info.get("authority") or info.get("owner") or info.get("multisigAuthority")
            usdc_sweep = (
                kind == "transferChecked"
                and usdc_mint is not None
                and info.get("mint") == str(usdc_mint)
                and info.get("destination") == cold_usdc
            )
            if kind.startswith("transfer") and authority == hot_s and not usdc_sweep:
                found.append(f"token {kind} by the hot wallet to {info.get('destination', '')}")
            elif kind == "closeAccount" and authority == hot_s and info.get("destination") != hot_s:
                found.append(f"token closeAccount to {info.get('destination', '')}")
            elif kind in {"approve", "approveChecked", "setAuthority"} and authority == hot_s:
                found.append(f"token {kind} by the hot wallet")
    return found
