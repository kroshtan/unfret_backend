"""
Prices, balances and buying credits.

``/v1/pricing``, ``/v1/credits``, ``/v1/credits/purchase-tx`` (a pack with SOL or USDC, VAT where it applies), and
card top-ups for API keys (``/v1/credits/fiat-checkout`` and the provider's webhook, off until a provider is wired
in).
"""

import base64
import io
import re
import time
from typing import Any

import segno
import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from solders.pubkey import Pubkey

from unfret.auth import get_credits, require_account
from unfret.config import settings
from unfret.credits import fiat, invites, keys, ledger, vat
from unfret.credits.fiat import FiatProvider, FiatProviderError, FiatWebhookError
from unfret.credits.packs import Pack, pack, packs
from unfret.credits.pricing import pricing, purchase_transaction, solana_pay_url
from unfret.facts import PageFacts
from unfret.prices import Currency, PriceUnavailableError, format_usd
from unfret.schemas.errors import BadRequestError, NotFoundError, ServiceUnavailableError

logger = structlog.get_logger("unfret")

router = APIRouter(tags=["Credits"])


class CreditsResponse(BaseModel):
    """An account's balance."""

    address: str = Field(description="The account: the wallet's address, or key:<id> for an API key.")
    balance_usd_micros: int = Field(description="The balance in micro-dollars (1 USD = 1,000,000).")
    checks_remaining: int
    balance_lamports: int = Field(
        description="Deprecated: the same micro-dollar balance under its old name, kept for older clients."
    )
    invite_eligible: bool = Field(
        description="Whether an invite can still be redeemed onto this account: only one that has never had credit."
    )


class PurchaseRequest(BaseModel):
    """Which pack, for whom, paid how, from where."""

    address: str = Field(max_length=64, description="The paying wallet (base58), which signs and sends the transfer.")
    currency: Currency = Field(default="SOL", description="Pay in SOL or USDC.")
    checks: int = Field(gt=0, description="The pack: one of the pack sizes in /v1/pricing.")
    country: str = Field(
        min_length=2,
        max_length=2,
        description="The buyer's country (ISO 3166 alpha-2), for VAT. The checkout prefills it from where the "
        "request comes from; the buyer can change it.",
    )
    beneficiary: str | None = Field(
        default=None,
        max_length=64,
        description="The wallet that gets the credits (defaults to address), e.g. a member key when a smart-wallet "
        "vault pays.",
    )
    key_id: str | None = Field(
        default=None,
        max_length=120,
        description="Top up this API key instead (its id, or the key itself: only the id is used).",
    )

    @model_validator(mode="after")
    def _one_target(self) -> PurchaseRequest:
        if self.beneficiary and self.key_id:
            raise ValueError("give beneficiary or key_id, not both")
        return self


class VatBreakdown(BaseModel):
    """What the buyer pays, in dollars, and why."""

    country: str
    vat_rate_pct: str
    net_usd_micros: int = Field(description="The pack's price, credited to the account.")
    vat_usd_micros: int
    total_usd_micros: int = Field(description="What is charged: net plus VAT.")
    total_usd: str


class PurchaseResponse(BaseModel):
    """The unsigned transfer for the buyer's wallet, and the same request as a Solana Pay link."""

    transaction: str
    currency: Currency
    amount: int = Field(description="The amount in the currency's base units (VAT included).")
    lamports: int | None = Field(default=None, description="The amount for SOL purchases (backward compatible).")
    mint: str | None = Field(default=None, description="The token mint for USDC purchases.")
    hot_wallet: str
    solana_pay_url: str
    checks: int
    price: VatBreakdown
    quote_id: str = Field(description="The purchase's quote: poll GET /v1/credits/quotes/{quote_id} to see it paid.")
    claim: str = Field(
        description="Send as X-Unfret-Claim when polling the quote: the first poll after payment returns the pack's "
        "invite codes, once. Keep it in the buyer's page only; it isn't stored, so it can't be shown again."
    )


class UnknownQuoteError(NotFoundError):
    """No such quote (never was, malformed, or purged a day after it expired)."""

    error_code = "UNKNOWN_QUOTE"
    headers = {"Cache-Control": "no-store"}


INVITES_DESCRIPTION = (
    "The pack's invite codes (ufi_...), to give away: returned once, in the first response after payment that "
    "carries the purchase's X-Unfret-Claim, then never again (only their hashes are kept). Empty otherwise."
)


class QuoteStatusResponse(BaseModel):
    """Whether a purchase's payment has been credited, and its invites once."""

    paid: bool = Field(description="True once a payment naming this quote has been credited.")
    checks: int = Field(description="The pack size the quote is for.")
    invites: list[str] = Field(default_factory=list, description=INVITES_DESCRIPTION)


@router.get("/v1/pricing")
async def get_pricing(request: Request, response: Response) -> dict[str, Any]:
    """
    The price of a check and everything derived from it.

    The x402 tiers, the packs (by card, and the lower crypto
    price), the VAT rules for crypto, exact USDC amounts and a live SOL quote.

    :param request: The incoming request.
    :param response: The response (marked uncacheable: it carries the visitor's likely country).
    :return: The price list.
    """
    door = get_credits(request)
    document = await pricing(settings, door.hot if door.purchases_enabled else None, door.book, door.usdc_mint)
    document["vat"]["country_hint"] = vat.edge_country({k.lower(): v for k, v in request.headers.items()})
    document["api_keys"] = door.sign_in_enabled
    smallest = packs(settings)[0].card_usd_micros
    document["fiat"] = {"available": get_fiat(request) is not None, "min_usd_micros": smallest}
    document["reports"] = {
        "per_month": settings.reports_per_month,
        "reward_checks": settings.report_reward_checks,
    }
    response.headers["Cache-Control"] = "private, no-store"
    return document


@router.get("/v1/credits", response_model=CreditsResponse)
async def get_balance(request: Request, address: str = Depends(require_account)) -> CreditsResponse:
    """
    The balance of the signed-in wallet or the API key.

    :param request: The incoming request.
    :param address: The account (injected by require_account).
    :return: The balance, how many analyses it covers, and whether an invite can still be redeemed onto it.
    """
    row = await ledger.balance(request.app.db_pool, address)
    balance = row or 0
    return CreditsResponse(
        address=address,
        balance_usd_micros=balance,
        checks_remaining=max(balance, 0) // settings.analysis_price_usd_micros,
        balance_lamports=balance,
        invite_eligible=row is None,  # the same test redeem makes: an account with a ledger row had credit
    )


def _hot_for_purchase(request: Request, currency: Currency = "SOL") -> Pubkey:
    door = get_credits(request)
    enabled = door.usdc_purchases_enabled if currency == "USDC" else door.purchases_enabled
    if not enabled or door.hot is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Buying checks is not available yet.")
    return door.hot


def _pack(checks: int) -> Pack:
    chosen = pack(settings, checks)
    if chosen is None:
        raise BadRequestError("Not a pack size.", metadata={"packs": settings.packs})
    return chosen


def _country(value: str) -> str:
    country = vat.normalise_country(value)
    if country is None:
        raise BadRequestError("Not a country code (ISO 3166 alpha-2).")
    return country


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


async def _quote(
    request: Request, checks: int, country: str, currency: Currency, beneficiary: str | None
) -> tuple[vat.Quote, int, str]:
    """Price and store a pack for ``country``, convert its total into ``currency``'s base units, make its claim."""
    chosen = _pack(checks)
    edge = vat.edge_country({k.lower(): v for k, v in request.headers.items()})
    quote = vat.price_quote(
        settings, chosen.checks, chosen.value_usd_micros, _country(country), edge=edge, beneficiary=beneficiary
    )
    try:
        amount = await get_credits(request).book.amount(quote.gross_usd_micros, currency)
    except PriceUnavailableError as exc:
        raise ServiceUnavailableError("SOL can't be priced right now; pay in USDC or try again shortly.") from exc
    claim = invites.new_claim()
    await vat.save_quote(request.app.db_pool, quote, claim_hash=invites.digest(claim))
    return quote, amount, claim


def _breakdown(quote: vat.Quote) -> VatBreakdown:
    return VatBreakdown(
        country=quote.country,
        vat_rate_pct=format(quote.vat_rate_bps / 100, "g"),
        net_usd_micros=quote.net_usd_micros,
        vat_usd_micros=quote.vat_usd_micros,
        total_usd_micros=quote.gross_usd_micros,
        total_usd=format_usd(quote.gross_usd_micros),
    )


@router.post("/v1/credits/purchase-tx", response_model=PurchaseResponse)
async def purchase_tx(body: PurchaseRequest, request: Request) -> PurchaseResponse:
    """
    Build the unsigned SOL or USDC transfer that buys a pack of credits; the buyer's wallet signs and sends it.

    The pack costs its crypto price plus the VAT of the buyer's country (see ``vat`` in ``/v1/pricing``); the
    transaction's memo names the stored quote. Credits arrive once the transfer is finalized (about 15–30 seconds):
    what arrived, minus its VAT, credited to the paying wallet unless ``beneficiary`` or ``key_id`` says otherwise.
    No sign-in needed: anyone may fund any wallet or API key.

    :param body: Which pack, for whom, paid how, from where.
    :param request: The incoming request.
    :return: The transaction (base64), a Solana Pay link for phone wallets, and the price breakdown.
    """
    hot = _hot_for_purchase(request, body.currency)
    payer = _wallet(body.address, hot)
    beneficiary: str | None = None
    if body.key_id:
        beneficiary = await _key_account(request, body.key_id)
    elif body.beneficiary:
        beneficiary = str(_wallet(body.beneficiary, hot))
    quote, amount, claim = await _quote(request, body.checks, body.country, body.currency, beneficiary)
    door = get_credits(request)
    mint = door.usdc_mint if body.currency == "USDC" else None
    reference = Pubkey.from_string(quote.reference) if quote.reference else None
    raw = purchase_transaction(
        payer, hot, amount, await door.rpc.latest_blockhash(), quote.memo, mint=mint, reference=reference
    )
    return PurchaseResponse(
        transaction=base64.b64encode(raw).decode(),
        currency=body.currency,
        amount=amount,
        lamports=amount if mint is None else None,
        mint=str(mint) if mint else None,
        hot_wallet=str(hot),
        solana_pay_url=solana_pay_url(hot, amount, quote.memo, mint, reference=quote.reference),
        checks=quote.checks,
        price=_breakdown(quote),
        quote_id=quote.id,
        claim=claim,
    )


@router.get("/v1/credits/quotes/{quote_id}", response_model=QuoteStatusResponse)
async def quote_status(
    request: Request,
    response: Response,
    quote_id: str,
    x_unfret_claim: str | None = Header(default=None, max_length=100),
) -> QuoteStatusResponse:
    """
    Whether a purchase has been paid: for the checkout to show when the checks have arrived, and its invites once.

    No sign-in: the quote's id (from ``purchase-tx`` or the QR code's ``X-Unfret-Quote`` header) is random, so only
    whoever started the purchase knows it until the payment's memo puts it on chain, which shows as much already.
    Says nothing about who pays or who gets the checks. Quotes are deleted a day after they expire (404 then). The
    invite codes need the purchase's claim token too (``X-Unfret-Claim``), which never goes on chain; the first
    response that carries it after payment returns them, and wipes them.

    :param request: The incoming request.
    :param response: The response (marked uncacheable: it changes once paid).
    :param quote_id: The quote's id.
    :param x_unfret_claim: The purchase's claim token (from ``purchase-tx`` or the QR code's ``X-Unfret-Claim``).
    :return: Paid or not, and the pack size.
    :raises ServiceUnavailableError: Without a database.
    :raises UnknownQuoteError: For an unknown, expired or malformed id.
    """
    response.headers["Cache-Control"] = "no-store"
    if request.app.db_pool is None:
        raise ServiceUnavailableError("Buying checks is not available yet.")
    found = None
    if re.fullmatch(vat.QUOTE_ID_PATTERN, quote_id):
        found = await vat.quote_status(request.app.db_pool, quote_id)
    if found is None:
        raise UnknownQuoteError("Unknown quote.")
    paid, checks = found
    codes = await invites.take_from_quote(request.app.db_pool, quote_id, x_unfret_claim) if paid else []
    return QuoteStatusResponse(paid=paid, checks=checks, invites=codes)


@router.get("/v1/credits/solana-pay.svg", response_class=Response)
async def solana_pay_qr(
    request: Request,
    *,
    checks: int = Query(gt=0),
    country: str = Query(min_length=2, max_length=2),
    currency: Currency = Query(default="SOL"),
    beneficiary: str | None = Query(default=None, max_length=64),
    key_id: str | None = Query(default=None, max_length=120),
) -> Response:
    """
    The Solana Pay request for a pack as a QR code, for paying from a phone wallet.

    The price (VAT included) and its breakdown are in the ``X-Unfret-Total-Usd`` and ``X-Unfret-Vat-Rate``
    headers; the quote's id, to poll ``/v1/credits/quotes/{quote_id}`` with, in ``X-Unfret-Quote``, and the claim
    token that collects the pack's invites with that poll in ``X-Unfret-Claim``; the Solana Pay link itself (to open
    a wallet app on the same phone) in ``X-Unfret-Pay-Url``.

    :param request: The incoming request.
    :param checks: The pack size.
    :param country: The buyer's country (ISO 3166 alpha-2), for VAT.
    :param currency: ``SOL`` or ``USDC``.
    :param beneficiary: The wallet to credit; without it the paying wallet is credited.
    :param key_id: The API key to credit instead.
    :return: An SVG image.
    """
    hot = _hot_for_purchase(request, currency)
    target = str(_wallet(beneficiary, hot)) if beneficiary else None
    if key_id:
        target = await _key_account(request, key_id)
    quote, amount, claim = await _quote(request, checks, country, currency, target)
    mint = get_credits(request).usdc_mint if currency == "USDC" else None
    # A standalone SVG document (with the SVG namespace): the page loads it as an <img>, and browsers don't draw an
    # <svg> without xmlns there. svg_inline() leaves the namespace out, since it's meant for embedding in HTML.
    buffer = io.BytesIO()
    pay_url = solana_pay_url(hot, amount, quote.memo, mint, reference=quote.reference)
    segno.make(pay_url, error="m").save(buffer, kind="svg", scale=6, border=2, dark="#111", xmldecl=False)
    svg = buffer.getvalue()
    headers = {
        "Cache-Control": "no-store",
        "X-Unfret-Total-Usd": format_usd(quote.gross_usd_micros),
        "X-Unfret-Vat-Rate": format(quote.vat_rate_bps / 100, "g"),
        "X-Unfret-Quote": quote.id,
        "X-Unfret-Claim": claim,
        # The same request as a link, for a wallet app on the device the page is open on (a phone can't scan itself).
        "X-Unfret-Pay-Url": pay_url,
    }
    return Response(svg, media_type="image/svg+xml", headers=headers)


@router.get("/v1/schema/page-facts")
def page_facts_schema() -> dict[str, Any]:
    """
    The JSON schema of ``PageFacts``, for clients (the browser extension) that extract facts themselves.

    :return: The schema.
    """
    return PageFacts.model_json_schema()


# ── Fiat (API keys only) ──────────────────────────────────────────────────────────────────────────────────────────


class FiatCheckoutRequest(BaseModel):
    """Which key to top up with which pack."""

    key_id: str = Field(max_length=120, description="The API key's id (or the key itself: only the id is used).")
    checks: int = Field(gt=0, description="The pack: one of the pack sizes in /v1/pricing (charged its price_usd).")


class FiatCheckoutResponse(BaseModel):
    """Where to send the buyer, and how to collect the order's invites when they come back."""

    checkout_url: str
    provider: str
    checkout_id: str = Field(description="Poll GET /v1/credits/fiat-checkouts/{checkout_id} (with the claim) for it.")
    claim: str = Field(
        description="Send as X-Unfret-Claim with that poll: the first one after payment returns the pack's invite "
        "codes, once. Keep it in the buyer's browser (e.g. localStorage) across the provider's checkout; it isn't "
        "stored and is never sent to the provider."
    )


class FiatCheckoutStatusResponse(BaseModel):
    """Whether a card checkout has been paid, and its invites once."""

    paid: bool = Field(description="True once the provider's webhook confirmed the payment and the key was credited.")
    checks: int | None = Field(description="The pack size the checkout is for.")
    invites: list[str] = Field(default_factory=list, description=INVITES_DESCRIPTION)


class UnknownCheckoutError(NotFoundError):
    """No such checkout, or not with this claim token."""

    error_code = "UNKNOWN_CHECKOUT"
    headers = {"Cache-Control": "no-store"}


def get_fiat(request: Request) -> FiatProvider | None:
    """
    The fiat provider (Polar), if it is configured.

    :param request: The incoming request.
    :return: The provider, or ``None`` while fiat is off.
    """
    provider: FiatProvider | None = request.app.fiat
    return provider


@router.post("/v1/credits/fiat-checkout", response_model=FiatCheckoutResponse)
async def fiat_checkout(body: FiatCheckoutRequest, request: Request) -> FiatCheckoutResponse:
    """
    Start a card (or other fiat) payment for a pack that tops up an API key, at the provider's hosted checkout.

    The provider is the seller: it handles the buyer's details, invoice and sales tax/VAT; this service only learns
    that the key was paid for. Credited the pack's value when the provider's webhook confirms the payment.

    :param body: The key and the pack.
    :param request: The incoming request.
    :return: The checkout URL.
    :raises ServiceUnavailableError: While no fiat provider is configured, or it can't start a checkout.
    """
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Card payments are not available yet; pay with SOL or USDC.")
    chosen = _pack(body.checks)
    account = await _key_account(request, body.key_id)
    try:
        checkout = await provider.create_checkout(account, chosen, f"{settings.public_base_url}/checkout?paid=1")
    except FiatProviderError as exc:
        raise ServiceUnavailableError(
            "Card payments are unavailable right now; try again later or pay with SOL or USDC."
        ) from exc
    claim = invites.new_claim()
    await fiat.remember_checkout(
        request.app.db_pool, provider.name, checkout, account, checks=chosen.checks, claim_hash=invites.digest(claim)
    )
    return FiatCheckoutResponse(
        checkout_url=checkout.url, provider=provider.name, checkout_id=checkout.checkout_id, claim=claim
    )


@router.get("/v1/credits/fiat-checkouts/{checkout_id}", response_model=FiatCheckoutStatusResponse)
async def fiat_checkout_status(
    request: Request,
    response: Response,
    checkout_id: str,
    x_unfret_claim: str | None = Header(default=None, max_length=100),
) -> FiatCheckoutStatusResponse:
    """
    Whether a card checkout has been paid, and the pack's invite codes once: for the checkout page on the way back.

    Needs the checkout's claim token (``X-Unfret-Claim``, from ``fiat-checkout``): the provider knows the checkout's
    id, but never the token. The first response after payment returns the codes and wipes them. 404 for an unknown
    checkout, a wrong or missing token, or one older than a month.

    :param request: The incoming request.
    :param response: The response (marked uncacheable: it changes once paid).
    :param checkout_id: The checkout's id (from ``fiat-checkout``).
    :param x_unfret_claim: The checkout's claim token.
    :return: Paid or not, the pack size, and the invites the first time.
    :raises ServiceUnavailableError: While card payments are off.
    :raises UnknownCheckoutError: For an unknown checkout or a wrong or missing claim token.
    """
    response.headers["Cache-Control"] = "no-store"
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Card payments are not available yet; pay with SOL or USDC.")
    found = await invites.checkout_status(request.app.db_pool, provider.name, checkout_id[:100], x_unfret_claim)
    if found is None:
        raise UnknownCheckoutError("Unknown checkout.")
    return FiatCheckoutStatusResponse(paid=found.paid, checks=found.checks, invites=found.invites)


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
    outcome = await fiat.apply_event(request.app.db_pool, event, settings)
    # Acknowledged either way (a retry wouldn't change it); an order we can't place needs a person.
    log = logger.error if outcome in {"unknown_account", "unknown_order", "too_small"} else logger.info
    log("fiat_webhook", kind=event.kind, outcome=outcome, provider=event.provider, order_id=event.order_id)
    return {"status": outcome}
