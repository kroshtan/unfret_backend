"""
Pay Unfret's x402 ``402 Payment Required`` from the agent's own wallet, within a spend guard.

Unfret prices checks in US dollars and offers wSOL (converted at its quote) and USDC (exact). The spend guard caps
dollars: USDC at face value, wSOL valued at the agent's own Pyth SOL/USD reading. The agent pays in USDC when it
prefers to and holds enough, otherwise in wSOL: it signs a token transfer to Unfret's hot wallet; Unfret adds its
fee-payer signature and submits it, so the agent pays only the price. If the agent's wSOL balance is short, plain SOL
is wrapped first in a separate transaction (native token program, no third party). Nothing here holds Unfret funds.
"""

import base64
import json
import math
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
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

from unfret_mcp.price import PriceError, SolUsd
from unfret_mcp.solana import (
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
_COMPUTE_UNIT_PRICE = 1  # micro-lamports; Unfret pays the fee, so keep its cost minimal
_WRAP_FEE_RESERVE = 10_000  # lamports kept back for the wrap transaction's own fee
# Unfret answers a paid check within about 90 s (settlement plus a 60 s analysis cap).
_TIMEOUT = httpx.Timeout(100, connect=10)


class SpendRefusedError(RuntimeError):
    """A payment would exceed the per-call maximum or the session cap."""


class PaymentFailedError(RuntimeError):
    """The 402 could not be paid (no acceptable requirement, insufficient funds, or a refusal)."""


class UnavailableError(PaymentFailedError):
    """Unfret can't take a payment right now (503 or 429); nothing was paid."""

    def __init__(self, detail: str, retry_after: int | None) -> None:
        """
        Record the answer.

        :param detail: Unfret's ``error_code`` and ``detail``.
        :param retry_after: Seconds to wait, if Unfret said.
        """
        super().__init__(detail)
        self.retry_after = retry_after


@dataclass
class SpendGuard:
    """Caps what one session may spend, per call and in total, in micro-dollars (1 USD = 1,000,000)."""

    max_per_call: int
    session_cap: int
    spent: int = field(default=0)

    def reserve(self, usd_micros: int) -> None:
        """
        Reserve a payment's dollar value before paying.

        :param usd_micros: What the payment is worth, in micro-dollars.
        :raises SpendRefusedError: If it exceeds the per-call maximum or the remaining session budget.
        """
        price = f"${usd_micros / 1e6:.4f}"
        if usd_micros > self.max_per_call:
            raise SpendRefusedError(f"price {price} exceeds the per-call maximum of ${self.max_per_call / 1e6:.2f}")
        if self.spent + usd_micros > self.session_cap:
            raise SpendRefusedError(f"price {price} would exceed the session cap of ${self.session_cap / 1e6:.2f}")
        self.spent += usd_micros

    def release(self, usd_micros: int) -> None:
        """
        Give back a reservation for a payment that didn't settle.

        :param usd_micros: The reserved value.
        """
        self.spent = max(0, self.spent - usd_micros)


def sol_value(lamports: int, usd_per_sol: Decimal) -> int:
    """
    What ``lamports`` are worth, in micro-dollars, rounded up (the guard errs towards refusing).

    :param lamports: The amount of SOL.
    :param usd_per_sol: The price.
    :return: Micro-dollars.
    """
    return math.ceil(Decimal(lamports) * usd_per_sol / 1000)


def _describe(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        return f"{body.get('error_code', response.status_code)}: {body.get('detail', '')}"
    return f"HTTP {response.status_code}"


def _retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "")
    return int(value) if value.isdigit() else None


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
    """Calls Unfret's ``/v1/check``, paying the 402 from the agent's wallet."""

    def __init__(
        self,
        *,
        api_url: str,
        agent: Keypair,
        rpc: Rpc,
        network: str,
        guard: SpendGuard,
        sol_usd: SolUsd | None = None,
        prefer: Currency = "USDC",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Configure the payer.

        :param api_url: Unfret's base URL.
        :param agent: The agent's keypair.
        :param rpc: Solana RPC for the agent's network.
        :param network: ``solana-devnet`` or ``solana-mainnet``.
        :param guard: The session spend guard (dollars).
        :param sol_usd: The agent's own SOL/USD price, to value wSOL charges; without it SOL payments are refused.
        :param prefer: Pay in USDC when offered, held and within the guard; otherwise SOL. ``SOL``: always SOL.
        :param transport: Optional httpx transport for the Unfret API (tests).
        """
        self.api_url = api_url.rstrip("/")
        self.agent = agent
        self.rpc = rpc
        self.network = CAIP2[network]
        self.guard = guard
        self.sol_usd = sol_usd
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

    async def _sol_value(self, lamports: int) -> int:
        if self.sol_usd is None:
            raise PaymentFailedError("can't value a SOL payment without a SOL/USD price; pay in USDC")
        try:
            return sol_value(lamports, await self.sol_usd.usd_per_sol())
        except PriceError as exc:
            raise PaymentFailedError(f"can't value a SOL payment right now ({exc}); pay in USDC") from exc

    async def _choose(self, payment_required: dict[str, Any]) -> tuple[dict[str, Any], Currency, int]:
        """
        Choose what to pay with and reserve its dollar value in the guard.

        :param payment_required: The decoded ``PAYMENT-REQUIRED`` header.
        :return: The requirement, its currency and the reserved micro-dollars.
        :raises PaymentFailedError: If no option can be paid.
        """
        try:
            usdc: dict[str, Any] | None = choose_requirement(payment_required, self.network, ("USDC",))
        except PaymentFailedError:
            usdc = None
        if usdc is not None and self.prefer == "USDC":
            amount = int(usdc["amount"])
            held = await self.rpc.token_balance(
                associated_token_address(self.agent.pubkey(), Pubkey.from_string(usdc["asset"]))
            )
            if held >= amount:
                self.guard.reserve(amount)  # USDC base units are micro-dollars
                return usdc, "USDC", amount
        try:
            sol = choose_requirement(payment_required, self.network, ("SOL",))
        except PaymentFailedError:
            if usdc is None:
                raise
            raise PaymentFailedError("only USDC is offered and the wallet holds too little of it") from None
        value = await self._sol_value(int(sol["amount"]))
        self.guard.reserve(value)
        return sol, "SOL", value

    async def check(self, body: dict[str, Any]) -> dict[str, Any]:
        """
        Run one check: request, pay the 402 within the guard, send the payment (retried once, the same payment).

        The guard gives the reservation back only when Unfret clearly refused before taking the money (a renewed
        402, a 4xx without a receipt, or a receipt saying nothing was submitted). When the outcome is unknown (a
        timeout, a 5xx or an unreadable answer) the money may have moved, so it stays counted.

        :param body: The ``/v1/check`` request body.
        :return: ``{"status": ok|not_paid|pending|unknown, "ok", "http_status", "verdict" or "error", "transaction",
            "paid", "paid_lamports", "next_step"?}``.
        :raises PaymentFailedError: If the API doesn't ask for payment as expected.
        """
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
            first = await client.post(f"{self.api_url}/v1/check", json=body)
            if first.status_code in {429, 503}:
                raise UnavailableError(_describe(first), _retry_after(first))
            if first.status_code != 402 or "PAYMENT-REQUIRED" not in first.headers:
                raise PaymentFailedError(f"expected 402 Payment Required, got {first.status_code}: {first.text[:200]}")
            requirement, currency, usd_micros = await self._choose(_unb64json(first.headers["PAYMENT-REQUIRED"]))
            amount = int(requirement["amount"])
            try:
                if currency == "SOL":
                    await self.ensure_wsol(amount)
                tx = build_payment(requirement, self.agent, await self.rpc.latest_blockhash())
            except BaseException:
                self.guard.release(usd_micros)  # nothing was sent
                raise
            payload = {
                "x402Version": 2,
                "accepted": requirement,
                "payload": {"transaction": base64.b64encode(bytes(tx)).decode()},
            }
            second: httpx.Response | None = None
            for _ in range(2):  # Unfret answers a retry of the same payment with the same result, charging once
                try:
                    second = await client.post(
                        f"{self.api_url}/v1/check", json=body, headers={"PAYMENT-SIGNATURE": _b64json(payload)}
                    )
                except httpx.TransportError:
                    second = None
                    continue
                if second.status_code < 500 or "PAYMENT-RESPONSE" in second.headers:
                    break
        return self._outcome(second, currency, amount, usd_micros, str(tx.signatures[1]))

    def _outcome(
        self, response: httpx.Response | None, currency: Currency, amount: int, usd_micros: int, payment: str
    ) -> dict[str, Any]:
        has_receipt = response is not None and "PAYMENT-RESPONSE" in response.headers
        receipt = _unb64json(response.headers["PAYMENT-RESPONSE"]) if response is not None and has_receipt else {}
        readable = True
        try:
            content: Any = response.json() if response is not None else None
        except ValueError:
            readable, content = False, {"detail": response.text[:200] if response is not None else ""}
        settled = bool(receipt.get("success"))
        unanswered = response is None or not readable or not isinstance(content, dict)
        if unanswered or (not receipt and response is not None and response.status_code >= 500):
            status = "unknown"
        elif settled:
            status = "ok"
        elif receipt.get("transaction") and receipt.get("errorReason") != "settlement_failed":
            status = "pending" if receipt.get("errorReason") == "settlement_pending" else "unknown"
        else:
            status = "not_paid"  # refused before any money moved
        if status == "not_paid":
            self.guard.release(usd_micros)
        charged = settled or status in {"pending", "unknown"}
        result: dict[str, Any] = {
            "status": status,
            "ok": status == "ok" and response is not None and response.status_code == 200,
            "http_status": response.status_code if response is not None else None,
            "transaction": receipt.get("transaction") or None,
            "payment": payment,
            "paid_lamports": amount if settled and currency == "SOL" else 0,
            "paid": {
                "currency": currency,
                "amount": amount if settled else 0,
                "usd_micros": usd_micros if settled else 0,
            },
        }
        result["verdict" if result["ok"] else "error"] = content
        if status in {"pending", "unknown"}:
            result["next_step"] = (
                "The payment may have been taken; don't pay again for this page now. Check the transaction on chain; "
                "if it landed without a verdict, Unfret credits its price to the paying wallet."
            )
        elif not result["ok"] and charged:
            result["next_step"] = "Paid, but no verdict: the price was refunded as Unfret credits (see error)."
        return result
