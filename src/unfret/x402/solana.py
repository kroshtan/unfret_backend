"""Solana constants, associated token account derivation and the few JSON-RPC calls x402 settlement needs."""

import asyncio
import base64
import re
from typing import Any

import httpx
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
# Phantom and Solflare inject these wallet-protection assertions; the x402 spec requires sponsors to allow them.
LIGHTHOUSE_PROGRAM = Pubkey.from_string("L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
MEMO_V1_PROGRAM = Pubkey.from_string("Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo")
SQUADS_V3_PROGRAM = Pubkey.from_string("SMPLecH534NA9acpos4G6x7uf3LWbCAwZQE9e8ZekMu")
SQUADS_V4_PROGRAM = Pubkey.from_string("SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf")

# Wrapped SOL: SOL held in an SPL token account, minted by the token program itself (no issuer).
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
WSOL_DECIMALS = 9

# Circle's USDC: mainnet (https://www.circle.com/multi-chain-usdc/solana) and devnet (Circle's developer quickstart).
USDC_MINTS = {
    "solana-mainnet": Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
    "solana-devnet": Pubkey.from_string("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"),
}
USDC_DECIMALS = 6


def usdc_mint(network: str, override: str = "") -> Pubkey:
    """
    The USDC mint for a network, or an explicit override.

    :param network: ``solana-devnet`` or ``solana-mainnet``.
    :param override: A mint address that takes precedence (empty for Circle's).
    :return: The mint.
    """
    return Pubkey.from_string(override) if override else USDC_MINTS[network]


def transfer_checked(source: Pubkey, mint: Pubkey, destination: Pubkey, authority: Pubkey, amount: int) -> Instruction:
    """
    SPL ``TransferChecked`` between two token accounts of ``mint``.

    :param source: The paying token account.
    :param mint: The token mint.
    :param destination: The receiving token account.
    :param authority: The owner of ``source`` (signs).
    :param amount: Base units.
    :return: The instruction.
    """
    data = bytes([12]) + amount.to_bytes(8, "little") + bytes([decimals(mint)])
    return Instruction(
        TOKEN_PROGRAM,
        data,
        [
            AccountMeta(source, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(destination, is_signer=False, is_writable=True),
            AccountMeta(authority, is_signer=True, is_writable=False),
        ],
    )


def decimals(mint: Pubkey) -> int:
    """
    The decimals of the mints Unfret accepts.

    :param mint: wSOL or a USDC mint.
    :return: 9 for wSOL, 6 otherwise.
    """
    return WSOL_DECIMALS if mint == WSOL_MINT else USDC_DECIMALS


# CAIP-2 identifiers: "solana:" + the first 32 characters of the cluster's genesis hash.
CAIP2 = {
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "solana-mainnet": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
}


# SPL Token program errors (spl_token::error::TokenError) a payer can act on, by custom error code.
_TOKEN_ERRORS = {
    0: "not_rent_exempt",
    1: "insufficient_funds",
    2: "invalid_mint",
    3: "mint_mismatch",
    4: "owner_mismatch",
    9: "source_token_account_missing",  # UninitializedState
    17: "account_frozen",
    18: "mint_decimals_mismatch",
}
# What the token program answers when an account it reads holds no token account (it doesn't exist).
_MISSING_ACCOUNT = frozenset({"InvalidAccountData", "UninitializedAccount"})
_FAILED = re.compile(r"^Program (\w+) failed: (.*)$")
_CUSTOM = re.compile(r"custom program error: 0x([0-9a-fA-F]+)")
_INSTRUCTION_ERROR_PARTS = 2  # [instruction index, error]


def _snake(name: str) -> str:
    """
    ``InsufficientFundsForRent`` → ``insufficient_funds_for_rent``, keeping only ``[a-z0-9_]`` (at most 64 chars).

    :param name: A Solana error variant name.
    :return: Its snake-case form.
    """
    return re.sub(r"[^a-z0-9_]", "", re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower())[:64] or "unknown"


def simulation_failure_reason(error: Any, logs: list[str]) -> str:
    """
    A short, safe reason for a failed payment simulation that the payer can act on.

    Built only from error variant names and the failing program's error code (about the client's own transaction,
    nothing of ours): ``insufficient_funds``, ``source_token_account_missing``, ``mint_mismatch``,
    ``account_not_found``, ``blockhash_not_found``, …; otherwise the error class in snake case (for example
    ``invalid_account_data``), or ``custom_program_error_<code>`` for another program's own error.

    :param error: The ``err`` of ``simulateTransaction`` (a string, or a one-key object such as ``InstructionError``).
    :param logs: The simulation's program logs.
    :return: The reason.
    """
    if any("insufficient funds" in line.lower() or "insufficient lamports" in line.lower() for line in logs):
        return "insufficient_funds"
    if isinstance(error, str):
        return _snake(error)
    if not isinstance(error, dict) or len(error) != 1:
        return "unknown"
    kind, value = next(iter(error.items()))
    if kind != "InstructionError":
        return _snake(str(kind))
    return _instruction_error_reason(
        value[1] if isinstance(value, list) and len(value) == _INSTRUCTION_ERROR_PARTS else None, logs
    )


def _instruction_error_reason(cause: Any, logs: list[str]) -> str:
    """
    The reason for an ``InstructionError``, read with the failing program from the logs.

    :param cause: The instruction's error (a class name, or ``{"Custom": code}``).
    :param logs: The simulation's program logs.
    :return: The reason.
    """
    failing = next((m for line in logs if (m := _FAILED.match(line))), None)  # the innermost failing program
    program = failing.group(1) if failing else None
    code = cause.get("Custom") if isinstance(cause, dict) else None
    if code is None and failing and (custom := _CUSTOM.search(failing.group(2))):
        code = int(custom.group(1), 16)
    if isinstance(code, int):
        if program == str(TOKEN_PROGRAM) and code in _TOKEN_ERRORS:
            return _TOKEN_ERRORS[code]
        if program == str(SYSTEM_PROGRAM_ID) and code == 1:  # SystemError::ResultWithNegativeLamports
            return "insufficient_funds"
        return f"custom_program_error_{code}"
    if isinstance(cause, str):
        if cause in _MISSING_ACCOUNT and program == str(TOKEN_PROGRAM):
            return "source_token_account_missing"
        return _snake(cause)
    return "instruction_error"


def associated_token_address(owner: Pubkey, mint: Pubkey = WSOL_MINT) -> Pubkey:
    """
    Derive the associated token account of ``owner`` for ``mint`` under the classic token program.

    :param owner: The wallet that owns the token account.
    :param mint: The token mint.
    :return: The associated token account address.
    """
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]


_POLL_S = 0.4  # about one slot


class RpcError(RuntimeError):
    """The Solana RPC returned an error, or the transaction failed on chain."""


class TransactionFailedError(RpcError):
    """The transaction landed with an error: its fee was paid, nothing else happened."""


class SettlementPendingError(RpcError):
    """A submitted transaction was not confirmed in time; it may still land."""

    def __init__(self, signature: str) -> None:
        """
        Remember the signature so the client can look the transaction up.

        :param signature: The submitted transaction's signature (base58).
        """
        super().__init__(f"transaction {signature} not confirmed in time")
        self.signature = signature


class SolanaRpc:
    """Minimal async JSON-RPC client for the calls settlement needs."""

    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """
        Create a client for one RPC endpoint.

        :param url: The RPC endpoint.
        :param transport: Optional httpx transport (used to stub the network in tests).
        """
        self._url = url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None  # one connection pool, opened on first use

    async def aclose(self) -> None:
        """Close the connection pool (on shutdown)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, params: list[Any]) -> Any:
        """
        Run one JSON-RPC method and return its ``result``.

        :param method: The RPC method name.
        :param params: The positional parameters.
        :return: The ``result`` member of the response.
        :raises RpcError: If the RPC answers with an error object, or can't be reached or understood.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15, transport=self._transport)
        try:
            response = await self._client.post(
                self._url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RpcError(f"{method}: {type(exc).__name__}") from exc
        if not isinstance(body, dict):
            raise RpcError(f"{method}: unexpected response")
        if "error" in body:
            raise RpcError(f"{method}: {body['error'].get('message', body['error'])}")
        return body["result"]

    async def signature_status(self, signature: str) -> dict[str, Any] | None:
        """
        A transaction's status, searching history too (``None`` while the network doesn't know it).

        :param signature: The transaction signature.
        :return: The status (``err``, ``confirmationStatus``, …) or ``None``.
        """
        result = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        status: dict[str, Any] | None = result["value"][0]
        return status

    async def balances(self, owner: Pubkey, token_accounts: list[Pubkey]) -> tuple[int, list[int | None]]:
        """
        A wallet's SOL and some token accounts' balances in one ``getMultipleAccounts`` call (one slot, one trip).

        :param owner: The wallet.
        :param token_accounts: Token accounts to read.
        :return: The wallet's lamports (0 if it doesn't exist) and each token amount (``None`` if the account doesn't
            exist).
        """
        result = await self.call(
            "getMultipleAccounts",
            [[str(owner), *(str(a) for a in token_accounts)], {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        first, *tokens = result["value"]
        amounts = [
            None if account is None else int(account["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            for account in tokens
        ]
        return (0 if first is None else int(first["lamports"])), amounts

    async def get_balance(self, address: Pubkey) -> int:
        """
        Return an account's balance in lamports.

        :param address: The account.
        :return: Lamports held.
        """
        result = await self.call("getBalance", [str(address), {"commitment": "confirmed"}])
        return int(result["value"])

    async def token_balance(self, account: Pubkey) -> int | None:
        """
        Return a token account's balance in atomic units, or ``None`` if the account doesn't exist.

        :param account: The token account.
        :return: The balance, or ``None``.
        """
        result = await self.call(
            "getAccountInfo", [str(account), {"encoding": "jsonParsed", "commitment": "confirmed"}]
        )
        if result["value"] is None:
            return None
        return int(result["value"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])

    async def rent_exempt_minimum(self, size: int) -> int:
        """
        Return the lamports an account of ``size`` bytes needs to be rent-exempt.

        :param size: The account data size.
        :return: Lamports.
        """
        return int(await self.call("getMinimumBalanceForRentExemption", [size]))

    async def latest_blockhash(self) -> str:
        """
        Return a recent blockhash.

        :return: The blockhash (base58).
        """
        result = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return str(result["value"]["blockhash"])

    async def recent_signatures(self, address: Pubkey, limit: int = 20) -> list[str]:
        """
        Return the most recent transaction signatures involving ``address``, newest first.

        :param address: The account.
        :param limit: How many.
        :return: Signatures (base58).
        """
        result = await self.call(
            "getSignaturesForAddress", [str(address), {"limit": limit, "commitment": "confirmed"}]
        )
        return [entry["signature"] for entry in result]

    async def signature_page(
        self,
        address: Pubkey,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "finalized",
    ) -> list[dict[str, Any]]:
        """
        Return one page of signatures involving ``address``, newest first (``getSignaturesForAddress``).

        :param address: The account.
        :param before: Start below this signature (paging).
        :param until: Stop at this signature (exclusive).
        :param limit: Page size, at most 1000.
        :param commitment: ``confirmed`` or ``finalized``.
        :return: Entries with ``signature``, ``err``, ``blockTime``, …
        """
        options: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if before:
            options["before"] = before
        if until:
            options["until"] = until
        result: list[dict[str, Any]] = await self.call("getSignaturesForAddress", [str(address), options])
        return result

    async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
        """
        Return a transaction in ``jsonParsed`` encoding.

        :param signature: The transaction signature.
        :param commitment: ``confirmed`` or ``finalized``.
        :return: The transaction, or ``None`` if not found.
        """
        result: dict[str, Any] | None = await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": commitment, "maxSupportedTransactionVersion": 0}],
        )
        return result

    async def simulate_accounts(
        self, raw_tx: bytes, addresses: list[Pubkey]
    ) -> tuple[Any, list[dict[str, Any] | None], list[int], list[str]]:
        """
        Simulate a transaction (no signature checks, its own blockhash) and return the post-state of some accounts.

        :param raw_tx: The serialised transaction.
        :param addresses: The accounts to report, in ``jsonParsed`` encoding.
        :return: The simulation error (``None`` on success), the accounts' post-state (``None`` if absent), the
            account indices of every program the transaction invoked by CPI, and the program logs.
        """
        result = await self.call(
            "simulateTransaction",
            [
                base64.b64encode(raw_tx).decode(),
                {
                    "encoding": "base64",
                    "sigVerify": False,
                    "replaceRecentBlockhash": False,
                    "commitment": "confirmed",
                    "innerInstructions": True,
                    "accounts": {"encoding": "jsonParsed", "addresses": [str(a) for a in addresses]},
                },
            ],
        )
        value = result["value"]
        inner = [
            ix["programIdIndex"] for group in value.get("innerInstructions") or [] for ix in group["instructions"]
        ]
        logs = [str(line) for line in value.get("logs") or []]
        return value.get("err"), list(value.get("accounts") or [None] * len(addresses)), inner, logs

    async def simulate(self, raw_tx: bytes) -> None:
        """
        Simulate a transaction without signature checks, so doomed payments cost no fees.

        :param raw_tx: The serialised transaction.
        :raises RpcError: If the simulation fails.
        """
        result = await self.call(
            "simulateTransaction",
            [base64.b64encode(raw_tx).decode(), {"encoding": "base64", "sigVerify": False, "commitment": "confirmed"}],
        )
        if result["value"].get("err") is not None:
            raise RpcError(f"simulation failed: {result['value']['err']}")

    async def send_and_confirm(self, raw_tx: bytes, timeout_s: float) -> str:
        """
        Submit a fully signed transaction and wait until it is confirmed without error.

        A transaction the RPC won't accept raises ``RpcError`` from :meth:`call`.

        :param raw_tx: The serialised, fully signed transaction.
        :param timeout_s: How long to wait for confirmation.
        :return: The transaction signature (base58).
        :raises TransactionFailedError: If the transaction errors on chain (its fee was paid).
        :raises SettlementPendingError: If it was submitted but not confirmed within ``timeout_s``.
        """
        encoded = base64.b64encode(raw_tx).decode()
        signature: str = await self.call(
            "sendTransaction", [encoded, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]
        )
        try:
            async with asyncio.timeout(timeout_s):
                polls = 0
                while True:
                    try:
                        status = await self.signature_status(signature)
                    except RpcError:
                        status = None  # a hiccup while polling isn't an outcome: keep asking until the timeout
                    if status is not None:
                        if status.get("err") is not None:
                            raise TransactionFailedError(f"transaction failed: {status['err']}")
                        if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                            return signature
                    elif polls and polls % 8 == 0:
                        # Leaders drop transactions; resending the same signed bytes is idempotent.
                        await self.call("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": True}])
                    polls += 1
                    await asyncio.sleep(_POLL_S)
        except TimeoutError as exc:
            raise SettlementPendingError(signature) from exc
