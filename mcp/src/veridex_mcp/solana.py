"""Solana helpers for the agent side: instruction encoders and the few RPC calls paying needs."""

import asyncio
import base64
from typing import Any

import httpx
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
WSOL_MINT = Pubkey.from_string("So11111111111111111111111111111111111111112")
WSOL_DECIMALS = 9
# Circle's USDC on each network.
USDC_MINTS = {
    "solana-devnet": Pubkey.from_string("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"),
    "solana-mainnet": Pubkey.from_string("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
}
USDC_DECIMALS = 6
CAIP2 = {
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    "solana-mainnet": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
}


def associated_token_address(owner: Pubkey, mint: Pubkey = WSOL_MINT) -> Pubkey:
    """
    Derive ``owner``'s associated token account for ``mint`` (classic token program).

    :param owner: The wallet.
    :param mint: The token mint.
    :return: The associated token account.
    """
    return Pubkey.find_program_address([bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)], ASSOCIATED_TOKEN_PROGRAM)[0]


def transfer_checked(
    source: Pubkey, destination: Pubkey, authority: Pubkey, amount: int, mint: Pubkey = WSOL_MINT
) -> Instruction:
    """
    Build an SPL ``TransferChecked`` of ``mint`` (wSOL by default, or USDC).

    :param source: The paying token account.
    :param destination: The receiving token account.
    :param authority: The owner of ``source`` (signs).
    :param amount: Base units of the token.
    :param mint: The token mint.
    :return: The instruction.
    """
    places = WSOL_DECIMALS if mint == WSOL_MINT else USDC_DECIMALS
    data = bytes([12]) + amount.to_bytes(8, "little") + bytes([places])
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


def memo(text: str) -> Instruction:
    """
    Build a Memo instruction (x402 uses a random nonce so identical payments stay distinct).

    :param text: The memo text.
    :return: The instruction.
    """
    return Instruction(MEMO_PROGRAM, text.encode(), [])


def create_ata_idempotent(payer: Pubkey, owner: Pubkey, mint: Pubkey = WSOL_MINT) -> Instruction:
    """
    Create ``owner``'s associated token account if it doesn't exist yet.

    :param payer: Pays the rent.
    :param owner: The wallet the account belongs to.
    :param mint: The token mint.
    :return: The instruction.
    """
    return Instruction(
        ASSOCIATED_TOKEN_PROGRAM,
        bytes([1]),
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(associated_token_address(owner, mint), is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ],
    )


def sync_native(account: Pubkey) -> Instruction:
    """
    Update a wSOL account's token balance after SOL was sent to it.

    :param account: The wSOL token account.
    :return: The instruction.
    """
    return Instruction(TOKEN_PROGRAM, bytes([17]), [AccountMeta(account, is_signer=False, is_writable=True)])


class RpcError(RuntimeError):
    """The RPC returned an error or a transaction failed."""


class Rpc:
    """Minimal async Solana JSON-RPC client."""

    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """
        Create a client.

        :param url: The RPC endpoint.
        :param transport: Optional httpx transport (tests).
        """
        self.url = url
        self._transport = transport

    async def call(self, method: str, params: list[Any]) -> Any:
        """
        Run one JSON-RPC method.

        :param method: The method.
        :param params: Its parameters.
        :return: The ``result``.
        :raises RpcError: On an RPC error.
        """
        async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
            response = await client.post(
                self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            )
            response.raise_for_status()
            body = response.json()
        if "error" in body:
            raise RpcError(f"{method}: {body['error'].get('message', body['error'])}")
        return body["result"]

    async def latest_blockhash(self) -> str:
        """
        Return a recent blockhash.

        :return: The blockhash (base58).
        """
        result = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return str(result["value"]["blockhash"])

    async def balance(self, address: Pubkey) -> int:
        """
        Return a wallet's SOL balance.

        :param address: The wallet.
        :return: Lamports.
        """
        return int((await self.call("getBalance", [str(address), {"commitment": "confirmed"}]))["value"])

    async def token_balance(self, account: Pubkey) -> int:
        """
        Return a token account's balance, 0 if it doesn't exist.

        :param account: The token account.
        :return: Atomic units.
        """
        try:
            result = await self.call("getTokenAccountBalance", [str(account), {"commitment": "confirmed"}])
        except RpcError:
            return 0
        return int(result["value"]["amount"])

    async def send_and_confirm(self, raw_tx: bytes, timeout_s: float = 60) -> str:
        """
        Submit a signed transaction and wait for confirmation.

        :param raw_tx: The serialised transaction.
        :param timeout_s: How long to wait.
        :return: The signature.
        :raises RpcError: If it fails or isn't confirmed in time.
        """
        signature: str = await self.call(
            "sendTransaction",
            [base64.b64encode(raw_tx).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}],
        )
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    status = (await self.call("getSignatureStatuses", [[signature]]))["value"][0]
                    if status is not None:
                        if status.get("err") is not None:
                            raise RpcError(f"transaction failed: {status['err']}")
                        if status.get("confirmationStatus") in {"confirmed", "finalized"}:
                            return signature
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise RpcError(f"transaction {signature} not confirmed in time") from exc
