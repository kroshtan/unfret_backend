"""
x402 on Base for the tests: EIP-3009 authorizations signed by throwaway keys, a stub Base RPC, a stub facilitator.

The keys are made per test run (``Account.create()``) and never hold anything; the facilitator's answers are those
recorded from x402.org on Base Sepolia (``tests/fixtures/x402_base``) or shaped exactly like them, and the stub RPC's
fees and gas estimate are those recorded from Base Sepolia's public RPC (``sepolia_rpc.json``).
"""

import json
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes

from unfret.config import Settings
from unfret.x402.base import BaseX402
from unfret.x402.evm import (
    AUTHORIZATION_USED_TOPIC,
    CAIP2,
    TRANSFER_TOPIC,
    USDC,
    BaseRpc,
    keccak256,
    topic_address,
)
from unfret.x402.facilitator import Facilitator
from unfret.x402.payment import encode_header
from unfret.x402.solana import RpcError

NETWORK = CAIP2["base-sepolia"]
SEPOLIA_USDC = USDC["base-sepolia"]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "x402_base"
RECORDED = json.loads((FIXTURES / "x402_org_sepolia.json").read_text())
RECORDED_RPC = json.loads((FIXTURES / "sepolia_rpc.json").read_text())

PAY_TO: LocalAccount = Account.create()  # the Base hot wallet (payTo); throwaway, holds nothing
COLD: LocalAccount = Account.create()
PAYER: LocalAccount = Account.create()


def hot_key(account: LocalAccount = PAY_TO) -> str:
    """A throwaway account's private key as ``UNFRET_X402_BASE_HOT_KEY`` takes it."""
    key = account.key.hex()
    return key if key.startswith("0x") else "0x" + key


def base_settings(**overrides: Any) -> Settings:
    """Base payments on Sepolia through a keyless facilitator, into the hot wallet ``PAY_TO``, swept to ``COLD``."""
    values: dict[str, Any] = {
        "x402_base_enabled": True,
        "x402_base_hot_key": hot_key(),
        "base_cold_address": COLD.address,
        "base_facilitator_url": "https://facilitator.test/x402",
        "price_per_check_usd": "0.15",
    }
    values.update(overrides)
    return Settings(**values)


def authorization(
    payer: LocalAccount = PAYER,
    *,
    to: str | None = None,
    value: int = 10_000,
    valid_after: int | None = None,
    valid_before: int | None = None,
    nonce: str | None = None,
    domain: tuple[str, str] = ("USDC", "2"),
    asset: str = SEPOLIA_USDC,
    chain_id: int = 84532,
) -> tuple[dict[str, Any], str]:
    """A signed ``transferWithAuthorization``, as an x402 client builds it (validAfter 10 min back, 5 min ahead)."""
    now = int(time.time())
    auth: dict[str, Any] = {
        "from": payer.address,
        "to": to or PAY_TO.address,
        "value": value,
        "validAfter": now - 600 if valid_after is None else valid_after,
        "validBefore": now + 300 if valid_before is None else valid_before,
        "nonce": nonce or "0x" + secrets.token_hex(32),
    }
    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": domain[0], "version": domain[1], "chainId": chain_id, "verifyingContract": asset},
        "message": auth,
    }
    signature = payer.sign_typed_data(full_message=typed).signature.hex()
    as_strings = {k: str(v) if k in {"value", "validAfter", "validBefore"} else v for k, v in auth.items()}
    return as_strings, signature if signature.startswith("0x") else "0x" + signature


def base_payload(accepted: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """A ``PaymentPayload`` answering ``accepted`` (kwargs go to :func:`authorization`)."""
    kwargs.setdefault("value", int(accepted["amount"]))
    auth, signature = authorization(**kwargs)
    return {
        "x402Version": 2,
        "resource": {"url": "https://client.test/echoed", "description": "what the client echoed"},
        "accepted": accepted,
        "payload": {"signature": signature, "authorization": auth},
        "extensions": {"bazaar": {"info": {"client": "poisoned"}}},
    }


def base_header(accepted: dict[str, Any], **kwargs: Any) -> str:
    """The ``PAYMENT-SIGNATURE`` header for :func:`base_payload`."""
    return encode_header(base_payload(accepted, **kwargs))


def settled_receipt(payer: str, pay_to: str, amount: int, nonce: str, *, status: str = "0x1") -> dict[str, Any]:
    """A Base receipt of a ``transferWithAuthorization``: the USDC Transfer and AuthorizationUsed logs."""
    return {
        "status": status,
        "logs": [
            {
                "address": SEPOLIA_USDC.lower(),
                "topics": [AUTHORIZATION_USED_TOPIC, topic_address(payer), nonce],
                "data": "0x",
            },
            {
                "address": SEPOLIA_USDC.lower(),
                "topics": [TRANSFER_TOPIC, topic_address(payer), topic_address(pay_to)],
                "data": "0x" + amount.to_bytes(32, "big").hex(),
            },
        ],
    }


def outflow_log(
    topic: str, owner: str, counterpart: str, value: int, tx: str = "0x" + "ab" * 32, **extra: Any
) -> dict[str, Any]:
    """A USDC Transfer or Approval log from ``owner``."""
    return {
        "address": SEPOLIA_USDC.lower(),
        "topics": [topic, topic_address(owner), topic_address(counterpart)],
        "data": "0x" + value.to_bytes(32, "big").hex(),
        "transactionHash": tx,
        **extra,
    }


def sweep_receipt(
    tx: str, amount: int, *, block: int, sender: str | None = None, to: str | None = None, status: str = "0x1"
) -> dict[str, Any]:
    """A receipt of the hot wallet's ``USDC.transfer(cold, amount)``, shaped like the recorded Sepolia one."""
    recorded = RECORDED_RPC["usdc_transfer_receipt"]
    sender = sender or PAY_TO.address
    log = outflow_log(TRANSFER_TOPIC, sender, to or COLD.address, amount, tx=tx, blockNumber=hex(block))
    return {
        **{k: v for k, v in recorded.items() if k != "logs"},
        "transactionHash": tx,
        "blockNumber": hex(block),
        "from": sender.lower(),
        "status": status,
        "logs": [log] if status == "0x1" else [],
    }


class FakeBaseRpc(BaseRpc):
    """
    Stands in for a Base RPC: receipts, authorization states, balances, the nonce, fees and logs by block range.

    Broadcast sweeps are decoded and kept in ``sent``; with ``mine`` (the default) each one lands at once: its receipt,
    its Transfer log at the head, the nonce and the balances move as on chain.
    """

    def __init__(self) -> None:
        super().__init__("http://base-rpc.invalid")
        self.receipts: dict[str, dict[str, Any]] = {}
        self.used: set[tuple[str, str]] = set()
        self.balance = 0
        self.eth = 10**15  # 0.001 ETH
        self.nonce = 0
        self.pending: int | None = None  # the pending nonce, when it differs (a transaction waiting to be mined)
        self.code_value = "0x"
        self.block_times: dict[int, int] = {}
        self.base_fee_wei = int(RECORDED_RPC["eth_getBlockByNumber"]["baseFeePerGas"], 16)
        self.tip_wei = int(RECORDED_RPC["eth_maxPriorityFeePerGas"], 16)
        self.gas_estimate = int(RECORDED_RPC["eth_estimateGas_usdc_transfer"], 16)
        self.sent: list[dict[str, Any]] = []
        self.send_error: Exception | None = None
        self.mine = True
        self.revert = False
        self.head = 10_000
        self.logs_by_block: dict[int, list[dict[str, Any]]] = {}
        self.log_calls: list[tuple[int, int]] = []
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise RpcError("rpc down")

    async def block_number(self) -> int:
        self._check()
        return self.head

    async def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        self._check()
        return self.receipts.get(tx_hash)

    async def authorization_used(self, token: str, authorizer: str, nonce: str) -> bool:
        self._check()
        return (authorizer.lower(), nonce.lower()) in self.used

    async def token_balance(self, token: str, owner: str) -> int:
        self._check()
        return self.balance

    async def eth_balance(self, owner: str) -> int:
        self._check()
        return self.eth

    async def transaction_count(self, owner: str, block: int | str = "latest") -> int:
        self._check()
        return self.pending if block == "pending" and self.pending is not None else self.nonce

    async def code(self, owner: str, block: int | str = "latest") -> str:
        self._check()
        return self.code_value

    async def block_timestamp(self, block: int) -> int | None:
        self._check()
        return self.block_times.get(block)

    async def base_fee(self) -> int:
        self._check()
        return self.base_fee_wei

    async def max_priority_fee(self) -> int:
        self._check()
        return self.tip_wei

    async def estimate_gas(self, tx: dict[str, Any]) -> int:
        self._check()
        assert tx["from"] == PAY_TO.address and tx["to"].lower() == SEPOLIA_USDC.lower()
        return self.gas_estimate

    async def send_raw(self, raw: str) -> str:
        self._check()
        if self.send_error is not None:
            raise self.send_error
        tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        sender = Account.recover_transaction(raw)
        tx_hash = "0x" + keccak256(bytes.fromhex(raw[2:])).hex()
        amount = int(tx["data"].hex()[-64:], 16)
        self.sent.append({**tx, "from": sender, "hash": tx_hash, "amount": amount})
        if self.mine:
            status = "0x0" if self.revert else "0x1"
            self.receipts[tx_hash] = sweep_receipt(tx_hash, amount, block=self.head, sender=sender, status=status)
            if not self.revert:
                self.logs_by_block.setdefault(self.head, []).extend(self.receipts[tx_hash]["logs"])
                self.balance -= amount
            self.nonce += 1
        else:
            self.pending = self.nonce + 1
        return tx_hash

    async def logs(self, token: str, topics: list[Any], from_block: int, to_block: int) -> list[dict[str, Any]]:
        self._check()
        self.log_calls.append((from_block, to_block))
        assert topics[1] == topic_address(PAY_TO.address)
        return [
            log
            for block, logs in sorted(self.logs_by_block.items())
            if from_block <= block <= to_block
            for log in logs
        ]


class FakeFacilitator:
    """
    An HTTP facilitator stub (as an httpx transport): answers verify and settle from what the test sets.

    ``verify`` and ``settle`` are the JSON bodies to answer (status 200); set one to an ``httpx.Response`` for anything
    else, or to an exception to raise. Requests are kept in ``requests``.
    """

    def __init__(self) -> None:
        self.verify: Any = {"isValid": True}
        self.settle: Any = {"success": True, "transaction": "0x" + "12" * 32, "network": NETWORK}
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.requests.append((endpoint, body, dict(request.headers)))
        answer = self.verify if endpoint == "verify" else self.settle
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        if endpoint == "settle" and answer.get("success") and "payer" not in answer:
            answer = {**answer, "payer": body["paymentPayload"]["payload"]["authorization"]["from"]}
        return httpx.Response(200, json=answer)

    def client(self, url: str = "https://facilitator.test/x402") -> Facilitator:
        return Facilitator(url, transport=httpx.MockTransport(self.handler))

    def calls(self, endpoint: str) -> list[dict[str, Any]]:
        return [body for name, body, _ in self.requests if name == endpoint]


def make_base(settings: Settings, rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> BaseX402:
    return BaseX402(settings, rpc=rpc, facilitator=facilitator.client())
