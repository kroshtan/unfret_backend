"""
Pay Veridex's x402 ``402 Payment Required`` from the agent's own wallet, within a spend guard.

The agent signs a wSOL transfer to Veridex's treasury; Veridex adds its fee-payer signature and submits it, so the
agent pays only the price. If the agent's wSOL balance is short, plain SOL is wrapped first in a separate
transaction (native token program, no third party). Nothing here ever holds Veridex funds.
"""

import base64
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from veridex_mcp.solana import (
    CAIP2,
    WSOL_MINT,
    Rpc,
    associated_token_address,
    create_ata_idempotent,
    memo,
    sync_native,
    transfer_checked,
)

_COMPUTE_UNITS = 50_000
_COMPUTE_UNIT_PRICE = 1  # micro-lamports; Veridex pays the fee, so keep its cost minimal
_WRAP_FEE_RESERVE = 10_000  # lamports kept back for the wrap transaction's own fee


class SpendRefusedError(RuntimeError):
    """A payment would exceed the per-call maximum or the session cap."""


class PaymentFailedError(RuntimeError):
    """The 402 could not be paid (no acceptable requirement, insufficient funds, or a refusal)."""


@dataclass
class SpendGuard:
    """Caps what one session may spend, per call and in total (lamports)."""

    max_per_call: int
    session_cap: int
    spent: int = field(default=0)

    def reserve(self, amount: int) -> None:
        """
        Reserve ``amount`` before paying.

        :param amount: The price.
        :raises SpendRefusedError: If it exceeds the per-call maximum or the remaining session budget.
        """
        if amount > self.max_per_call:
            raise SpendRefusedError(f"price {amount} lamports exceeds the per-call maximum of {self.max_per_call}")
        if self.spent + amount > self.session_cap:
            raise SpendRefusedError(f"price {amount} lamports would exceed the session cap of {self.session_cap}")
        self.spent += amount

    def release(self, amount: int) -> None:
        """
        Give back a reservation for a payment that didn't settle.

        :param amount: The reserved price.
        """
        self.spent = max(0, self.spent - amount)


def _b64json(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _unb64json(value: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(base64.b64decode(value))
    return decoded


def choose_requirement(payment_required: dict[str, Any], network: str) -> dict[str, Any]:
    """
    Pick the ``exact`` wSOL requirement on our network from a ``PaymentRequired``.

    :param payment_required: The decoded ``PAYMENT-REQUIRED`` header.
    :param network: Our CAIP-2 network.
    :return: The requirement to pay.
    :raises PaymentFailedError: If none is acceptable.
    """
    for option in payment_required.get("accepts", []):
        flow = (option.get("extra") or {}).get("paymentFlow", "authorization")
        if (
            option.get("scheme") == "exact"
            and option.get("network") == network
            and option.get("asset") == str(WSOL_MINT)
            and flow in {"upfront", "authorization"}
            and (option.get("extra") or {}).get("feePayer")
        ):
            return dict(option)
    raise PaymentFailedError("no acceptable payment option (exact, wSOL, on this network)")


def build_payment(requirement: dict[str, Any], agent: Keypair, blockhash: str) -> VersionedTransaction:
    """
    Build and partially sign (as the agent) the payment transaction for one requirement.

    :param requirement: The chosen ``PaymentRequirements``.
    :param agent: The agent's keypair (transfer authority).
    :param blockhash: A recent blockhash.
    :return: The transaction, missing only the fee payer's signature.
    """
    fee_payer = Pubkey.from_string(requirement["extra"]["feePayer"])
    pay_to = Pubkey.from_string(requirement["payTo"])
    amount = int(requirement["amount"])
    instructions = [
        set_compute_unit_limit(_COMPUTE_UNITS),
        set_compute_unit_price(_COMPUTE_UNIT_PRICE),
        transfer_checked(
            associated_token_address(agent.pubkey()), associated_token_address(pay_to), agent.pubkey(), amount
        ),
        memo(requirement["extra"].get("memo") or secrets.token_hex(16)),
    ]
    message = MessageV0.try_compile(fee_payer, instructions, [], Hash.from_string(blockhash))
    return VersionedTransaction.populate(
        message, [Signature.default(), agent.sign_message(to_bytes_versioned(message))]
    )


def build_wrap(agent: Keypair, lamports: int, blockhash: str) -> VersionedTransaction:
    """
    Build the agent's own transaction that wraps ``lamports`` of SOL into its wSOL account.

    :param agent: The agent's keypair (pays and signs).
    :param lamports: How much SOL to wrap.
    :param blockhash: A recent blockhash.
    :return: The signed transaction.
    """
    ata = associated_token_address(agent.pubkey())
    instructions = [
        create_ata_idempotent(agent.pubkey(), agent.pubkey()),
        transfer(TransferParams(from_pubkey=agent.pubkey(), to_pubkey=ata, lamports=lamports)),
        sync_native(ata),
    ]
    message = MessageV0.try_compile(agent.pubkey(), instructions, [], Hash.from_string(blockhash))
    return VersionedTransaction(message, [agent])


class Payer:
    """Calls Veridex's ``/v1/check``, paying the 402 from the agent's wallet."""

    def __init__(
        self,
        *,
        api_url: str,
        agent: Keypair,
        rpc: Rpc,
        network: str,
        guard: SpendGuard,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Configure the payer.

        :param api_url: Veridex's base URL.
        :param agent: The agent's keypair.
        :param rpc: Solana RPC for the agent's network.
        :param network: ``solana-devnet`` or ``solana-mainnet``.
        :param guard: The session spend guard.
        :param transport: Optional httpx transport for the Veridex API (tests).
        """
        self.api_url = api_url.rstrip("/")
        self.agent = agent
        self.rpc = rpc
        self.network = CAIP2[network]
        self.guard = guard
        self._transport = transport

    async def ensure_wsol(self, amount: int) -> None:
        """
        Make sure the agent's wSOL account holds at least ``amount``, wrapping SOL if it doesn't.

        :param amount: The lamports of wSOL needed.
        :raises PaymentFailedError: If the agent doesn't have enough SOL to wrap.
        """
        held = await self.rpc.token_balance(associated_token_address(self.agent.pubkey()))
        if held >= amount:
            return
        deficit = amount - held
        if await self.rpc.balance(self.agent.pubkey()) < deficit + _WRAP_FEE_RESERVE:
            raise PaymentFailedError(f"not enough SOL to wrap {deficit} lamports into wSOL")
        wrap = build_wrap(self.agent, deficit, await self.rpc.latest_blockhash())
        await self.rpc.send_and_confirm(bytes(wrap))

    async def check(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        Run one check: request, pay the 402 within the guard, retry with the payment.

        :param body: The ``/v1/check`` request body.
        :return: ``{"status", "verdict" or "error", "transaction", "paid_lamports"}``.
        :raises PaymentFailedError: If the API doesn't ask for payment as expected.
        """
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            first = await client.post(f"{self.api_url}/v1/check", json=body)
            if first.status_code != 402 or "PAYMENT-REQUIRED" not in first.headers:  # noqa: PLR2004
                raise PaymentFailedError(f"expected 402 Payment Required, got {first.status_code}: {first.text[:200]}")
            requirement = choose_requirement(_unb64json(first.headers["PAYMENT-REQUIRED"]), self.network)
            amount = int(requirement["amount"])
            self.guard.reserve(amount)
            settled = False
            try:
                await self.ensure_wsol(amount)
                tx = build_payment(requirement, self.agent, await self.rpc.latest_blockhash())
                payload = {
                    "x402Version": 2,
                    "accepted": requirement,
                    "payload": {"transaction": base64.b64encode(bytes(tx)).decode()},
                }
                second = await client.post(
                    f"{self.api_url}/v1/check", json=body, headers={"PAYMENT-SIGNATURE": _b64json(payload)}
                )
                settlement = (
                    _unb64json(second.headers["PAYMENT-RESPONSE"]) if "PAYMENT-RESPONSE" in second.headers else {}
                )
                settled = bool(settlement.get("success"))
            finally:
                if not settled:
                    self.guard.release(amount)
        result: dict[str, Any] = {
            "status": second.status_code,
            "transaction": settlement.get("transaction"),
            "paid_lamports": amount if settled else 0,
        }
        result["verdict" if second.status_code == 200 else "error"] = second.json()  # noqa: PLR2004
        return result
