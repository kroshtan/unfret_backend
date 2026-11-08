"""
The hot wallet: receives x402 payments (its wSOL and USDC accounts), pays their fees, and sweeps SOL to cold.

Every use of its key goes through one function, :meth:`HotWallet.sign`, which accepts exactly three things:

* a :class:`~veridex.x402.payment.VerifiedPayment`, co-signed only as fee payer: the hot wallet may not appear in
  any instruction, so its signature can only pay the network fee;
* a :class:`Sweep`, re-validated here instruction by instruction: optionally close the hot wallet's wSOL account
  back into the hot wallet (unwrapping), recreate that account, create its USDC account if missing, and optionally
  transfer SOL to the cold address. Nothing else, and no other destination;
* a :class:`~veridex.x402.swap.VerifiedSwap`: USDC to wSOL into the hot wallet's own wSOL account, checked by
  simulation in :func:`~veridex.x402.swap.verify_swap` (the cold wallet only ever receives SOL).

Credit purchases also pay into the hot wallet; their scanner (veridex.credits.scanner) counts only INCOMING transfers,
ignoring the sweeps, swaps and fee payments this module signs.
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
from veridex.x402.solana import ASSOCIATED_TOKEN_PROGRAM, TOKEN_PROGRAM, WSOL_MINT, associated_token_address
from veridex.x402.swap import VerifiedSwap, is_verified_swap

SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
_CLOSE_ACCOUNT = 9
_CREATE_IDEMPOTENT = 1
_SYSTEM_TRANSFER = 2


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
    except Exception:
        # Deliberately unchained and without detail: parsing errors can echo part of the secret.
        raise ValueError("invalid keypair") from None


_SWEEP = object()  # only build_sweep can mint a Sweep


@dataclass(frozen=True)
class Sweep:
    """An unsigned sweep transaction from :func:`build_sweep`."""

    message: MessageV0
    to_cold: int
    _token: object = field(repr=False)

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


def check_sweep(message: object, hot: Pubkey, cold: Pubkey, usdc_mint: Pubkey | None = None) -> None:  # noqa: C901
    """
    Validate a sweep's instructions independently of how it was built.

    Allowed, in order: an optional ``CloseAccount`` of the hot wallet's wSOL account into the hot wallet, one
    ``CreateIdempotent`` of that account paid by the hot wallet, an optional ``CreateIdempotent`` of its USDC account
    (for ``usdc_mint`` only), and an optional System transfer from the hot wallet to the cold address. The hot wallet
    must be the fee payer and the only signer.

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
            program == SYSTEM_PROGRAM
            and len(data) == 12  # noqa: PLR2004
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
    ]
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

    def sign(self, item: VerifiedPayment | Sweep | VerifiedSwap) -> bytes:
        """
        Sign a verified payment (as fee payer only), a sweep to the cold address, or a verified swap; nothing else.

        :param item: A payment from :func:`~veridex.x402.payment.confirm_by_simulation`, a sweep from
            :func:`build_sweep`, or a swap from :func:`~veridex.x402.swap.verify_swap`.
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
            return bytes(VersionedTransaction.populate(message, [signature, item.transaction.signatures[1]]))
        if isinstance(item, Sweep):
            check_sweep(item.message, self.pubkey, self.cold, self.usdc_mint)
            return bytes(VersionedTransaction(item.message, [self._keypair]))
        if is_verified_swap(item):
            assert isinstance(item, VerifiedSwap)  # noqa: S101 (narrowing for mypy)
            message = item.message
            if message.header.num_required_signatures != 1 or message.account_keys[0] != self.pubkey:
                raise PermissionError("refusing to sign: a swap must have the hot wallet as its only signer")
            return bytes(VersionedTransaction(message, [self._keypair]))
        raise PermissionError("refusing to sign: only verified payments, sweeps to cold and verified swaps")


def _token_deltas(tx: dict[str, Any], owner: str) -> dict[str, int]:
    """
    Net token balance change per mint across the token accounts ``owner`` holds in a transaction.

    :param tx: A ``getTransaction`` result in ``jsonParsed`` encoding.
    :param owner: The owner (base58).
    :return: Change per mint (base58), in base units.
    """
    meta = tx.get("meta") or {}
    deltas: dict[str, int] = {}
    for sign, key in ((-1, "preTokenBalances"), (1, "postTokenBalances")):
        for entry in meta.get(key) or []:
            if entry.get("owner") == owner:
                mint = str(entry.get("mint", ""))
                deltas[mint] = deltas.get(mint, 0) + sign * int(entry["uiTokenAmount"]["amount"])
    return deltas


def is_swap_into_sol(
    tx: dict[str, Any], hot: Pubkey, usdc_mint: Pubkey, min_lamports_per_usdc: float | None = None
) -> bool:
    """
    Whether a transaction's net effect on the hot wallet is a USDC→SOL swap: USDC down, wSOL up, nothing else lost.

    The hot wallet paid only the fee (its SOL dropped by exactly ``meta.fee``), every other token it holds is
    unchanged, and, given a rate floor, it got at least ``min_lamports_per_usdc`` per USDC base unit.

    :param tx: A ``getTransaction`` result in ``jsonParsed`` encoding.
    :param hot: The hot wallet.
    :param usdc_mint: The USDC mint.
    :param min_lamports_per_usdc: Optional sanity floor on the rate (e.g. half the oracle rate).
    :return: ``True`` for such a swap.
    """
    meta = tx.get("meta") or {}
    message = tx.get("transaction", {}).get("message", {})
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in message.get("accountKeys", [])]
    if meta.get("err") is not None or not keys or keys[0] != str(hot):
        return False
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if not pre or not post or pre[0] - post[0] != int(meta.get("fee", 0)):
        return False
    deltas = _token_deltas(tx, str(hot))
    spent, got = -deltas.pop(str(usdc_mint), 0), deltas.pop(str(WSOL_MINT), 0)
    if spent <= 0 or got <= 0 or any(change < 0 for change in deltas.values()):
        return False
    return min_lamports_per_usdc is None or got >= spent * min_lamports_per_usdc


def unexpected_outflows(  # noqa: C901
    tx: dict[str, Any],
    hot: Pubkey,
    cold: Pubkey,
    usdc_mint: Pubkey | None = None,
    min_lamports_per_usdc: float | None = None,
) -> list[str]:
    """
    Find outgoing transfers from the hot wallet to anywhere but cold (or its own token accounts) in a transaction.

    Used on the hot wallet's recent signatures as a compromise signal: everything this service signs moves value only
    to the cold address (sweeps), into the hot wallet's own token accounts (creating them), or, in a swap, USDC out
    for more SOL in. A token transfer by the hot wallet is legitimate only inside a transaction whose net effect is
    such a swap (:func:`is_swap_into_sol`); approvals, authority changes and closing to a foreign account never are.

    A failed transaction moved nothing but its fee, and one the hot wallet didn't sign can't have moved its value
    (it is a key pair: it can only authorise as a header signer), so neither is an outflow.

    :param tx: A ``getTransaction`` result in ``jsonParsed`` encoding.
    :param hot: The hot wallet.
    :param cold: The cold address.
    :param usdc_mint: The USDC mint, if the hot wallet accepts USDC.
    :param min_lamports_per_usdc: Optional rate floor for recognising swaps.
    :return: Human-readable descriptions of each unexpected outflow (empty if none).
    """
    hot_s, allowed = str(hot), {str(cold), str(associated_token_address(hot))}
    if (tx.get("meta") or {}).get("err") is not None:
        return []
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys") or []
    if not any(isinstance(k, dict) and k.get("pubkey") == hot_s and k.get("signer") for k in keys):
        return []
    if usdc_mint is not None:
        allowed.add(str(associated_token_address(hot, usdc_mint)))
    swap = usdc_mint is not None and is_swap_into_sol(tx, hot, usdc_mint, min_lamports_per_usdc)
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
            if kind.startswith("transfer") and authority == hot_s and not swap:
                found.append(f"token {kind} by the hot wallet to {info.get('destination', '')}")
            elif kind == "closeAccount" and authority == hot_s and info.get("destination") != hot_s:
                found.append(f"token closeAccount to {info.get('destination', '')}")
            elif kind in {"approve", "approveChecked", "setAuthority"} and authority == hot_s:
                found.append(f"token {kind} by the hot wallet")
    return found
