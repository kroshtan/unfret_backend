"""
``POST /v1/check``: pay-per-check for AI shopping agents over x402 (v2, ``exact`` scheme, wSOL on Solana).

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
from veridex.net import fetch_public
from veridex.x402.payment import PaymentError, encode_header, verify_payment
from veridex.x402.service import X402, DuplicateSettlementError
from veridex.x402.solana import RpcError, SettlementPendingError
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


def _payment_required(request: Request, x402: X402, tier: Tier, error: str) -> JSONResponse:
    body = {"x402Version": 2, "error": error, "resource": _resource(), "accepts": [x402.requirements(tier)]}
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


async def _page_html(body: CheckRequest) -> str:
    if body.page_content:
        return body.page_content
    raw = await fetch_public(body.url, max_bytes=settings.max_page_bytes)
    return raw.decode("utf-8", errors="replace")


@router.post("/v1/check", response_model=Verdict, responses={402: {"description": "Payment required (x402)"}})
async def check(body: CheckRequest, request: Request) -> Response:  # noqa: C901, PLR0911
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

    header = request.headers.get(PAYMENT_SIGNATURE)
    if not header:
        return _payment_required(request, x402, body.tier, f"{PAYMENT_SIGNATURE} header is required")

    assert x402.hot is not None  # noqa: S101
    try:
        payment = verify_payment(
            header,
            x402.requirements(body.tier),
            pay_to=x402.hot.pubkey,
            fee_payer=x402.hot.pubkey,
            max_compute_units=settings.x402_max_compute_units,
            max_compute_unit_price=settings.x402_max_compute_unit_price_microlamports,
        )
    except PaymentError as exc:
        if exc.malformed:
            return JSONResponse({"detail": exc.reason, "error_code": "INVALID_PAYMENT"}, status_code=400)
        return _payment_required(request, x402, body.tier, exc.reason)

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

    payer = str(payment.payer)
    try:
        signature = await x402.settle(payment)
    except DuplicateSettlementError:
        return _payment_required(request, x402, body.tier, "duplicate_settlement")
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

    paid = {PAYMENT_RESPONSE: _settlement(x402, success=True, transaction=signature, payer=payer)}
    try:
        async with asyncio.timeout(settings.analysis_timeout_s):
            result = await graph.ainvoke({"page_content": html, "url": body.url, "flags": []})
    except Exception as exc:  # noqa: BLE001
        # The payment settled but the analysis failed. Say so plainly; refunds are a separate decision.
        logger.warning("check_analysis_failed_after_payment", error=type(exc).__name__)
        timed_out = isinstance(exc, TimeoutError)
        return JSONResponse(
            {
                "detail": "Payment settled, but the analysis " + ("timed out." if timed_out else "failed."),
                "error_code": "GATEWAY_TIMEOUT" if timed_out else "INTERNAL_ERROR",
                "transaction": signature,
            },
            status_code=504 if timed_out else 500,
            headers=paid,
        )

    verdict = to_verdict(result, policy=body.policy, tier=body.tier)
    return JSONResponse(verdict.model_dump(mode="json"), headers=paid)


def _discovery() -> dict[str, Any]:
    return {
        "x402Version": 2,
        "service": "Veridex",
        "version": __version__,
        "endpoint": f"{settings.public_base_url}/v1/check",
        "method": "POST",
        "scheme": "exact",
        "network": settings.solana_network,
        "asset": "wSOL (So11111111111111111111111111111111111111112)",
        "feePayer": "Veridex pays the network fee",
        "tiers": {
            "quick": {"price_lamports": settings.x402_price_quick_lamports, "skills": sorted(QUICK_SKILLS)},
            "deep": {"price_lamports": settings.x402_price_deep_lamports, "skills": "all"},
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
            "flags": [{"type": "string", "evidence": "string", "weight": "0..1"}],
            "policy_applied": "string",
            "tier": "string",
            "cached": "bool",
            "checked_at": "ISO-8601",
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
    quick, deep = d["tiers"]["quick"]["price_lamports"], d["tiers"]["deep"]["price_lamports"]
    return f"""# Veridex

> Pay-per-check risk signals for AI shopping agents: is this store or product listing showing signs of scams,
> dropshipping of generic goods, fake reviews or dark patterns? Returns evidence and an action, never a "scam" label.

## Endpoint

POST {d["endpoint"]} with JSON {{"url": "...", "page_content": "<html> (optional)", "tier": "quick|deep",
"policy": "strict|balanced|lenient"}}. Send page_content for marketplaces (Amazon blocks server-side fetches).

## Payment (x402 v2)

The first call answers 402 with a PAYMENT-REQUIRED header. Pay with the `exact` scheme on Solana ({d["network"]}) in
wSOL (wrapped SOL; no stablecoins) and retry with PAYMENT-SIGNATURE. Veridex is the fee payer, so you only pay the
price: quick = {quick / 1e9:.4f} SOL, deep = {deep / 1e9:.4f} SOL. A page we can't fetch costs nothing.

## Response

{{"action": "allow|warn|block", "risk_score": 0..1, "confidence": 0..1, "flags": [{{"type", "evidence", "weight"}}],
"policy_applied", "tier", "cached", "checked_at"}}. "warn" means: ask your human before buying.

## Machine-readable

{settings.public_base_url}/.well-known/x402
"""
