"""
``POST /v1/check``: pay-per-check for AI shopping agents over x402 (v2, ``exact`` scheme, wSOL or USDC on Solana).

Flow (``upfront``): no ``PAYMENT-SIGNATURE`` header → 402 with ``PAYMENT-REQUIRED``. With one: verify it strictly,
fetch the page if only a URL was given (before any money moves), co-sign as fee payer (the hot wallet, which is also
``payTo``), submit and confirm, then run the analysis and answer with the verdict and ``PAYMENT-RESPONSE``. Nothing
about the payment or the page is stored.
"""

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from veridex import __version__
from veridex.config import settings
from veridex.credits import ledger
from veridex.net import fetch_public
from veridex.prices import format_usd
from veridex.shopper import pick_language
from veridex.x402.payment import PaymentError, encode_header
from veridex.x402.service import X402, DuplicateSettlementError, SettlementMismatchError
from veridex.x402.solana import WSOL_MINT, RpcError, SettlementPendingError, usdc_mint
from veridex.x402.verdict import POLICY_THRESHOLDS, QUICK_SKILLS, Policy, Tier, Verdict, to_verdict

logger = structlog.get_logger("veridex")

router = APIRouter(tags=["x402"])

PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
PAYMENT_RESPONSE = "PAYMENT-RESPONSE"


class CheckRequest(BaseModel):
    """What to check, how deeply, and how cautious the caller wants the action to be."""

    url: str = Field(description="URL of the store or product page.")
    page_content: str | None = Field(
        default=None,
        max_length=settings.max_page_bytes,
        description="The page's HTML, if the agent has it (required for marketplaces that block server fetches).",
    )
    tier: Tier = "quick"
    policy: Policy = "balanced"


def get_x402(request: Request) -> X402:
    """
    Return the app's x402 service, creating it from settings on first use.

    :param request: The incoming request.
    :return: The service.
    """
    app = request.app
    if getattr(app, "x402", None) is None:
        app.x402 = X402(settings)
    service: X402 = app.x402
    return service


def _resource() -> dict[str, Any]:
    return {
        "url": f"{settings.public_base_url}/v1/check",
        "description": "Scam, dropshipping and dark-pattern risk check of a store or product page",
        "mimeType": "application/json",
        "serviceName": "Veridex",
    }


def _payment_required(accepts: list[dict[str, Any]], error: str) -> JSONResponse:
    body = {"x402Version": 2, "error": error, "resource": _resource(), "accepts": accepts}
    return JSONResponse(body, status_code=402, headers={PAYMENT_REQUIRED: encode_header(body)})


def _settlement(x402: X402, *, success: bool, transaction: str, payer: str, reason: str | None = None) -> str:
    response: dict[str, Any] = {
        "success": success,
        "transaction": transaction,
        "network": x402.network,
        "payer": payer,
    }
    if reason:
        response["errorReason"] = reason
    return encode_header(response)


async def _refund_as_credits(request: Request, payer: str, usd_micros: int) -> int:
    """
    Credit a paid-but-failed check's price to the paying wallet's credits balance.

    :param request: The incoming request (for the database pool).
    :param payer: The paying wallet (base58).
    :param usd_micros: The price paid (the tier's dollar price, whichever currency paid it).
    :return: The micro-dollars refunded, or 0 if it couldn't be recorded (logged for manual follow-up).
    """
    pool = request.app.db_pool
    if pool is None:
        logger.error("x402_refund_unrecorded", reason="no_database", usd_micros=usd_micros)
        return 0
    try:
        await ledger.credit(pool, payer, usd_micros)
    except Exception as exc:  # noqa: BLE001 - the answer must still go out; the log has what's needed to fix it
        logger.error("x402_refund_unrecorded", error=type(exc).__name__, usd_micros=usd_micros)
        return 0
    logger.info("x402_refunded_as_credits", usd_micros=usd_micros)
    return usd_micros


async def _page_html(body: CheckRequest) -> str:
    if body.page_content:
        return body.page_content
    raw = await fetch_public(body.url, max_bytes=settings.max_page_bytes)
    return raw.decode("utf-8", errors="replace")


@router.post("/v1/check", response_model=Verdict, responses={402: {"description": "Payment required (x402)"}})
async def check(body: CheckRequest, request: Request) -> Response:  # noqa: C901, PLR0911, PLR0912
    """
    Check a store or product page for scam, dropshipping and dark-pattern signals; paid per call over x402.

    :param body: URL, optional page HTML, tier and policy.
    :param request: The incoming request (payment header, app state).
    :return: The verdict, or an x402 payment-required / error response.
    """
    x402 = get_x402(request)
    health = await x402.health()
    if health.status != "ok":
        return JSONResponse(
            {"detail": "Payments are not available right now.", "error_code": "SERVICE_UNAVAILABLE"}, status_code=503
        )

    accepts = await x402.accepts(body.tier)
    if not accepts:
        return JSONResponse(
            {"detail": "No payment option can be priced right now; retry shortly.", "error_code": "PRICE_UNAVAILABLE"},
            status_code=503,
            headers={"Retry-After": "30"},
        )
    header = request.headers.get(PAYMENT_SIGNATURE)
    if not header:
        return _payment_required(accepts, f"{PAYMENT_SIGNATURE} header is required")

    try:
        pre = x402.precheck(header, body.tier, accepts)
    except PaymentError as exc:
        if exc.malformed:
            return JSONResponse({"detail": exc.reason, "error_code": "INVALID_PAYMENT"}, status_code=400)
        return _payment_required(accepts, exc.reason)

    # Get the page before any money moves, so a page we can't read costs the caller nothing.
    try:
        html = await _page_html(body)
    except Exception as exc:  # noqa: BLE001
        logger.info("check_page_fetch_failed", error=type(exc).__name__)
        return JSONResponse(
            {
                "detail": "Could not fetch the page; send page_content instead. You were not charged.",
                "error_code": "PAGE_UNAVAILABLE",
            },
            status_code=422,
        )

    graph = request.app.graph if body.tier == "deep" else request.app.quick_graph
    if graph is None:
        return JSONResponse({"detail": "Analysis is not available.", "error_code": "SERVICE_UNAVAILABLE"}, 503)

    payer = str(pre.payer)
    try:
        settlement = await x402.settle(pre)
    except PaymentError as exc:  # the simulation showed a different outcome; nothing was submitted
        return _payment_required(accepts, exc.reason)
    except DuplicateSettlementError:
        return _payment_required(accepts, "duplicate_settlement")
    except SettlementMismatchError as exc:
        # It landed, but not as the simulation promised: no analysis for this payment.
        headers = {
            PAYMENT_RESPONSE: _settlement(
                x402, success=False, transaction=exc.signature, payer=payer, reason="settlement_mismatch"
            )
        }
        return JSONResponse({"detail": "The payment did not settle as required."}, 402, headers)
    except SettlementPendingError as exc:
        headers = {
            PAYMENT_RESPONSE: _settlement(
                x402, success=False, transaction=exc.signature, payer=payer, reason="settlement_pending"
            )
        }
        return JSONResponse({"detail": "Payment submitted but not yet confirmed; retry later."}, 402, headers)
    except RpcError as exc:
        logger.info("x402_settlement_failed", error=str(exc)[:200])
        headers = {
            PAYMENT_RESPONSE: _settlement(x402, success=False, transaction="", payer=payer, reason="settlement_failed")
        }
        return JSONResponse({"detail": "Payment could not be settled; you were not charged."}, 402, headers)

    signature = settlement.signature
    paid = {PAYMENT_RESPONSE: _settlement(x402, success=True, transaction=signature, payer=payer)}
    try:
        async with asyncio.timeout(settings.analysis_timeout_s):
            language = pick_language(request.headers.get("accept-language"))
            result = await graph.ainvoke({"page_content": html, "url": body.url, "flags": [], "language": language})
    except Exception as exc:  # noqa: BLE001
        # The payment settled but the analysis failed: refund it as credits to the paying wallet (never an
        # outgoing transfer: the hot wallet only ever sends to cold), usable after signing in at /connect.
        logger.warning("check_analysis_failed_after_payment", error=type(exc).__name__)
        timed_out = isinstance(exc, TimeoutError)
        content: dict[str, Any] = {
            "detail": "Payment settled, but the analysis " + ("timed out." if timed_out else "failed."),
            "error_code": "GATEWAY_TIMEOUT" if timed_out else "INTERNAL_ERROR",
            "transaction": signature,
        }
        # The tier's dollar price, whichever currency paid for it (credit balances are micro-dollars).
        refunded = await _refund_as_credits(request, payer, x402.usd_price(body.tier))
        if refunded:
            content["refunded_usd_micros"] = refunded
            content["refunded_lamports"] = refunded  # deprecated name of the same micro-dollar credit
            content["refund"] = f"Credited to {payer}; sign in with that wallet at {settings.public_base_url}/connect."
        return JSONResponse(content, status_code=504 if timed_out else 500, headers=paid)

    verdict = to_verdict(result, policy=body.policy, tier=body.tier)
    return JSONResponse(verdict.model_dump(mode="json"), headers=paid)


def _discovery() -> dict[str, Any]:
    mint = usdc_mint(settings.solana_network, settings.usdc_mint)
    return {
        "x402Version": 2,
        "service": "Veridex",
        "version": __version__,
        "endpoint": f"{settings.public_base_url}/v1/check",
        "method": "POST",
        "scheme": "exact",
        "network": settings.solana_network,
        "pricing": "canonical in USD",
        "assets": {
            "USDC": {"mint": str(mint), "pricing": "exactly the USD price (1 USDC = $1)"},
            "wSOL": {
                "mint": str(WSOL_MINT),
                "pricing": "the USD price converted at quote time with Pyth's SOL/USD price; a payment may differ "
                f"from the quote by up to {settings.quote_tolerance_bps / 100:g}%; offered only while the oracle is "
                "fresh, otherwise pay in USDC",
            },
        },
        "feePayer": "Veridex pays the network fee",
        "wallets": "standard wallets and smart wallets (Squads and other program-controlled wallets paying by CPI)",
        "tiers": {
            "quick": {
                "price_usd": format_usd(settings.x402_price_quick_usd_micros),
                "price_usd_micros": settings.x402_price_quick_usd_micros,
                "skills": sorted(QUICK_SKILLS),
            },
            "deep": {
                "price_usd": format_usd(settings.x402_price_deep_usd_micros),
                "price_usd_micros": settings.x402_price_deep_usd_micros,
                "skills": "all",
            },
        },
        "policies": {name: {"warn_at": w, "block_at": b} for name, (w, b) in POLICY_THRESHOLDS.items()},
        "request": {
            "url": "string",
            "page_content": "string (optional)",
            "tier": "quick|deep",
            "policy": "strict|balanced|lenient",
        },
        "response": {
            "action": "allow|warn|block (warn = ask your human)",
            "risk_score": "0..1",
            "confidence": "0..1",
            "flags": [{"type": "string", "evidence": "string", "weight": ">= 0 (contribution to the risk)"}],
            "policy_applied": "string",
            "tier": "string",
            "cached": "bool",
            "checked_at": "ISO-8601",
            "model_version": "string|null (the scorer model that set risk_score)",
        },
    }


@router.get("/.well-known/x402", include_in_schema=False)
async def discovery() -> JSONResponse:
    """
    Describe the paid endpoint, its tiers and prices for x402 directories and agents.

    :return: The discovery document.
    """
    return JSONResponse(_discovery())


@router.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
async def llms_txt() -> str:
    """
    Describe the service for language-model agents.

    :return: Plain text.
    """
    d = _discovery()
    quick, deep = d["tiers"]["quick"]["price_usd"], d["tiers"]["deep"]["price_usd"]
    return f"""# Veridex

> Pay-per-check risk signals for AI shopping agents: is this store or product listing showing signs of scams,
> dropshipping of generic goods, fake reviews or dark patterns? Returns evidence and an action, never a "scam" label.

## Endpoint

POST {d["endpoint"]} with JSON {{"url": "...", "page_content": "<html> (optional)", "tier": "quick|deep",
"policy": "strict|balanced|lenient"}}. Send page_content for marketplaces (Amazon blocks server-side fetches).

## Payment (x402 v2)

The first call answers 402 with a PAYMENT-REQUIRED header listing the options: wSOL (wrapped SOL) and USDC. Pay one
with the `exact` scheme on Solana ({d["network"]}) and retry with PAYMENT-SIGNATURE. Prices are in US dollars:
quick = {quick}, deep = {deep}. USDC pays exactly that; the wSOL amount is the dollar price converted at quote time
with Pyth's SOL/USD price (wSOL isn't offered while the oracle is stale). Standard wallets and
smart wallets (Squads and other program-controlled wallets paying by CPI) both work, as long as the transaction
carries one memo and pays the quoted amount into payTo's token account for that mint. Veridex is the fee payer, so
you only pay the price. A page we can't fetch costs nothing.

## Response

{{"action": "allow|warn|block", "risk_score": 0..1, "confidence": 0..1, "flags": [{{"type", "evidence", "weight"}}],
"policy_applied", "tier", "cached", "checked_at", "model_version"}}. "warn" means: ask your human before buying.

## Machine-readable

{settings.public_base_url}/.well-known/x402
"""
