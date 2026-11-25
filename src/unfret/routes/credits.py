"""
Prices, balances and buying credits.

``/v1/pricing``, ``/v1/pricing/topup``, ``/v1/credits``, ``/v1/credits/purchase-tx`` (a top-up with SOL or USDC, VAT
added), and
card top-ups for API keys (``/v1/credits/fiat-checkout`` and the provider's webhook, off until a provider is wired
in).
"""

import base64
import io
import re
import time
from decimal import Decimal
from typing import Any

import segno
import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from solders.pubkey import Pubkey

from unfret import spend, usage
from unfret.auth import get_credits, require_account
from unfret.config import settings
from unfret.credits import claims, fiat, free, keys, ledger, local_methods, vat
from unfret.credits.fiat import FiatProvider, FiatProviderError, FiatWebhookError
from unfret.credits.pricing import pricing, purchase_transaction, solana_pay_url
from unfret.credits.topup import (
    EURO_AREA,
    CardCurrency,
    Topup,
    card_currency,
    format_money,
    label,
    topup,
    valid,
)
from unfret.facts import PageFacts
from unfret.prices import Currency, PriceUnavailableError, format_usd
from unfret.routes.analyze import donate_allowed, donate_link, legacy_free_left, paused_metadata
from unfret.schemas.errors import (
    BadRequestError,
    ConflictError,
    GoneError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from unfret.visitors import RateLimit

logger = structlog.get_logger("unfret")

router = APIRouter(tags=["Credits"])

# Price quotes per visitor network (in memory, hashed): each one is stored and costs an RPC call.
_quotes = RateLimit(settings.quotes_per_ip_per_minute)


def _quote_allowed(request: Request) -> None:
    if (wait := _quotes.retry_after(request.client.host if request.client else "unknown")) is not None:
        raise TooManyRequestsError(
            "Too many purchases started from here; wait a moment.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )


class CreditsResponse(BaseModel):
    """An account's balance."""

    address: str = Field(description="The account: the wallet's address, or key:<id> for an API key.")
    balance_usd_micros: int = Field(description="The balance in micro-dollars (1 USD = 1,000,000).")
    checks_remaining: int = Field(
        description="Checks left: the balance in checks, plus free_checks_remaining (a developer pass's free checks "
        "are api_free_checks_remaining)."
    )
    balance_lamports: int = Field(
        description="Deprecated: the same micro-dollar balance under its old name, kept for older clients."
    )
    free_checks_remaining: int = Field(
        default=0,
        description="For older store clients that check with a pass: the free deep checks this pass may still run "
        "this hour (included in checks_remaining), while it has no bought checks. New clients need no pass (GET "
        "/v1/pricing → free). 0 for a developer pass.",
    )
    free_checks_paused: bool = Field(
        default=False,
        description="Free deep checks are paused: the free pool, or the day's limit on it, is used up for today, "
        "or for a developer pass's (api_free_checks_remaining) today's budget (back at 00:00 UTC either way). Bought "
        "checks and quick checks work.",
    )
    api_free_checks_remaining: int = Field(
        default=0,
        description="A developer pass's free deep checks: they count on POST /v1/check only (the API, the MCP "
        "server, the clients), not on /v1/analyze (the website, the extension, the phone app), so they aren't in "
        "checks_remaining. 0 for any other pass and for a wallet.",
    )


class PurchaseRequest(BaseModel):
    """How much, for whom, paid how, from where."""

    address: str = Field(max_length=64, description="The paying wallet (base58), which signs and sends the transfer.")
    currency: Currency = Field(default="SOL", description="Pay in SOL or USDC.")
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        decimal_places=2,
        description="The top-up in US dollars before VAT, at least /v1/pricing's topup.min_amount: it buys "
        "floor(amount / price per check) checks plus the bonus tier's; VAT for the country is added on top.",
    )
    checks: int | None = Field(
        default=None,
        gt=0,
        description="Deprecated (fixed packs are gone): the checks to buy, priced as checks × the price of a check.",
    )
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
    net_usd_micros: int = Field(description="The price less its VAT (the VAT is worked out of the price).")
    vat_usd_micros: int = Field(description="The VAT included in the price (zero outside the EU).")
    total_usd_micros: int = Field(description="What is charged: the amount before VAT plus the VAT for the country.")
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


class UnknownQuoteError(NotFoundError):
    """No such quote (never was, malformed, or purged a day after it expired)."""

    error_code = "UNKNOWN_QUOTE"
    headers = {"Cache-Control": "no-store"}


class QuoteStatusResponse(BaseModel):
    """Whether a purchase's payment has been credited."""

    paid: bool = Field(description="True once a payment naming this quote has been credited.")
    checks: int = Field(description="The checks the quote buys (with its bonus).")


def _method_document(method: local_methods.LocalMethod) -> dict[str, str]:
    return {"id": method.id, "label": method.label, "currency": method.currency}


@router.get("/v1/pricing")
async def get_pricing(request: Request, response: Response) -> dict[str, Any]:
    """
    The price of a check and everything derived from it.

    The x402 tiers, how top-ups are priced (any amount from the minimum, VAT added), which countries pay by card
    in euros and which have a local payment method, the suggested country and its card currency, the VAT rules for
    crypto, exact USDC amounts and a live SOL quote.

    :param request: The incoming request.
    :param response: The response (marked uncacheable: it carries the visitor's likely country).
    :return: The price list.
    """
    door = get_credits(request)
    document = await pricing(settings, door.hot if door.purchases_enabled else None, door.book, door.usdc_mint)
    country = vat.edge_country({k.lower(): v for k, v in request.headers.items()})
    # Only a suggestion: the page shows it and lets the visitor pick another country, which then decides the card
    # currency and the local method from the two tables below. Never stored or logged.
    document["vat"]["country_hint"] = country
    # The countries whose card prices are in euros (empty while euro prices are off); dollars everywhere else.
    document["euro_area"] = sorted(EURO_AREA) if settings.card_eur_enabled else []
    # Each country's local way to pay (iDEAL | Wero in the Netherlands), offered next to the card.
    document["local_payment_methods"] = {
        country_code: _method_document(method)
        for country_code, method in sorted(local_methods.LOCAL_METHODS.items())
        if local_methods.offered(settings, method)
    }
    # For the suggested country (kept for clients that don't pick a country themselves).
    document["card_currency"] = card_currency(settings, country)
    method = local_methods.local_method(settings, country)
    document["local_payment_method"] = _method_document(method) if method else None
    document["api_keys"] = door.sign_in_enabled
    # The free tier: quick and deep checks are free for shoppers, with no pass (a deep one with a proof of work),
    # while the free pool and the day's limit allow (unfret.spend); a developer pass has its own free deep checks.
    standing = await spend.standing(request.app.db_pool, settings)
    paused = standing.scope is not None
    donate = donate_allowed(request)
    document["free"] = {
        "quick_checks": True,
        "deep_checks": True,
        "paused": paused,
        "back_at": "00:00 UTC",
        # What the free pool still allows today, in deep checks (at the measured average cost of one): euros stay
        # internal. 0 while paused.
        "deep_checks_left_today": await spend.checks_left_today(request.app.db_pool, settings),
        **({"pause": paused_metadata(standing, donate=donate)} if paused else {}),
        "proof_of_work": {"challenge_url": "/v1/analyze/challenge", "bits": settings.deep_pow_bits},
        **({"donate_url": donate_link()} if donate else {}),
        "developer_checks_per_pass": settings.developer_free_checks,
        "checks_per_pass": 0,  # deprecated: a shopper's pass came with free checks; no pass is needed now
    }
    document["fiat"] = {
        "available": get_fiat(request) is not None,
        "min_usd_micros": int(settings.topup_min_amount * 1_000_000),
        "currencies": ["usd", "eur"] if settings.card_eur_enabled else ["usd"],
        "min_eur_cents": int(settings.topup_min_amount * 100) if settings.card_eur_enabled else None,
    }
    document["reports"] = {"per_month": settings.reports_per_month}
    response.headers["Cache-Control"] = "private, no-store"
    return document


class TopupQuote(BaseModel):
    """What an amount buys, and what it costs with VAT, for the checkout to show before anyone pays."""

    currency: CardCurrency
    amount: str = Field(description="Before VAT, in the currency.")
    checks: int = Field(description="All the checks it buys: floor(amount / price per check) plus the bonus.")
    base_checks: int
    bonus_checks: int
    bonus_pct: int
    vat_pct: str | None = Field(
        description="The VAT added at checkout for this country (null: unknown country, or outside the EU for a card, "
        "where the provider may add sales tax)."
    )
    vat: str | None
    total: str | None = Field(description="Amount plus VAT, in the currency (null while the VAT isn't known).")
    label: str = Field(description='E.g. "€100.00 → 1,150 checks (incl. 150 bonus) · €121.00 incl. 21% VAT (NL)".')
    min_amount: str


@router.get("/v1/pricing/topup", response_model=TopupQuote)
async def topup_quote(
    amount: Decimal = Query(gt=0, le=1_000_000, decimal_places=2),
    currency: CardCurrency = Query(default="usd"),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    method: str = Query(default="card", pattern="^(card|crypto)$"),
) -> TopupQuote:
    """
    What a top-up of ``amount`` (before VAT) buys, and its total with the VAT for ``country``.

    For a card the provider adds the country's standard VAT rate in the EU; for crypto the rate is ours
    (``/v1/pricing`` → ``vat``). Nothing is stored. An amount below the minimum still answers (``checks`` as computed),
    so a page can show it while the buyer types; buying it is refused (``AMOUNT_TOO_SMALL``).

    :param amount: The amount before VAT.
    :param currency: ``usd`` or ``eur`` (crypto is always ``usd``).
    :param country: The buyer's country (ISO 3166 alpha-2), once known.
    :param method: ``card`` or ``crypto``.
    :return: The checks, the VAT and the total.
    """
    cur: CardCurrency = "usd" if method == "crypto" else currency
    top = topup(settings, amount, cur)
    where = vat.normalise_country(country) if country else None
    pct: Decimal | None = None
    if where is not None:
        if method == "crypto":
            pct = Decimal(vat.rate_bps(settings, where)) / 100
        elif where in vat.standard_rates():
            pct = vat.standard_rates()[where]
    tax = (top.amount * pct / 100).quantize(Decimal("0.01")) if pct is not None else None
    return TopupQuote(
        currency=cur,
        amount=format(top.amount, "f"),
        checks=top.checks,
        base_checks=top.base_checks,
        bonus_checks=top.bonus_checks,
        bonus_pct=top.bonus_pct,
        vat_pct=format(pct.normalize(), "f") if pct is not None else None,
        vat=format(tax, "f") if tax is not None else None,
        total=format(top.amount + tax, "f") if tax is not None else None,
        label=label(top, pct, where),
        min_amount=format(settings.topup_min_amount, "f"),
    )


@router.get("/v1/credits", response_model=CreditsResponse)
async def get_balance(request: Request, address: str = Depends(require_account)) -> CreditsResponse:
    """
    The balance of the signed-in wallet or the API key.

    :param request: The incoming request.
    :param address: The account (injected by require_account).
    :return: The balance and how many analyses it covers (a developer pass's free checks apart: they are for the
        API).
    """
    pool = request.app.db_pool
    row = await ledger.balance(pool, address)
    balance = row or 0
    key_id = address.removeprefix(keys.ACCOUNT_PREFIX) if keys.is_key_account(address) else None
    api_free = await free.left(pool, key_id, api=True) if key_id is not None else 0
    developer = key_id is not None and bool(
        await pool.fetchval("SELECT developer FROM api_keys WHERE id = $1", key_id)
    )
    # An older store client checks with its pass, and offers a deep check only while this says there are checks:
    # the free deep checks its pass may still run this hour (see analyze._free_gate), paused at the spend ceiling.
    # Only while it has no bought checks: those pay first, and are what it shows then.
    ceiling = (await spend.standing(pool, settings)).scope is not None
    bought = max(balance, 0) // settings.analysis_price_usd_micros
    legacy = 0 if developer or bought else legacy_free_left(address)
    return CreditsResponse(
        address=address,
        balance_usd_micros=balance,
        checks_remaining=bought + legacy,
        balance_lamports=balance,
        free_checks_remaining=legacy,
        free_checks_paused=(bool(legacy) and ceiling) or (bool(api_free) and await free.paused(pool, settings)),
        api_free_checks_remaining=api_free,
    )


def _hot_for_purchase(request: Request, currency: Currency = "SOL") -> Pubkey:
    door = get_credits(request)
    enabled = door.usdc_purchases_enabled if currency == "USDC" else door.purchases_enabled
    if not enabled or door.hot is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Buying checks is not available yet.")
    return door.hot


def _topup(amount: Decimal | None, checks: int | None, currency: str) -> Topup:
    """
    The top-up a request asks for: an amount before VAT, or (older clients) a number of checks at the check's price.

    :param amount: The amount before VAT, in the currency.
    :param checks: Deprecated: checks to buy.
    :param currency: ``usd`` or ``eur``.
    :return: The top-up.
    :raises BadRequestError: Without an amount, or below the minimum (``AMOUNT_TOO_SMALL``).
    """
    if amount is None and checks is not None:
        amount = Decimal(checks) * (
            settings.price_per_check_eur if currency == "eur" else settings.price_per_check_usd
        )
    if amount is None:
        raise BadRequestError("Say how much: amount, before VAT.", error_code="AMOUNT_REQUIRED")
    if not valid(settings, amount.quantize(Decimal("0.01"))):
        least = format_money(settings.topup_min_amount, currency)
        raise BadRequestError(
            f"A top-up is at least {least} before VAT.",
            error_code="AMOUNT_TOO_SMALL",
            metadata={"min_amount": format(settings.topup_min_amount, "f"), "currency": currency},
        )
    return topup(settings, amount, currency)


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
    request: Request,
    top: Topup,
    country: str,
    currency: Currency,
    beneficiary: str | None,
) -> tuple[vat.Quote, int]:
    """Price and store a top-up for ``country`` (VAT on top), and convert its total into ``currency``'s units."""
    edge = vat.edge_country({k.lower(): v for k, v in request.headers.items()})
    quote = vat.price_quote(
        settings,
        top.checks,
        top.amount_cents * 10_000,
        _country(country),
        edge=edge,
        beneficiary=beneficiary,
    )
    try:
        amount = await get_credits(request).book.amount(quote.gross_usd_micros, currency)
    except PriceUnavailableError as exc:
        raise ServiceUnavailableError("SOL can't be priced right now; pay in USDC or try again shortly.") from exc
    await vat.save_quote(request.app.db_pool, quote)
    return quote, amount


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
    Build the unsigned SOL or USDC transfer that tops up an account; the buyer's wallet signs and sends it.

    The buyer names an amount before VAT; the VAT for their country (see ``vat`` in ``/v1/pricing``) is added and
    how much of it is VAT. The transaction's memo names the stored quote. Credits arrive once the transfer is finalized
    (about 15–30 seconds): everything that arrived, credited to the paying wallet unless ``beneficiary`` or ``key_id``
    says otherwise.
    No sign-in needed: anyone may fund any wallet or API key.

    :param body: How much, for whom, paid how, from where.
    :param request: The incoming request.
    :return: The transaction (base64), a Solana Pay link for phone wallets, and the price breakdown.
    """
    _quote_allowed(request)
    hot = _hot_for_purchase(request, body.currency)
    payer = _wallet(body.address, hot)
    beneficiary: str | None = None
    if body.key_id:
        beneficiary = await _key_account(request, body.key_id)
    elif body.beneficiary:
        beneficiary = str(_wallet(body.beneficiary, hot))
    top = _topup(body.amount, body.checks, "usd")
    quote, amount = await _quote(request, top, body.country, body.currency, beneficiary)
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
    )


@router.get("/v1/credits/quotes/{quote_id}", response_model=QuoteStatusResponse)
async def quote_status(
    request: Request,
    response: Response,
    quote_id: str,
) -> QuoteStatusResponse:
    """
    Whether a purchase has been paid: for the checkout to show when the checks have arrived.

    No sign-in: the quote's id (from ``purchase-tx`` or the QR code's ``X-Unfret-Quote`` header) is random, so only
    whoever started the purchase knows it until the payment's memo puts it on chain, which shows as much already.
    Says nothing about who pays or who gets the checks. Quotes are deleted a day after they expire (404 then).

    :param request: The incoming request.
    :param response: The response (marked uncacheable: it changes once paid).
    :param quote_id: The quote's id.
    :return: Paid or not, and the checks the quote buys.
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
    return QuoteStatusResponse(paid=paid, checks=checks)


@router.get("/v1/credits/solana-pay.svg", response_class=Response)
async def solana_pay_qr(
    request: Request,
    *,
    amount: Decimal | None = Query(default=None, gt=0, decimal_places=2),
    checks: int | None = Query(default=None, gt=0),
    country: str = Query(min_length=2, max_length=2),
    currency: Currency = Query(default="SOL"),
    beneficiary: str | None = Query(default=None, max_length=64),
    key_id: str | None = Query(default=None, max_length=120),
) -> Response:
    """
    The Solana Pay request for a top-up as a QR code, for paying from a phone wallet.

    The total (VAT added) and its breakdown are in the ``X-Unfret-Total-Usd`` and ``X-Unfret-Vat-Rate``
    headers; the quote's id, to poll ``/v1/credits/quotes/{quote_id}`` with, in ``X-Unfret-Quote``; the Solana Pay
    link itself (to open a wallet app on the same phone) in ``X-Unfret-Pay-Url``.

    :param request: The incoming request.
    :param amount: The top-up in US dollars before VAT.
    :param checks: Deprecated: checks to buy (priced at the check's price).
    :param country: The buyer's country (ISO 3166 alpha-2), for VAT.
    :param currency: ``SOL`` or ``USDC``.
    :param beneficiary: The wallet to credit; without it the paying wallet is credited.
    :param key_id: The API key to credit instead.
    :return: An SVG image.
    """
    _quote_allowed(request)
    hot = _hot_for_purchase(request, currency)
    target = str(_wallet(beneficiary, hot)) if beneficiary else None
    if key_id:
        target = await _key_account(request, key_id)
    quote, amount_due = await _quote(request, _topup(amount, checks, "usd"), country, currency, target)
    mint = get_credits(request).usdc_mint if currency == "USDC" else None
    # A standalone SVG document (with the SVG namespace): the page loads it as an <img>, and browsers don't draw an
    # <svg> without xmlns there. svg_inline() leaves the namespace out, since it's meant for embedding in HTML.
    buffer = io.BytesIO()
    pay_url = solana_pay_url(hot, amount_due, quote.memo, mint, reference=quote.reference)
    segno.make(pay_url, error="m").save(buffer, kind="svg", scale=6, border=2, dark="#111", xmldecl=False)
    svg = buffer.getvalue()
    headers = {
        "Cache-Control": "no-store",
        "X-Unfret-Total-Usd": format_usd(quote.gross_usd_micros),
        "X-Unfret-Vat-Rate": format(quote.vat_rate_bps / 100, "g"),
        "X-Unfret-Quote": quote.id,
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
    """Which key to top up with how much."""

    key_id: str = Field(max_length=120, description="The API key's id (or the key itself: only the id is used).")
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        decimal_places=2,
        description="The top-up before VAT, in the checkout's currency, at least /v1/pricing's topup.min_amount: "
        "floor(amount / price per check) checks plus the bonus tier's. The provider adds VAT/sales tax at checkout.",
    )
    checks: int | None = Field(
        default=None,
        gt=0,
        description="Deprecated (fixed packs are gone): the checks to buy, priced as checks × the price of a check.",
    )
    currency: CardCurrency | None = Field(
        default=None,
        description="usd or eur (Polar then offers iDEAL | Wero to "
        "Dutch buyers once they choose the Netherlands as billing country). Default: /v1/pricing's card_currency for "
        "this visitor (eur from the euro area), or the payment method's currency.",
    )
    payment_method: str = Field(
        default=local_methods.CARD,
        max_length=32,
        description="card (default: the buyer picks their country on the provider's page), or a local method's id "
        "from /v1/pricing's local_payment_methods for the buyer's chosen country (e.g. wero: iDEAL | Wero, a EUR "
        "checkout with the Netherlands prefilled as billing country). A local method is charged in its own currency.",
    )

    @field_validator("currency", "payment_method", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        """
        Accept ``EUR`` as well as ``eur`` (and ``Wero`` as ``wero``).

        :param value: The raw value.
        :return: The value, lower case if it is a string.
        """
        return value.lower() if isinstance(value, str) else value

    @field_validator("payment_method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        """
        Only ``card`` or a listed local method.

        :param value: The method id.
        :return: The id.
        :raises ValueError: For an unknown method.
        """
        if value not in local_methods.method_ids():
            raise ValueError(f"payment_method must be one of {', '.join(local_methods.method_ids())}")
        return value


class FiatCheckoutResponse(BaseModel):
    """Where to send the buyer, and how to see the order paid when they come back."""

    checkout_url: str
    provider: str
    currency: CardCurrency = Field(description="The checkout's currency: usd or eur.")
    payment_method: str = Field(
        description="The checkout's method: the one asked for, or card if the provider refused it and the dollar "
        "card checkout was made instead."
    )
    checkout_id: str = Field(description="Poll GET /v1/credits/fiat-checkouts/{checkout_id} (with the claim) for it.")
    claim: str = Field(
        description="Send as X-Unfret-Claim with that poll. Keep it in the buyer's browser (e.g. localStorage) "
        "across the provider's checkout; only its hash is stored, and it is never sent to the provider."
    )


class FiatCheckoutStatusResponse(BaseModel):
    """Whether a card checkout has been paid."""

    paid: bool = Field(description="True once the provider's webhook confirmed the payment and the key was credited.")
    checks: int | None = Field(description="The checks the checkout buys.")
    reissued: bool = Field(
        default=False,
        description="True when the purchase was picked up in another browser (POST /v1/credits/fiat-checkouts/"
        "{checkout_id}/pass): the key this browser made for it was replaced and no longer works.",
    )


class ReissuedPassResponse(BaseModel):
    """A card purchase picked up in another browser: a fresh pass (API key) for it, shown this once."""

    key_id: str = Field(description="The key's public id (unchanged).")
    key: str = Field(description="The new key (the pass). Send as Authorization: Bearer <key>.")
    recovery_code: str = Field(description="The new recovery code (the backup code).")
    account: str = Field(description="The ledger account, key:<id>.")
    checks: int | None = Field(description="The pack size the checkout paid for.")
    checks_remaining: int = Field(description="The key's balance in checks.")


class CheckoutNotPaidError(ConflictError):
    """The checkout exists, but its payment hasn't been confirmed (yet)."""

    error_code = "CHECKOUT_NOT_PAID"
    headers = {"Cache-Control": "no-store", "Retry-After": "3"}


class CheckoutToppedUpError(ConflictError):
    """The checkout added checks to a pass that existed before it: that pass is never replaced from here."""

    error_code = "CHECKOUT_TOPPED_UP"
    headers = {"Cache-Control": "no-store"}


class CheckoutCollectedError(ConflictError):
    """The purchase was already picked up, in the browser that paid or by an earlier reissue."""

    error_code = "CHECKOUT_COLLECTED"
    headers = {"Cache-Control": "no-store"}


class CheckoutExpiredError(GoneError):
    """The payment is older than the reissue window."""

    error_code = "CHECKOUT_EXPIRED"
    headers = {"Cache-Control": "no-store"}


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
    Start a card (or other fiat) payment that tops up an API key by an amount, at the provider's hosted checkout.

    The provider is the seller: it handles the buyer's details, invoice and sales tax/VAT; this service only learns
    that the key was paid for. Credited the checks the amount buys (with its bonus) when the provider's webhook
    confirms the payment.

    In euros for ``currency: "eur"`` (by default for visitors from the euro area), in dollars otherwise. A local
    payment method (``payment_method``, e.g. ``wero``) is a checkout in its currency with its billing country
    prefilled, which is what makes the provider show it. Should the provider refuse a euro checkout (e.g. a product
    without its euro price yet), the buyer gets the dollar card checkout instead of none; ``currency`` and
    ``payment_method`` in the response say which it is.

    :param body: The key, the amount, the currency and the payment method.
    :param request: The incoming request.
    :return: The checkout URL.
    :raises ServiceUnavailableError: While no fiat provider is configured, or it can't start a checkout.
    :raises BadRequestError: For euros while euro prices are off, or a method in another currency than asked for.
    """
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Card payments are not available yet; pay with SOL or USDC.")
    method = local_methods.method_by_id(body.payment_method)  # None: card
    if method is not None:
        if not local_methods.offered(settings, method):
            raise BadRequestError(f"{method.label} isn't available: card payments are in US dollars only.")
        if body.currency not in (None, method.currency):
            raise BadRequestError(f"{method.label} is paid in {method.currency.upper()}.")
        currency: CardCurrency = method.currency
    else:
        currency = body.currency or card_currency(
            settings, vat.edge_country({k.lower(): v for k, v in request.headers.items()})
        )
    if currency == "eur" and not settings.card_eur_enabled:
        raise BadRequestError("Card payments are in US dollars only.")
    chosen = _topup(body.amount, body.checks, currency)
    account = await _key_account(request, body.key_id)
    # The provider fills in the checkout's id: back in another browser (a bank app, a phone), which has neither the
    # pass nor the claim token, /checkout can still pick the purchase up (fiat_checkout_pass).
    return_url = f"{settings.public_base_url}/checkout?paid=1&checkout={provider.checkout_id_placeholder}"
    checkout, paid_with = None, body.payment_method
    # As asked first, then a dollar card checkout rather than no checkout.
    attempts = {(currency, method.billing_country if method else None): body.payment_method, ("usd", None): "card"}
    for (attempt, billing_country), attempt_method in attempts.items():
        try:
            top = chosen if attempt == chosen.currency else topup(settings, chosen.amount, attempt)
            checkout = await provider.create_checkout(account, top, return_url, attempt, billing_country)
        except FiatProviderError:
            if attempt != "usd" or billing_country:
                logger.warning("fiat_checkout_eur_fallback", checks=chosen.checks, payment_method=attempt_method)
            continue
        currency, paid_with = attempt, attempt_method
        break
    if checkout is None:
        raise ServiceUnavailableError(
            "Card payments are unavailable right now; try again later or pay with SOL or USDC."
        )
    claim = claims.new_claim()
    await fiat.remember_checkout(
        request.app.db_pool, provider.name, checkout, account, checks=chosen.checks, claim_hash=claims.digest(claim)
    )
    return FiatCheckoutResponse(
        checkout_url=checkout.url,
        provider=provider.name,
        currency=currency,
        payment_method=paid_with,
        checkout_id=checkout.checkout_id,
        claim=claim,
    )


@router.get("/v1/credits/fiat-checkouts/{checkout_id}", response_model=FiatCheckoutStatusResponse)
async def fiat_checkout_status(
    request: Request,
    response: Response,
    checkout_id: str,
    x_unfret_claim: str | None = Header(default=None, max_length=100),
) -> FiatCheckoutStatusResponse:
    """
    Whether a card checkout has been paid: for the checkout page on the way back.

    Needs the checkout's claim token (``X-Unfret-Claim``, from ``fiat-checkout``): the provider knows the checkout's
    id, but never the token. 404 for an unknown checkout, a wrong or missing token, or one older than a month.

    :param request: The incoming request.
    :param response: The response (marked uncacheable: it changes once paid).
    :param checkout_id: The checkout's id (from ``fiat-checkout``).
    :param x_unfret_claim: The checkout's claim token.
    :return: Paid or not, and the checks the quote buys.
    :raises ServiceUnavailableError: While card payments are off.
    :raises UnknownCheckoutError: For an unknown checkout or a wrong or missing claim token.
    """
    response.headers["Cache-Control"] = "no-store"
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Card payments are not available yet; pay with SOL or USDC.")
    found = await claims.checkout_status(request.app.db_pool, provider.name, checkout_id[:100], x_unfret_claim)
    if found is None:
        raise UnknownCheckoutError("Unknown checkout.")
    return FiatCheckoutStatusResponse(paid=found.paid, checks=found.checks, reissued=found.reissued)


@router.post("/v1/credits/fiat-checkouts/{checkout_id}/pass", response_model=ReissuedPassResponse)
async def fiat_checkout_pass(request: Request, response: Response, checkout_id: str) -> ReissuedPassResponse:
    """
    Pick up a card purchase in another browser: a fresh pass (API key) for the key it paid for.

    For a buyer whose bank app or phone brought them back to a browser without the pass that was made for the
    purchase. The provider's return link names the checkout (``/checkout?paid=1&checkout=<id>``). Allowed once,
    within 24 hours of the payment, and only while the browser that paid hasn't picked the purchase up itself (a
    status poll with its claim token after payment); the key is rotated: a new key and recovery code, the same id
    and balance, and the old key stops working. Only for a pass made for the purchase: a top-up of a pass that had
    checks before (or got some since, from another card order) is never rotated from here.

    Refusals: 404 ``UNKNOWN_CHECKOUT``; 409 ``CHECKOUT_NOT_PAID`` (not confirmed yet: retry after a few seconds);
    409 ``CHECKOUT_TOPPED_UP`` (the checks went to a pass in use: nothing to collect here); 409
    ``CHECKOUT_COLLECTED`` (picked up already); 410 ``CHECKOUT_EXPIRED``; 429 (too many attempts from here).

    :param request: The incoming request.
    :param response: The response (marked uncacheable).
    :param checkout_id: The checkout's id, from the return link.
    :return: The new pass, shown this once.
    :raises ServiceUnavailableError: While card payments are off.
    :raises TooManyRequestsError: If this network tried too many times today.
    :raises UnknownCheckoutError: For an unknown checkout.
    :raises CheckoutNotPaidError: While the payment isn't confirmed.
    :raises CheckoutToppedUpError: When the purchase topped up a pass in use.
    :raises CheckoutCollectedError: When the purchase was picked up already.
    :raises CheckoutExpiredError: After the window.
    """
    response.headers["Cache-Control"] = "no-store"
    provider = get_fiat(request)
    if provider is None or request.app.db_pool is None:
        raise ServiceUnavailableError("Card payments are not available yet.")
    door = get_credits(request)
    if not door.key_guard.recoveries.allow(request.client.host if request.client else "unknown"):
        raise TooManyRequestsError("Too many attempts from here today; try again tomorrow.")
    try:
        reissued = await fiat.reissue_pass(request.app.db_pool, provider.name, checkout_id[:100])
    except fiat.ReissueError as exc:
        if exc.reason == "not_paid":
            raise CheckoutNotPaidError("The payment isn't confirmed yet; try again in a few seconds.") from exc
        if exc.reason == "topped_up":
            raise CheckoutToppedUpError("These checks were added to your pass; open Unfret where you use it.") from exc
        if exc.reason == "collected":
            raise CheckoutCollectedError("These checks were already picked up, in the browser you paid from.") from exc
        if exc.reason == "expired":
            raise CheckoutExpiredError("This purchase is too old to pick up here; use your recovery code.") from exc
        raise UnknownCheckoutError("Unknown checkout.") from exc
    logger.info("fiat_pass_reissued")
    balance = await keys.balance(request.app.db_pool, reissued.key.key_id)
    return ReissuedPassResponse(
        key_id=reissued.key.key_id,
        key=reissued.key.key,
        recovery_code=reissued.key.recovery_code,
        account=reissued.key.account,
        checks=reissued.checks,
        checks_remaining=max(balance, 0) // settings.analysis_price_usd_micros,
    )


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
    if outcome == "credited":
        usage.record("payment", method="card", purpose="pass", provider=event.provider, currency=event.currency)
    return {"status": outcome}
