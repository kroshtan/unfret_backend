"""Prices, balances and buying credits: ``/v1/pricing``, ``/v1/credits``, ``/v1/credits/purchase-tx``."""

import base64
from typing import Any

import segno
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from solders.pubkey import Pubkey

from veridex.auth import get_credits, require_wallet
from veridex.config import settings
from veridex.credits import ledger
from veridex.credits.pricing import pricing, purchase_transaction, solana_pay_url
from veridex.facts import PageFacts
from veridex.schemas.errors import BadRequestError, ServiceUnavailableError

router = APIRouter(tags=["Credits"])


class CreditsResponse(BaseModel):
    """A wallet's balance."""

    address: str
    balance_lamports: int
    checks_remaining: int


class PurchaseRequest(BaseModel):
    """Who buys how much."""

    address: str = Field(max_length=64, description="The buying wallet (base58); credits go to it.")
    lamports: int = Field(gt=0, description="Amount of SOL to send, in lamports.")


class PurchaseResponse(BaseModel):
    """The unsigned transfer for the buyer's wallet, and the same request as a Solana Pay link."""

    transaction: str
    lamports: int
    hot_wallet: str
    solana_pay_url: str


@router.get("/v1/pricing")
def get_pricing(request: Request) -> dict[str, Any]:
    """
    The price of an analysis, x402 tier prices, purchase bonuses, suggested packs and where payments go.

    :param request: The incoming request.
    :return: The price list.
    """
    door = get_credits(request)
    return pricing(settings, door.hot if door.purchases_enabled else None)


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


def _purchase_parties(request: Request, address: str, lamports: int) -> tuple[Pubkey, Pubkey]:
    door = get_credits(request)
    if not door.purchases_enabled or door.hot is None:
        raise ServiceUnavailableError("Buying credits is not available yet.")
    if not settings.purchase_min_lamports <= lamports <= settings.purchase_max_lamports:
        raise BadRequestError(
            "Amount out of range.",
            metadata={"min_lamports": settings.purchase_min_lamports, "max_lamports": settings.purchase_max_lamports},
        )
    try:
        payer = Pubkey.from_string(address)
    except ValueError as exc:
        raise BadRequestError("Not a Solana address.") from exc
    if payer == door.hot:
        raise BadRequestError("Not a buyer's address.")
    return payer, door.hot


@router.post("/v1/credits/purchase-tx", response_model=PurchaseResponse)
async def purchase_tx(body: PurchaseRequest, request: Request) -> PurchaseResponse:
    """
    Build the unsigned SOL transfer that buys credits; the buyer's wallet signs and sends it.

    Credits arrive once the transfer is finalized (about 15–30 seconds), credited to the sending wallet with any
    purchase bonus. No sign-in needed: anyone may fund any wallet.

    :param body: Who buys how much.
    :param request: The incoming request.
    :return: The transaction (base64) and a Solana Pay link for phone wallets.
    """
    payer, hot = _purchase_parties(request, body.address, body.lamports)
    raw = purchase_transaction(payer, hot, body.lamports, await get_credits(request).rpc.latest_blockhash())
    return PurchaseResponse(
        transaction=base64.b64encode(raw).decode(),
        lamports=body.lamports,
        hot_wallet=str(hot),
        solana_pay_url=solana_pay_url(hot, body.lamports),
    )


@router.get("/v1/credits/solana-pay.svg", response_class=Response)
def solana_pay_qr(request: Request, lamports: int = Query(gt=0)) -> Response:
    """
    The Solana Pay request for ``lamports`` as a QR code, for paying from a phone wallet.

    :param request: The incoming request.
    :param lamports: The amount.
    :return: An SVG image.
    :raises ServiceUnavailableError: If buying credits is not available.
    :raises BadRequestError: If the amount is out of range.
    """
    door = get_credits(request)
    if not door.purchases_enabled or door.hot is None:
        raise ServiceUnavailableError("Buying credits is not available yet.")
    if not settings.purchase_min_lamports <= lamports <= settings.purchase_max_lamports:
        raise BadRequestError("Amount out of range.")
    svg = segno.make(solana_pay_url(door.hot, lamports), error="m").svg_inline(scale=6, border=2, dark="#111")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/v1/schema/page-facts")
def page_facts_schema() -> dict[str, Any]:
    """
    The JSON schema of ``PageFacts``, for clients (the browser extension) that extract facts themselves.

    :return: The schema.
    """
    return PageFacts.model_json_schema()
