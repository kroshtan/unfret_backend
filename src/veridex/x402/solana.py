"""Solana constants, associated token account derivation and the few JSON-RPC calls x402 settlement needs."""

import asyncio
import base64
from typing import Any

import httpx
from solders.pubkey import Pubkey

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
COMPUTE_BUDGET_PROGRAM = Pubkey.from_string("ComputeBudget111111111111111111111111111111")
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
# Phantom and Solflare inject these wallet-protection assertions; the x402 spec requires sponsors to allow them.
LIGHTHOUSE_PROGRAM = Pubkey.from_string("L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95")

# Wrapped SOL: SOL held in an SPL token account, minted by the token program itself (no issuer).
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
WSOL_DECIMALS = 9

# CAIP-2 identifiers: "solana:" + the first 32 characters of the cluster's genesis hash.
CAIP2 = {
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "solana-mainnet": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
}


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


class RpcError(RuntimeError):
    """The Solana RPC returned an error, or the transaction failed on chain."""


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

    async def call(self, method: str, params: list[Any]) -> Any:
        """
        Run one JSON-RPC method and return its ``result``.

        :param method: The RPC method name.
        :param params: The positional parameters.
        :return: The ``result`` member of the response.
        :raises RpcError: If the RPC answers with an error object.
        """
        async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
            response = await client.post(
                self._url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            response.raise_for_status()
            body = response.json()
        if "error" in body:
            raise RpcError(f"{method}: {body['error'].get('message', body['error'])}")
        return body["result"]

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

    async def parsed_transaction(self, signature: str) -> dict[str, Any] | None:
        """
        Return a confirmed transaction in ``jsonParsed`` encoding.

        :param signature: The transaction signature.
        :return: The transaction, or ``None`` if not found.
        """
        result: dict[str, Any] | None = await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
        )
        return result

    async def simulate_accounts(
        self, raw_tx: bytes, addresses: list[Pubkey]
    ) -> tuple[Any, list[dict[str, Any] | None]]:
        """
        Simulate a transaction (no signature checks, its own blockhash) and return the post-state of some accounts.

        :param raw_tx: The serialised transaction.
        :param addresses: The accounts to report, in ``jsonParsed`` encoding.
        :return: The simulation error (``None`` on success) and the accounts' post-state (``None`` if absent).
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
                    "accounts": {"encoding": "jsonParsed", "addresses": [str(a) for a in addresses]},
                },
            ],
        )
        value = result["value"]
        return value.get("err"), list(value.get("accounts") or [None] * len(addresses))

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

        :param raw_tx: The serialised, fully signed transaction.
        :param timeout_s: How long to wait for confirmation.
        :return: The transaction signature (base58).
        :raises RpcError: If submission fails or the transaction errors on chain.
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
                    result = await self.call("getSignatureStatuses", [[signature]])
                    status = result["value"][0]
                    if status is not None:
                        if status.get("err") is not None:
                            raise RpcError(f"transaction failed: {status['err']}")
                        if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                            return signature
                    elif polls and polls % 3 == 0:
                        # Leaders drop transactions; resending the same signed bytes is idempotent.
                        await self.call("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": True}])
                    polls += 1
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise SettlementPendingError(signature) from exc
