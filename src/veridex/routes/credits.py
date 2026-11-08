"""Prices, balances and buying credits: ``/v1/pricing``, ``/v1/credits``, ``/v1/credits/purchase-tx``."""

import base64
from typing import Any

import segno
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from solders.pubkey import Pubkey

from veridex.auth import get_credits, require_wallet
from veridex.config import settings
from veridex.credits import ledger
from veridex.credits.pricing import pricing, purchase_limits, purchase_transaction, solana_pay_url
from veridex.facts import PageFacts
from veridex.prices import Currency, PriceUnavailableError
from veridex.schemas.errors import BadRequestError, ServiceUnavailableError

router = APIRouter(tags=["Credits"])


class CreditsResponse(BaseModel):
    """A wallet's balance."""

    address: str
    balance_lamports: int
    checks_remaining: int


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

    @model_validator(mode="after")
    def _one_amount(self) -> PurchaseRequest:
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
    The canonical SOL prices (analysis, x402 tiers), bonuses, packs and limits, plus a live USDC quote.

    :param request: The incoming request.
    :return: The price list.
    """
    door = get_credits(request)
    return await pricing(settings, door.hot if door.purchases_enabled else None, door.book, door.usdc_mint)


@router.get("/v1/credits", response_model=CreditsResponse)
async def get_balance(request: Request, address: str = Depends(require_wallet)) -> CreditsResponse:
    """
    The signed-in wallet's balance.

    :param request: The incoming request.
    :param address: The signed-in wallet.
    :return: The balance, and how many analyses it covers.
    """
    lamports = await ledger.balance(request.app.db_pool, address) or 0
    return CreditsResponse(
        address=address, balance_lamports=lamports, checks_remaining=lamports // settings.analysis_price_lamports
    )


async def _hot_for_purchase(request: Request, amount: int, currency: Currency = "SOL") -> Pubkey:
    door = get_credits(request)
    enabled = door.usdc_purchases_enabled if currency == "USDC" else door.purchases_enabled
    if not enabled or door.hot is None:
        raise ServiceUnavailableError("Buying credits is not available yet.")
    try:
        low, high = await purchase_limits(settings, door.book, currency)
    except PriceUnavailableError as exc:
        raise ServiceUnavailableError("USDC can't be priced right now; pay in SOL or try again shortly.") from exc
    if not low <= amount <= high:
        raise BadRequestError("Amount out of range.", metadata={"currency": currency, "min": low, "max": high})
    return door.hot


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

    Credits arrive once the transfer is finalized (about 15–30 seconds): its SOL value (USDC converted at crediting
    time) plus any bonus tier that value reaches, credited to the beneficiary named in the transaction's memo (the
    paying wallet unless ``beneficiary`` says otherwise). No sign-in needed: anyone may fund any wallet.

    :param body: Who buys how much, in which currency.
    :param request: The incoming request.
    :return: The transaction (base64) and a Solana Pay link for phone wallets.
    """
    assert body.amount is not None  # noqa: S101 - set by the model validator
    hot = await _hot_for_purchase(request, body.amount, body.currency)
    payer = _wallet(body.address, hot)
    beneficiary = _wallet(body.beneficiary, hot) if body.beneficiary else payer
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
    lamports: int | None = Query(default=None, gt=0),
    amount: int | None = Query(default=None, gt=0),
    currency: Currency = Query(default="SOL"),
    beneficiary: str | None = Query(default=None, max_length=64),
) -> Response:
    """
    The Solana Pay request as a QR code, for paying from a phone wallet.

    :param request: The incoming request.
    :param lamports: The SOL amount (backward compatible; same as ``amount`` for SOL).
    :param amount: The amount in the currency's base units.
    :param currency: ``SOL`` or ``USDC``.
    :param beneficiary: The wallet to credit (a memo); without it the paying wallet is credited.
    :return: An SVG image.
    :raises BadRequestError: If no amount is given.
    """
    value = amount if amount is not None else (lamports if currency == "SOL" else None)
    if value is None:
        raise BadRequestError("Give amount (base units of the currency).")
    hot = await _hot_for_purchase(request, value, currency)
    target = _wallet(beneficiary, hot) if beneficiary else None
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
