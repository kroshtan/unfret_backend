"""
Base (EVM) constants, address checks and the few JSON-RPC calls x402 on Base needs.

Payments: the payer signs an EIP-3009 ``transferWithAuthorization`` for USDC, the facilitator submits it and pays the
gas. The only transaction Unfret ever signs on Base is a sweep of the hot wallet's USDC to the cold address
(``unfret.x402.evm_wallet``), paid for with the hot wallet's own ETH. The RPC here reads a settled transaction's
receipt (to check what really moved), an authorization's state, the hot wallet's USDC, ETH, nonce and code, the logs
of its outgoing transfers and approvals (the outflow audit) and the network's fees, and broadcasts signed sweeps.
"""

import re
from typing import Any, TypeGuard

import httpx

from unfret.x402.solana import RpcError

# CAIP-2 identifiers (eip155:<chain id>), as x402 v2 names networks.
CAIP2 = {"base-sepolia": "eip155:84532", "base-mainnet": "eip155:8453"}
CHAIN_IDS = {"base-sepolia": 84532, "base-mainnet": 8453}
# Public RPC endpoints (rate-limited; production should set its own, base_rpc_url).
PUBLIC_RPC = {"base-sepolia": "https://sepolia.base.org", "base-mainnet": "https://mainnet.base.org"}
# Circle's USDC on Base (https://developers.circle.com/stablecoins/usdc-contract-addresses), with the EIP-712 domain
# its transferWithAuthorization signatures use: name() and version() as each contract answers them (checked on chain
# 2026-11-17: "USD Coin" on mainnet, "USDC" on Sepolia; version "2" on both).
USDC = {
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base-mainnet": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}
USDC_DOMAIN = {"base-sepolia": ("USDC", "2"), "base-mainnet": ("USD Coin", "2")}
USDC_DECIMALS = 6

# Event topics and function selectors (keccak-256 of the signatures; tests recompute them).
# Transfer(address,address,uint256), Approval(address,address,uint256)
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"  # AuthorizationUsed(address,bytes32)
)
AUTHORIZATION_STATE_SELECTOR = "0xe94a0102"  # authorizationState(address,bytes32)
BALANCE_OF_SELECTOR = "0x70a08231"  # balanceOf(address)
TRANSFER_SELECTOR = "0xa9059cbb"  # transfer(address,uint256)

_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32 = re.compile(r"^0x[0-9a-fA-F]{64}$")
_MASK = (1 << 64) - 1
_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)  # fmt: skip
_ROTATIONS = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))
_RATE = 136  # bytes absorbed per permutation for a 256-bit output


def _rotl(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK if shift else value


def _keccak_f(lanes: list[int]) -> list[int]:
    for constant in _ROUND_CONSTANTS:
        column = [lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20] for x in range(5)]
        theta = [column[(x - 1) % 5] ^ _rotl(column[(x + 1) % 5], 1) for x in range(5)]
        lanes = [lane ^ theta[i % 5] for i, lane in enumerate(lanes)]
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(lanes[x + 5 * y], _ROTATIONS[x][y])
        lanes = [
            moved[i] ^ (~moved[(i % 5 + 1) % 5 + 5 * (i // 5)] & _MASK & moved[(i % 5 + 2) % 5 + 5 * (i // 5)])
            for i in range(25)
        ]
        lanes[0] ^= constant
    return lanes


def keccak256(data: bytes) -> bytes:
    """
    Ethereum's Keccak-256 (the original Keccak padding, not NIST SHA3-256).

    Only used for EIP-55 address checksums, so a plain implementation is fast enough.

    :param data: The input.
    :return: The 32-byte digest.
    """
    padded = bytearray(data) + b"\x01"
    padded += bytes(-len(padded) % _RATE)
    padded[-1] |= 0x80
    lanes = [0] * 25
    for start in range(0, len(padded), _RATE):
        block = padded[start : start + _RATE]
        for i in range(_RATE // 8):
            lanes[i] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        lanes = _keccak_f(lanes)
    return b"".join(lane.to_bytes(8, "little") for lane in lanes[:4])


def checksum_address(address: str) -> str:
    """
    The EIP-55 mixed-case form of an address.

    :param address: ``0x`` and 40 hex digits, any case.
    :return: The checksummed address.
    :raises ValueError: If it isn't an address.
    """
    if not _ADDRESS.match(address):
        raise ValueError("not an EVM address")
    plain = address[2:].lower()
    digest = keccak256(plain.encode()).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(plain))


def parse_address(address: str) -> str:
    """
    Validate a configured address and return its checksummed form.

    All-lowercase (or all-uppercase) addresses carry no checksum and are accepted; a mixed-case one must be a valid
    EIP-55 checksum, so a typo in a payout address is caught at start-up rather than paid into.

    :param address: The address as configured.
    :return: The checksummed address.
    :raises ValueError: If it isn't an address, is the zero address, or its checksum is wrong.
    """
    address = address.strip()
    checksummed = checksum_address(address)
    body = address[2:]
    if body != body.lower() and body != body.upper() and address != checksummed:
        raise ValueError("EVM address checksum mismatch")
    if int(body, 16) == 0:
        raise ValueError("the zero address")
    return checksummed


def is_address(value: Any) -> TypeGuard[str]:
    """
    Whether ``value`` is an EVM address string (any case; no checksum check).

    :param value: Anything.
    :return: ``True`` for ``0x`` and 40 hex digits.
    """
    return isinstance(value, str) and bool(_ADDRESS.match(value))


def is_bytes32(value: Any) -> TypeGuard[str]:
    """
    Whether ``value`` is a ``0x``-prefixed 32-byte hex string.

    :param value: Anything.
    :return: ``True`` if so.
    """
    return isinstance(value, str) and bool(_BYTES32.match(value))


def topic_address(address: str) -> str:
    """
    An address as an indexed event topic (left-padded to 32 bytes, lowercase).

    :param address: The address.
    :return: The topic.
    """
    return "0x" + address[2:].lower().rjust(64, "0")


def address_from_topic(topic: str) -> str:
    """
    The address in an indexed event topic, lowercase.

    :param topic: A 32-byte topic.
    :return: ``0x`` and the low 20 bytes.
    """
    return "0x" + topic[-40:].lower()


def word(value: str) -> int:
    """
    A hex quantity or 32-byte word as an integer (``0x`` alone is 0).

    :param value: The hex string.
    :return: The integer.
    """
    return int(value, 16) if value not in {"0x", ""} else 0


class BaseRpc:
    """Minimal async Ethereum JSON-RPC client for the reads x402 on Base needs."""

    def __init__(self, url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """
        Create a client for one RPC endpoint.

        :param url: The RPC endpoint.
        :param transport: Optional httpx transport (used to stub the network in tests).
        """
        self._url = url
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """Close the connection pool (on shutdown)."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, method: str, params: list[Any]) -> Any:
        """
        Run one JSON-RPC method and return its ``result``.

        :param method: The method name.
        :param params: The positional parameters.
        :return: The ``result`` member.
        :raises RpcError: On an error object, or if the RPC can't be reached or understood.
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
            error = body["error"]
            raise RpcError(f"{method}: {error.get('message', error) if isinstance(error, dict) else error}")
        return body.get("result")

    async def block_number(self) -> int:
        """
        The latest block number.

        :return: The block number.
        """
        return word(str(await self.call("eth_blockNumber", [])))

    async def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """
        A transaction's receipt.

        :param tx_hash: The transaction hash.
        :return: The receipt, or ``None`` while it isn't mined (or known).
        """
        result = await self.call("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    async def authorization_used(self, token: str, authorizer: str, nonce: str) -> bool:
        """
        Whether an EIP-3009 authorization was used (or cancelled) on the token contract.

        :param token: The token contract.
        :param authorizer: The authorization's ``from``.
        :param nonce: Its 32-byte nonce.
        :return: ``authorizationState(authorizer, nonce)``.
        """
        data = AUTHORIZATION_STATE_SELECTOR + topic_address(authorizer)[2:] + nonce[2:].lower()
        return word(str(await self.call("eth_call", [{"to": token, "data": data}, "latest"]))) != 0

    async def token_balance(self, token: str, owner: str) -> int:
        """
        An address's balance of an ERC-20 token.

        :param token: The token contract.
        :param owner: The holder.
        :return: Base units.
        """
        data = BALANCE_OF_SELECTOR + topic_address(owner)[2:]
        return word(str(await self.call("eth_call", [{"to": token, "data": data}, "latest"])))

    async def eth_balance(self, owner: str) -> int:
        """
        An address's ETH balance at the latest block.

        :param owner: The address.
        :return: Wei.
        """
        return word(str(await self.call("eth_getBalance", [owner, "latest"])))

    async def transaction_count(self, owner: str, block: int | str = "latest") -> int:
        """
        An address's nonce: how many transactions it has signed, as of a block (or ``"pending"``).

        :param owner: The address.
        :param block: A block number, ``"latest"`` or ``"pending"`` (which counts transactions waiting to be mined).
        :return: The nonce.
        """
        tag = hex(block) if isinstance(block, int) else block
        return word(str(await self.call("eth_getTransactionCount", [owner, tag])))

    async def code(self, owner: str, block: int | str = "latest") -> str:
        """
        The code at an address (``0x`` for a plain account; an EIP-7702 delegation shows as ``0xef0100…``).

        :param owner: The address.
        :param block: A block number or ``"latest"``.
        :return: The code, hex.
        """
        tag = hex(block) if isinstance(block, int) else block
        return str(await self.call("eth_getCode", [owner, tag]) or "0x")

    async def block_timestamp(self, block: int) -> int | None:
        """
        A block's time.

        :param block: The block number.
        :return: Unix seconds, or ``None`` if the block isn't known.
        """
        result = await self.call("eth_getBlockByNumber", [hex(block), False])
        return word(str(result["timestamp"])) if isinstance(result, dict) and "timestamp" in result else None

    async def base_fee(self) -> int:
        """
        The latest block's base fee per gas (EIP-1559).

        :return: Wei per gas.
        :raises RpcError: If the block has no base fee.
        """
        result = await self.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(result, dict) or "baseFeePerGas" not in result:
            raise RpcError("eth_getBlockByNumber: no base fee")
        return word(str(result["baseFeePerGas"]))

    async def max_priority_fee(self) -> int:
        """
        The node's suggested priority fee (tip) per gas.

        :return: Wei per gas.
        """
        return word(str(await self.call("eth_maxPriorityFeePerGas", [])))

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        """
        The gas a call would use, which also runs it against the latest state (a revert is an error).

        :param tx: ``from``, ``to``, ``data``, ``value``.
        :return: Gas units.
        """
        return word(str(await self.call("eth_estimateGas", [tx])))

    async def send_raw(self, raw: str) -> str:
        """
        Broadcast a signed transaction.

        :param raw: The signed transaction, ``0x`` hex.
        :return: Its hash, as the node reports it (a refusal is an :class:`RpcError`, from :meth:`call`).
        """
        return str(await self.call("eth_sendRawTransaction", [raw]))

    async def logs(self, token: str, topics: list[Any], from_block: int, to_block: int) -> list[dict[str, Any]]:
        """
        The token contract's logs matching ``topics`` in a block range (both ends included).

        :param token: The token contract.
        :param topics: The topic filter (a list per position means "any of").
        :param from_block: The first block.
        :param to_block: The last block.
        :return: The logs, oldest first.
        :raises RpcError: If the answer isn't a list of logs.
        """
        result = await self.call(
            "eth_getLogs",
            [{"address": token, "fromBlock": hex(from_block), "toBlock": hex(to_block), "topics": topics}],
        )
        if not isinstance(result, list):
            raise RpcError("eth_getLogs: unexpected response")
        return [entry for entry in result if isinstance(entry, dict)]
