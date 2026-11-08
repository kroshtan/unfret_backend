"""
Swapping the hot wallet's USDC to SOL before a sweep, so the cold wallet only ever receives SOL.

Routes come from Jupiter's swap API (``GET {swap_api_url}/quote`` + ``POST {swap_api_url}/swap``; a setting, so it
can point at a self-hosted instance). The API is not trusted: :func:`verify_swap` simulates the transaction it returns
and only then mints a :class:`VerifiedSwap`, the third kind of thing :meth:`~veridex.x402.wallet.HotWallet.sign`
accepts. A swap is accepted only if, in the simulation:

* the hot wallet is the fee payer and the only signer, compute budget and priority fee are capped, no
  signature-verification precompiles, the size fits;
* the hot wallet's USDC account loses exactly the swapped amount, its wSOL account gains at least the minimum output
  (the worse of Jupiter's slippage bound and a floor from Pyth's SOL/USD price), and its SOL drops by no more than
  the fee;
* the hot wallet still owns both token accounts, with no delegate and no close authority set, and its own account is
  still a plain System account: nothing gave the hot wallet's authority away.

The output stays in the hot wallet's wSOL account; the regular sweep unwraps it and sends SOL to cold.
"""

import base64
from dataclasses import dataclass, field
from typing import Any

import httpx
from solders.message import Message, MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from veridex.x402.payment import (
    LAMPORTS_PER_SIGNATURE,
    MAX_TRANSACTION_BYTES,
    PRECOMPILES,
    PaymentError,
    compute_budget,
)
from veridex.x402.solana import WSOL_MINT, SolanaRpc, associated_token_address

SYSTEM_PROGRAM = "11111111111111111111111111111111"
_VERIFIED = object()  # only verify_swap can mint a VerifiedSwap


class SwapError(RuntimeError):
    """A swap route could not be obtained, or the transaction failed verification."""


@dataclass(frozen=True)
class VerifiedSwap:
    """A USDC→wSOL swap of the hot wallet that passed :func:`verify_swap`."""

    message: Message | MessageV0
    amount_in: int
    min_out: int
    _token: object = field(repr=False)

    def __post_init__(self) -> None:
        """Refuse construction anywhere but :func:`verify_swap`."""
        if self._token is not _VERIFIED:
            raise TypeError("VerifiedSwap can only be created by verify_swap")


def is_verified_swap(item: object) -> bool:
    """
    Whether ``item`` is a swap minted by :func:`verify_swap`.

    :param item: Anything.
    :return: ``True`` only for a genuine :class:`VerifiedSwap`.
    """
    return isinstance(item, VerifiedSwap) and item._token is _VERIFIED


class JupiterClient:
    """The two calls of Jupiter's swap API that a swap needs."""

    def __init__(self, base_url: str, api_key: str = "", transport: httpx.AsyncBaseTransport | None = None) -> None:
        """
        Configure the client.

        :param base_url: E.g. ``https://lite-api.jup.ag/swap/v1``.
        :param api_key: Sent as ``x-api-key`` when set.
        :param transport: Optional httpx transport (tests).
        """
        self.base_url, self._transport = base_url.rstrip("/"), transport
        self._headers = {"x-api-key": api_key} if api_key else {}

    async def quote(self, input_mint: Pubkey, output_mint: Pubkey, amount: int, slippage_bps: int) -> dict[str, Any]:
        """
        Ask for the best exact-in route.

        :param input_mint: What is sold.
        :param output_mint: What is bought.
        :param amount: How much is sold (base units).
        :param slippage_bps: The slippage the route may use.
        :return: Jupiter's quote response (``inAmount``, ``outAmount``, ``otherAmountThreshold``, …).
        :raises SwapError: On an API error.
        """
        params = {
            "inputMint": str(input_mint),
            "outputMint": str(output_mint),
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "swapMode": "ExactIn",
        }
        async with httpx.AsyncClient(timeout=20, transport=self._transport, headers=self._headers) as client:
            response = await client.get(f"{self.base_url}/quote", params=params)
        if not response.is_success:
            raise SwapError(f"quote failed: HTTP {response.status_code}")
        quote: dict[str, Any] = response.json()
        return quote

    async def transaction(
        self, quote: dict[str, Any], user: Pubkey, destination: Pubkey, max_priority_lamports: int
    ) -> bytes:
        """
        Have the route built into an unsigned transaction for ``user``, with the output into ``destination``.

        :param quote: A response from :meth:`quote`.
        :param user: The swapping wallet (fee payer and signer).
        :param destination: The token account that receives the output (the hot wallet's wSOL account).
        :param max_priority_lamports: The largest priority fee the transaction may set.
        :return: The serialised, unsigned transaction.
        :raises SwapError: On an API error or an unusable answer.
        """
        body = {
            "quoteResponse": quote,
            "userPublicKey": str(user),
            "wrapAndUnwrapSol": False,
            "useSharedAccounts": True,
            "destinationTokenAccount": str(destination),
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {"maxLamports": max_priority_lamports, "priorityLevel": "medium"}
            },
        }
        async with httpx.AsyncClient(timeout=20, transport=self._transport, headers=self._headers) as client:
            response = await client.post(f"{self.base_url}/swap", json=body)
        if not response.is_success:
            raise SwapError(f"swap build failed: HTTP {response.status_code}")
        try:
            return base64.b64decode(response.json()["swapTransaction"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise SwapError("swap build returned no transaction") from exc


def _token_state(account: dict[str, Any] | None, *, owner: Pubkey, mint: Pubkey) -> int:
    """
    The balance of one of the hot wallet's token accounts after the simulation, refusing any change of control.

    :param account: The account's simulated post-state (``jsonParsed``).
    :param owner: The hot wallet, which must still own it.
    :param mint: The account's mint.
    :return: The balance.
    :raises SwapError: If the account is gone, changed owner or mint, or got a delegate or close authority.
    """
    try:
        info = account["data"]["parsed"]["info"]  # type: ignore[index]
    except (KeyError, TypeError) as exc:
        raise SwapError("token account missing after swap") from exc
    if info.get("owner") != str(owner) or info.get("mint") != str(mint) or info.get("state") != "initialized":
        raise SwapError("token account changed hands")
    if info.get("delegate") or info.get("closeAuthority"):
        raise SwapError("swap would set a delegate or close authority")
    return int(info["tokenAmount"]["amount"])


async def verify_swap(  # noqa: C901, PLR0913
    raw_tx: bytes,
    *,
    rpc: SolanaRpc,
    hot: Pubkey,
    usdc_mint: Pubkey,
    amount_in: int,
    min_out: int,
    max_compute_units: int,
    max_priority_lamports: int,
) -> VerifiedSwap:
    """
    Check a swap transaction before the hot wallet signs it (see the module docstring for the rules).

    Call it with settlements and sweeps serialised (``X402`` holds the wallet lock), so the balance reads and the
    simulation see only this transaction's effect.

    :param raw_tx: The unsigned transaction from the swap API.
    :param rpc: The Solana RPC.
    :param hot: The hot wallet.
    :param usdc_mint: The USDC mint.
    :param amount_in: The USDC amount that must leave the hot wallet, exactly.
    :param min_out: The least wSOL the hot wallet must receive.
    :param max_compute_units: Largest accepted compute unit limit.
    :param max_priority_lamports: Largest accepted priority fee, in lamports.
    :return: The verified swap.
    :raises SwapError: If any rule is broken.
    """
    try:
        tx = VersionedTransaction.from_bytes(raw_tx)
    except ValueError as exc:
        raise SwapError("unreadable swap transaction") from exc
    message = tx.message
    if len(raw_tx) > MAX_TRANSACTION_BYTES or not isinstance(message, Message | MessageV0):
        raise SwapError("unsupported swap transaction")
    keys = message.account_keys
    if message.header.num_required_signatures != 1 or keys[0] != hot:
        raise SwapError("the hot wallet must be the fee payer and the only signer")
    if any(keys[ix.program_id_index] in PRECOMPILES for ix in message.instructions):
        raise SwapError("precompile not allowed")
    try:
        priority = compute_budget(message, max_compute_units, max_priority_lamports * 1_000_000)
    except PaymentError as exc:
        raise SwapError(f"compute budget: {exc.reason}") from exc
    if priority > max_priority_lamports:
        raise SwapError("priority fee too high")
    max_fee = LAMPORTS_PER_SIGNATURE + priority

    usdc_ata, wsol_ata = associated_token_address(hot, usdc_mint), associated_token_address(hot, WSOL_MINT)
    before_sol = await rpc.get_balance(hot)
    before_usdc, before_wsol = await rpc.token_balance(usdc_ata), await rpc.token_balance(wsol_ata)
    if before_usdc is None or before_wsol is None:
        raise SwapError("hot wallet token accounts not ready")
    error, (after_hot, after_usdc, after_wsol) = await rpc.simulate_accounts(raw_tx, [hot, usdc_ata, wsol_ata])
    if error is not None or after_hot is None:
        raise SwapError("swap simulation failed")
    if after_hot.get("owner") != SYSTEM_PROGRAM:
        raise SwapError("swap would reassign the hot wallet")
    if before_usdc - _token_state(after_usdc, owner=hot, mint=usdc_mint) != amount_in:
        raise SwapError("swap spends the wrong USDC amount")
    if _token_state(after_wsol, owner=hot, mint=WSOL_MINT) - before_wsol < min_out:
        raise SwapError("swap output below the minimum")
    if before_sol - int(after_hot["lamports"]) > max_fee:
        raise SwapError("swap costs the hot wallet more than the fee")
    return VerifiedSwap(message=message, amount_in=amount_in, min_out=min_out, _token=_VERIFIED)
