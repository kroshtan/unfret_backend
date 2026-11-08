"""
Pay Veridex's x402 ``402 Payment Required`` from the agent's own wallet, within a spend guard.

Veridex offers wSOL and, while it can price it, USDC. The agent pays in USDC when it prefers to and holds enough,
otherwise in wSOL: it signs a token transfer to Veridex's hot wallet; Veridex adds its fee-payer signature and submits
it, so the agent pays only the price. If the agent's wSOL balance is short, plain SOL is wrapped first in a separate
transaction (native token program, no third party). Nothing here ever holds Veridex funds.
"""

import base64
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

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
    USDC_MINTS,
    WSOL_MINT,
    Rpc,
    associated_token_address,
    create_ata_idempotent,
    memo,
    sync_native,
    transfer_checked,
)

Currency = Literal["SOL", "USDC"]

_COMPUTE_UNITS = 50_000
_COMPUTE_UNIT_PRICE = 1  # micro-lamports; Veridex pays the fee, so keep its cost minimal
_WRAP_FEE_RESERVE = 10_000  # lamports kept back for the wrap transaction's own fee


class SpendRefusedError(RuntimeError):
    """A payment would exceed the per-call maximum or the session cap."""


class PaymentFailedError(RuntimeError):
    """The 402 could not be paid (no acceptable requirement, insufficient funds, or a refusal)."""


@dataclass
class SpendGuard:
    """Caps what one session may spend, per call and in total, separately per currency (base units)."""

    max_per_call: int
    session_cap: int
    usdc_max_per_call: int = 0
    usdc_session_cap: int = 0
    spent: int = field(default=0)
    usdc_spent: int = field(default=0)

    def reserve(self, amount: int, currency: Currency = "SOL") -> None:
        """
        Reserve ``amount`` before paying.

        :param amount: The price (lamports for SOL, base units for USDC).
        :param currency: ``SOL`` or ``USDC``.
        :raises SpendRefusedError: If it exceeds the per-call maximum or the remaining session budget.
        """
        per_call, cap, spent = (
            (self.usdc_max_per_call, self.usdc_session_cap, self.usdc_spent)
            if currency == "USDC"
            else (self.max_per_call, self.session_cap, self.spent)
        )
        unit = "USDC units" if currency == "USDC" else "lamports"
        if amount > per_call:
            raise SpendRefusedError(f"price {amount} {unit} exceeds the per-call maximum of {per_call}")
        if spent + amount > cap:
            raise SpendRefusedError(f"price {amount} {unit} would exceed the session cap of {cap}")
        if currency == "USDC":
            self.usdc_spent += amount
        else:
            self.spent += amount

    def release(self, amount: int, currency: Currency = "SOL") -> None:
        """
        Give back a reservation for a payment that didn't settle.

        :param amount: The reserved price.
        :param currency: ``SOL`` or ``USDC``.
        """
        if currency == "USDC":
            self.usdc_spent = max(0, self.usdc_spent - amount)
        else:
            self.spent = max(0, self.spent - amount)


def _b64json(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _unb64json(value: str) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(base64.b64decode(value))
    return decoded


def _mint(currency: Currency, network: str) -> Pubkey:
    if currency == "SOL":
        return WSOL_MINT
    return USDC_MINTS["solana-mainnet" if network == CAIP2["solana-mainnet"] else "solana-devnet"]


def choose_requirement(
    payment_required: dict[str, Any], network: str, currencies: tuple[Currency, ...] = ("SOL",)
) -> dict[str, Any]:
    """
    Pick the ``exact`` requirement on our network in the first acceptable currency.

    :param payment_required: The decoded ``PAYMENT-REQUIRED`` header.
    :param network: Our CAIP-2 network.
    :param currencies: The currencies we may pay in, in order of preference.
    :return: The requirement to pay.
    :raises PaymentFailedError: If none is acceptable.
    """
    for currency in currencies:
        mint = str(_mint(currency, network))
        for option in payment_required.get("accepts", []):
            flow = (option.get("extra") or {}).get("paymentFlow", "authorization")
            if (
                option.get("scheme") == "exact"
                and option.get("network") == network
                and option.get("asset") == mint
                and flow in {"upfront", "authorization"}
                and (option.get("extra") or {}).get("feePayer")
            ):
                return dict(option)
    raise PaymentFailedError(f"no acceptable payment option (exact, {'/'.join(currencies)}, on this network)")


def build_payment(requirement: dict[str, Any], agent: Keypair, blockhash: str) -> VersionedTransaction:
    """
    Build and partially sign (as the agent) the payment transaction for one requirement.

    :param requirement: The chosen ``PaymentRequirements`` (its ``asset`` is the mint paid in).
    :param agent: The agent's keypair (transfer authority).
    :param blockhash: A recent blockhash.
    :return: The transaction, missing only the fee payer's signature.
    """
    fee_payer = Pubkey.from_string(requirement["extra"]["feePayer"])
    pay_to = Pubkey.from_string(requirement["payTo"])
    mint = Pubkey.from_string(requirement["asset"])
    amount = int(requirement["amount"])
    instructions = [
        set_compute_unit_limit(_COMPUTE_UNITS),
        set_compute_unit_price(_COMPUTE_UNIT_PRICE),
        transfer_checked(
            associated_token_address(agent.pubkey(), mint),
            associated_token_address(pay_to, mint),
            agent.pubkey(),
            amount,
            mint,
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

    def __init__(  # noqa: PLR0913
        self,
        *,
        api_url: str,
        agent: Keypair,
        rpc: Rpc,
        network: str,
        guard: SpendGuard,
        prefer: Currency = "USDC",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Configure the payer.

        :param api_url: Veridex's base URL.
        :param agent: The agent's keypair.
        :param rpc: Solana RPC for the agent's network.
        :param network: ``solana-devnet`` or ``solana-mainnet``.
        :param guard: The session spend guard.
        :param prefer: Pay in USDC when offered, held and within the guard; otherwise SOL. ``SOL``: always SOL.
        :param transport: Optional httpx transport for the Veridex API (tests).
        """
        self.api_url = api_url.rstrip("/")
        self.agent = agent
        self.rpc = rpc
        self.network = CAIP2[network]
        self.guard = guard
        self.prefer: Currency = prefer
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

    async def _choose(self, payment_required: dict[str, Any]) -> tuple[dict[str, Any], Currency]:
        """
        Choose what to pay with and reserve it in the guard.

        :param payment_required: The decoded ``PAYMENT-REQUIRED`` header.
        :return: The requirement and its currency (already reserved).
        :raises PaymentFailedError: If no option can be paid.
        :raises SpendRefusedError: If the guard refuses the SOL price.
        """
        if self.prefer == "USDC":
            try:
                usdc = choose_requirement(payment_required, self.network, ("USDC",))
                amount = int(usdc["amount"])
                held = await self.rpc.token_balance(
                    associated_token_address(self.agent.pubkey(), Pubkey.from_string(usdc["asset"]))
                )
                if held >= amount:
                    self.guard.reserve(amount, "USDC")
                    return usdc, "USDC"
            except (PaymentFailedError, SpendRefusedError):
                pass  # no USDC offered, or not within the USDC caps: fall back to SOL
        sol = choose_requirement(payment_required, self.network, ("SOL",))
        self.guard.reserve(int(sol["amount"]), "SOL")
        return sol, "SOL"

    async def check(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        Run one check: request, pay the 402 within the guard, retry with the payment.

        :param body: The ``/v1/check`` request body.
        :return: ``{"status", "verdict" or "error", "transaction", "paid_lamports", "paid"}``.
        :raises PaymentFailedError: If the API doesn't ask for payment as expected.
        """
        async with httpx.AsyncClient(timeout=120, transport=self._transport) as client:
            first = await client.post(f"{self.api_url}/v1/check", json=body)
            if first.status_code != 402 or "PAYMENT-REQUIRED" not in first.headers:  # noqa: PLR2004
                raise PaymentFailedError(f"expected 402 Payment Required, got {first.status_code}: {first.text[:200]}")
            requirement, currency = await self._choose(_unb64json(first.headers["PAYMENT-REQUIRED"]))
            amount = int(requirement["amount"])
            settled = False
            try:
                if currency == "SOL":
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
                    self.guard.release(amount, currency)
        result: dict[str, Any] = {
            "status": second.status_code,
            "transaction": settlement.get("transaction"),
            "paid_lamports": amount if settled and currency == "SOL" else 0,
            "paid": {"currency": currency, "amount": amount if settled else 0},
        }
        result["verdict" if second.status_code == 200 else "error"] = second.json()  # noqa: PLR2004
        return result
