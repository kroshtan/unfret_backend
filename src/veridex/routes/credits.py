"""
Prices, balances and buying credits.

``/v1/pricing``, ``/v1/credits``, ``/v1/credits/purchase-tx``, and fiat top-ups for API keys
(``/v1/credits/fiat-checkout`` and the provider's webhook, off until a provider is wired in).
"""

import base64
import time
from typing import Any

import segno
import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from solders.pubkey import Pubkey

from veridex.auth import get_credits, require_account
from veridex.config import settings
from veridex.credits import fiat, keys, ledger
from veridex.credits.fiat import FiatProvider, FiatWebhookError
from veridex.credits.pricing import pricing, purchase_limits, purchase_transaction, solana_pay_url
from veridex.facts import PageFacts
from veridex.prices import Currency, PriceUnavailableError
from veridex.schemas.errors import BadRequestError, NotFoundError, ServiceUnavailableError

logger = structlog.get_logger("veridex")

router = APIRouter(tags=["Credits"])


class CreditsResponse(BaseModel):
    """An account's balance."""

    address: str = Field(description="The account: the wallet's address, or key:<id> for an API key.")
    balance_usd_micros: int = Field(description="The balance in micro-dollars (1 USD = 1,000,000).")
    checks_remaining: int
    balance_lamports: int = Field(
        description="Deprecated: the same micro-dollar balance under its old name, kept for older clients."
    )


class PurchaseRequest(BaseModel):
    """Who buys how much, in which currency."""

    address: str = Field(max_length=64, description="The paying wallet (base58), which signs and sends the transfer.")
    currency: Currency = Field(default="SOL", description="Pay in SOL or USDC.")
    amount: int | None = Field(default=None, gt=0, description="Amount in the currency's base units.")
    lamports: int | None = Field(default=None, gt=0, description="Amount of SOL in lamports (same as amount for SOL).")
    beneficiary: str | None = Field(
        default=None,
        max_length=64,
        description="The wallet that gets the credits (defaults to address), e.g. a member key when a smart-wallet "
        "vault pays. Recorded in a memo the scanner reads.",
    )
    key_id: str | None = Field(
        default=None,
        max_length=120,
        description="Top up this API key instead (its id, or the key itself: only the id is used). Recorded in the "
        "memo as key:<id>.",
    )

    @model_validator(mode="after")
    def _one_amount(self) -> PurchaseRequest:
        if self.beneficiary and self.key_id:
            raise ValueError("give beneficiary or key_id, not both")
        if self.amount is None:
            if self.lamports is None or self.currency != "SOL":
                raise ValueError("give amount (base units of the currency)")
            self.amount = self.lamports
        return self


class PurchaseResponse(BaseModel):
    """The unsigned transfer for the buyer's wallet, and the same request as a Solana Pay link."""

    transaction: str
    currency: Currency
    amount: int
    lamports: int | None = Field(default=None, description="The amount for SOL purchases (backward compatible).")
    mint: str | None = Field(default=None, description="The token mint for USDC purchases.")
    hot_wallet: str
    solana_pay_url: str


@router.get("/v1/pricing")
async def get_pricing(request: Request) -> dict[str, Any]:
    """
    The dollar prices (analysis, x402 tiers), bonuses, packs and limits, with exact USDC amounts and a live SOL quote.

    :param request: The incoming request.
    :return: The price list.
    """
    door = get_credits(request)
    document = await pricing(settings, door.hot if door.purchases_enabled else None, door.book, door.usdc_mint)
    document["api_keys"] = door.sign_in_enabled
    document["fiat"] = {"available": get_fiat(request) is not None, "min_usd_micros": settings.fiat_min_usd_micros}
    document["reports"] = {
        "per_month": settings.reports_per_month,
        "reward_checks": settings.report_reward_checks,
    }
    return document


@router.get("/v1/credits", response_model=CreditsResponse)
async def get_balance(request: Request, address: str = Depends(require_account)) -> CreditsResponse:
    """
    The balance of the signed-in wallet or the API key.

    :param request: The incoming request.
    :param address: The account (injected by require_account).
    :return: The balance, and how many analyses it covers.
    """
    balance = await ledger.balance(request.app.db_pool, address) or 0
    return CreditsResponse(
        address=address,
        balance_usd_micros=balance,
        checks_remaining=max(balance, 0) // settings.analysis_price_usd_micros,
        balance_lamports=balance,
    )


async def _hot_for_purchase(request: Request, amount: int, currency: Currency = "SOL") -> Pubkey:
    door = get_credits(request)
    enabled = door.usdc_purchases_enabled if currency == "USDC" else door.purchases_enabled
    if not enabled or door.hot is None:
        raise ServiceUnavailableError("Buying credits is not available yet.")
    try:
        low, high = await purchase_limits(settings, door.book, currency)
    except PriceUnavailableError as exc:
        raise ServiceUnavailableError("SOL can't be priced right now; pay in USDC or try again shortly.") from exc
    if not low <= amount <= high:
        raise BadRequestError("Amount out of range.", metadata={"currency": currency, "min": low, "max": high})
    return door.hot


async def _key_account(request: Request, value: str) -> str:
    try:
        key_id = keys.key_id_from(value)
    except keys.ApiKeyError as exc:
        raise BadRequestError("Not an API key id.") from exc
    if not await keys.exists(request.app.db_pool, key_id):
        raise NotFoundError("Unknown API key.", error_code="UNKNOWN_KEY")
    return keys.account_of(key_id)


def _wallet(address: str, hot: Pubkey) -> Pubkey:
    try:
        wallet = Pubkey.from_string(address)
    except ValueError as exc:
        raise BadRequestError("Not a Solana address.") from exc
    if wallet == hot:
        raise BadRequestError("Not a buyer's address.")
    return wallet


@router.post("/v1/credits/purchase-tx", response_model=PurchaseResponse)
async def purchase_tx(body: PurchaseRequest, request: Request) -> PurchaseResponse:
    """
    Build the unsigned SOL or USDC transfer that buys credits; the buyer's wallet signs and sends it.

    Credits arrive once the transfer is finalized (about 15–30 seconds): its dollar value (SOL converted at crediting
    time, USDC exact) plus any bonus tier that value reaches, credited to the beneficiary named in the transaction's
    memo (the paying wallet unless ``beneficiary`` or ``key_id`` says otherwise). No sign-in needed: anyone may fund
    any wallet or API key.

    :param body: Who buys how much, in which currency.
    :param request: The incoming request.
    :return: The transaction (base64) and a Solana Pay link for phone wallets.
    """
    assert body.amount is not None  # noqa: S101 - set by the model validator
    hot = await _hot_for_purchase(request, body.amount, body.currency)
    payer = _wallet(body.address, hot)
    beneficiary: Pubkey | str = payer
    if body.key_id:
        beneficiary = await _key_account(request, body.key_id)
    elif body.beneficiary:
        beneficiary = _wallet(body.beneficiary, hot)
    door = get_credits(request)
    mint = door.usdc_mint if body.currency == "USDC" else None
    raw = purchase_transaction(payer, hot, body.amount, await door.rpc.latest_blockhash(), beneficiary, mint=mint)
    return PurchaseResponse(
        transaction=base64.b64encode(raw).decode(),
        currency=body.currency,
        amount=body.amount,
        lamports=body.amount if mint is None else None,
        mint=str(mint) if mint else None,
        hot_wallet=str(hot),
        solana_pay_url=solana_pay_url(hot, body.amount, beneficiary, mint),
    )


@router.get("/v1/credits/solana-pay.svg", response_class=Response)
async def solana_pay_qr(
    request: Request,
    *,
    lamports: int | None = Query(default=None, gt=0),
    amount: int | None = Query(default=None, gt=0),
    currency: Currency = Query(default="SOL"),
    beneficiary: str | None = Query(default=None, max_length=64),
    key_id: str | None = Query(default=None, max_length=120),
) -> Response:
    """
    The Solana Pay request as a QR code, for paying from a phone wallet.

    :param request: The incoming request.
    :param lamports: The SOL amount (backward compatible; same as ``amount`` for SOL).
    :param amount: The amount in the currency's base units.
    :param currency: ``SOL`` or ``USDC``.
    :param beneficiary: The wallet to credit (a memo); without it the paying wallet is credited.
    :param key_id: The API key to credit instead (a memo).
    :return: An SVG image.
    :raises BadRequestError: If no amount is given.
    """
    value = amount if amount is not None else (lamports if currency == "SOL" else None)
    if value is None:
        raise BadRequestError("Give amount (base units of the currency).")
    hot = await _hot_for_purchase(request, value, currency)
    target: Pubkey | str | None = _wallet(beneficiary, hot) if beneficiary else None
    if key_id:
        target = await _key_account(request, key_id)
    mint = get_credits(request).usdc_mint if currency == "USDC" else None
    svg = segno.make(solana_pay_url(hot, value, target, mint), error="m").svg_inline(scale=6, border=2, dark="#111")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/v1/schema/page-facts")
def page_facts_schema() -> dict[str, Any]:
    """
    The JSON schema of ``PageFacts``, for clients (the browser extension) that extract facts themselves.

    :return: The schema.
    """
    return PageFacts.model_json_schema()


# ── Fiat (API keys only) ──────────────────────────────────────────────────────────────────────────────────────────


class FiatCheckoutRequest(BaseModel):
    """Which key to top up with how much."""

    key_id: str = Field(max_length=120, description="The API key's id (or the key itself: only the id is used).")
    amount_usd_micros: int = Field(
        gt=0, description="The amount in micro-dollars, whole cents, at least fiat.min_usd_micros from /v1/pricing."
    )


class FiatCheckoutResponse(BaseModel):
    """Where to send the buyer."""

    checkout_url: str
    provider: str


def get_fiat(request: Request) -> FiatProvider | None:
    """
    The fiat provider, if one is wired in (none is yet: the Merchant of Record is still to be chosen).

    :param request: The incoming request.
    :return: The provider, or ``None`` while fiat is off.
    """
    provider: FiatProvider | None = request.app.fiat
    return provider


@router.post("/v1/credits/fiat-checkout", response_model=FiatCheckoutResponse)
async def fiat_checkout(body: FiatCheckoutRequest, request: Request) -> FiatCheckoutResponse:
    """
    Start a card (or other fiat) payment that tops up an API key, at the provider's hosted checkout.

    The provider handles the buyer's details, invoice and tax; this service only learns that the key was paid for.
    Credited when the provider's webhook confirms the payment, with the same bonus tiers as crypto.

    :param body: The key and the amount.
    :param request: The incoming request.
    :return: The checkout URL.
    :raises ServiceUnavailableError: While no fiat provider is configured.
    :raises BadRequestError: If the amount is out of range.
    """
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Card payments are not available yet; pay with SOL or USDC.")
    amount = body.amount_usd_micros
    in_range = settings.fiat_min_usd_micros <= amount <= settings.purchase_max_usd_micros
    if not in_range or amount % fiat.USD_MICROS_PER_CENT:
        raise BadRequestError(
            "Amount out of range.",
            metadata={"min": settings.fiat_min_usd_micros, "max": settings.purchase_max_usd_micros},
        )
    account = await _key_account(request, body.key_id)
    checkout = await provider.create_checkout(
        account, amount // fiat.USD_MICROS_PER_CENT, f"{settings.public_base_url}/checkout?paid=1"
    )
    await fiat.remember_checkout(request.app.db_pool, provider.name, checkout, account)
    return FiatCheckoutResponse(checkout_url=checkout.url, provider=provider.name)


@router.post("/v1/webhooks/fiat", include_in_schema=False)
async def fiat_webhook(request: Request) -> dict[str, Any]:
    """
    The provider's order webhook: verify it, then credit a paid order once or take back a refund or dispute.

    Only the order's id, amount, refunded amount, checkout id and our account id are read from the payload; the
    buyer's details in it are dropped unread.

    :param request: The incoming request (the raw body is what the signature covers).
    :return: What happened.
    :raises ServiceUnavailableError: While no fiat provider is configured.
    :raises BadRequestError: For an unauthentic, stale or unreadable webhook.
    """
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("No fiat provider configured.")
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        event = provider.parse_webhook(headers, await request.body(), time.time())
    except FiatWebhookError as exc:
        logger.warning("fiat_webhook_rejected", reason=str(exc))
        raise BadRequestError("Webhook rejected.") from exc
    if event is None:
        return {"status": "ignored"}
    outcome = await fiat.apply_event(request.app.db_pool, event, settings.credit_bonus_tiers)
    # Acknowledged either way (a retry wouldn't change it); an order we can't place needs a person.
    log = logger.error if outcome in {"unknown_account", "unknown_order"} else logger.info
    log("fiat_webhook", kind=event.kind, outcome=outcome, provider=event.provider, order_id=event.order_id)
    return {"status": outcome}
