"""
``POST /v1/check``: pay-per-check for AI shopping agents over x402 (v2, ``exact`` scheme).

Two networks, each optional: wSOL or USDC on Solana, and USDC on Base (behind ``x402_base_enabled``). No
``PAYMENT-SIGNATURE`` header → 402 with ``PAYMENT-REQUIRED`` listing every option (Solana's first, then Base's).

Solana (``upfront``): verify the payment strictly, fetch the page if only a URL was given (before any money moves),
co-sign as fee payer (the hot wallet, which is also ``payTo``), submit and confirm, then run the analysis and answer
with the verdict and ``PAYMENT-RESPONSE``. Base (``authorization``, x402's default): check the payment locally and
with the facilitator, fetch and analyse, and only then have the facilitator settle it (see ``unfret.x402.base``).
Nothing about the payment or the page is stored.

Instead of paying, an agent may send its Unfret pass (``Authorization: Bearer ufk_…``): the check is then charged to
the pass like ``POST /v1/analyze`` (:mod:`unfret.routes.check_pass`). A request with ``PAYMENT-SIGNATURE`` is always
an x402 payment.
"""

import asyncio
import json
import re
import time
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from unfret import __version__, clients_info, observations, verdict_store
from unfret.config import settings
from unfret.credits import ledger
from unfret.net import fetch_public_page
from unfret.page_fetch import final_address, is_challenge_page
from unfret.prices import format_usd
from unfret.visitors import RateLimit
from unfret.x402.base import (
    BasePrecheck,
    BaseSettlementMismatchError,
    BaseX402,
    SettlementRefusedError,
    SettlementUnknownError,
)
from unfret.x402.facilitator import FacilitatorError, FacilitatorUnreachableError
from unfret.x402.ownership import ownership_proofs
from unfret.x402.payment import PaymentError, Precheck, decode_header, encode_header
from unfret.x402.service import X402, DuplicateSettlementError, Settlement, SettlementMismatchError
from unfret.x402.solana import (
    CAIP2,
    WSOL_MINT,
    RpcError,
    SettlementPendingError,
    TransactionFailedError,
    decimals,
    usdc_mint,
)
from unfret.x402.verdict import (
    POLICY_THRESHOLDS,
    QUICK_SKILLS,
    Policy,
    Tier,
    Verdict,
    tier_description,
    to_verdict,
)

logger = structlog.get_logger("unfret")

router = APIRouter(tags=["Check (x402 or a pass)"])

PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
PAYMENT_RESPONSE = "PAYMENT-RESPONSE"

_REPLAY_TTL_S = 900.0  # how long a paid check's answer is kept for a retry with the same payment
_PENDING_WATCH_S = 120.0  # how long a submitted-but-unconfirmed payment is watched before it's let go
_rate = RateLimit(settings.x402_check_per_minute)
# Answers to paid checks by payment (the client's signature): a retry gets the same answer, never a second charge.
_replays: dict[str, tuple[float, asyncio.Future[tuple[int, dict[str, Any], dict[str, str]]]]] = {}
_background: set[asyncio.Task[None]] = set()


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


def rate_limited(wait: float) -> JSONResponse:
    """
    The 429 for too many requests to ``/v1/check``.

    :param wait: Seconds until the next request is allowed.
    :return: The response, with ``Retry-After``.
    """
    return JSONResponse(
        {"detail": "Too many requests; slow down.", "error_code": "RATE_LIMITED"},
        status_code=429,
        headers={"Retry-After": str(max(1, int(wait) + 1))},
    )


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


def get_x402_base(request: Request) -> BaseX402:
    """
    Return the app's x402-on-Base service, creating it from settings on first use.

    :param request: The incoming request.
    :return: The service (off unless ``x402_base_enabled``).
    """
    app = request.app
    if getattr(app, "x402_base", None) is None:
        app.x402_base = BaseX402(settings)
    service: BaseX402 = app.x402_base
    return service


def _resource(*, discovery: bool = False) -> dict[str, Any]:
    """
    The x402 ``resource`` object of ``/v1/check``.

    :param discovery: Add the Bazaar's service metadata (``tags``, ``iconUrl``): with Base payments, whose
        facilitator catalogues the endpoint from it.
    :return: The resource.
    """
    resource: dict[str, Any] = {
        "url": f"{settings.public_base_url}/v1/check",
        "description": CHECK_DESCRIPTION,
        "mimeType": "application/json",
        "serviceName": "Unfret",
    }
    if discovery:
        resource["tags"] = list(BAZAAR_TAGS)
        resource["iconUrl"] = f"{settings.public_base_url}/static/icons/icon-512.png"
    return resource


def _payment_required(accepts: list[dict[str, Any]], error: str, reason: str | None = None) -> JSONResponse:
    base = any(option["network"].startswith("eip155:") for option in accepts)
    body: dict[str, Any] = {
        "x402Version": 2,
        "error": error,
        "error_code": "PAYMENT_REQUIRED",
        "resource": _resource(discovery=base),
        "accepts": accepts,
    }
    if base:  # discovery metadata for the Bazaar; a 402 without a Base option stays exactly as before
        body["extensions"] = _base_extensions()
    if reason:  # a finer cause the client can act on, e.g. why the payment's simulation failed
        body["reason"] = reason
    return JSONResponse(body, status_code=402, headers={PAYMENT_REQUIRED: encode_header(body)})


def _settlement(
    x402: X402 | BaseX402, *, success: bool, transaction: str, payer: str, reason: str | None = None
) -> str:
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
    return await _credit(request.app.db_pool, payer, usd_micros)


async def _credit(pool: Any, payer: str, usd_micros: int) -> int:
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


def _provenance(request: Request, body: CheckRequest, account: str | None = None) -> observations.Provenance:
    """
    Where a checked page came from, for what the analysis records (:mod:`unfret.observations`).

    :param request: The incoming request (its client's network, hashed with today's salt, never stored).
    :param body: The request body: without ``page_content`` Unfret fetched the page itself.
    :param account: The pass, for a check paid with one (never the paying wallet).
    :return: The provenance.
    """
    ip = request.client.host if request.client else "unknown"
    return observations.for_request(ip, account, fetched=not body.page_content, tier=body.tier)


async def _page_html(body: CheckRequest) -> tuple[str, CheckRequest]:
    if body.page_content:
        return body.page_content, body
    fetched = await fetch_public_page(body.url, max_bytes=settings.max_page_bytes)
    html = fetched.body.decode("utf-8", errors="replace")
    address = final_address(body.url, fetched.url)
    return html, body if address == body.url else body.model_copy(update={"url": address})


async def _readable_page(body: CheckRequest) -> tuple[str, CheckRequest] | Response:
    """
    The page to analyse, or the (free) error response when it can't be read.

    A link that redirected (a short link) is checked under the page's own address
    (:func:`unfret.page_fetch.final_address`).

    :param body: The request body.
    :return: The HTML and the request as analysed (``body``, with the page's own address after a redirect), or a 422
        response.
    """
    try:
        html, body = await _page_html(body)
    except Exception as exc:  # noqa: BLE001
        logger.info("check_page_fetch_failed", error=type(exc).__name__)
        return JSONResponse(
            {
                "detail": "Could not fetch the page; send page_content instead. You were not charged.",
                "error_code": "PAGE_UNAVAILABLE",
            },
            status_code=422,
        )
    if is_challenge_page(html):
        return JSONResponse(
            {
                "detail": "The page is a bot check, not the store; send the real page as page_content. "
                "You were not charged.",
                "error_code": "PAGE_BLOCKED",
            },
            status_code=422,
        )
    return html, body


async def _credit_if_it_lands(x402: X402, pool: Any, signature: str, payer: str, usd_micros: int) -> None:
    """
    Watch a payment that was submitted but not confirmed in time; if it lands, credit its price (no check ran).

    :param x402: The payment service.
    :param pool: The database pool.
    :param signature: The submitted transaction.
    :param payer: The paying wallet.
    :param usd_micros: The tier's price.
    """
    deadline = time.monotonic() + _PENDING_WATCH_S
    while time.monotonic() < deadline:
        try:
            status = await x402.rpc.signature_status(signature)
        except RpcError:
            status = None
        if status is not None and status.get("err") is not None:
            return  # it failed: nothing was paid
        if status is not None and status.get("confirmationStatus") in {"confirmed", "finalized"}:
            await _credit(pool, payer, usd_micros)
            logger.warning("x402_pending_payment_landed_credited", usd_micros=usd_micros)
            return
        await asyncio.sleep(2)
    logger.warning("x402_pending_payment_unresolved")


async def _settle(
    x402: X402, pre: Precheck, accepts: list[dict[str, Any]], payer: str, ip: str, *, pool: Any, usd_micros: int
) -> Settlement | Response:
    """
    Settle a prechecked payment, or answer why it didn't settle (counting failures that cost us a fee).

    :param x402: The payment service.
    :param pre: The prechecked payment.
    :param accepts: The options offered, for a renewed 402.
    :param payer: The paying wallet.
    :param ip: The visitor's address, for the breaker.
    :param pool: The database pool, to credit a pending payment that lands later.
    :param usd_micros: The tier's price, credited in that case.
    :return: The settlement, or the response to send instead.
    """
    try:
        return await x402.settle(pre)
    except PaymentError as exc:  # the simulation showed a different outcome; nothing was submitted
        if exc.cause:
            logger.info("x402_payment_refused", error=exc.reason, reason=exc.cause)
        return _payment_required(accepts, exc.reason, exc.cause)
    except DuplicateSettlementError:
        return _payment_required(accepts, "duplicate_settlement")
    except SettlementMismatchError as exc:
        # It landed, but not as the simulation promised: no analysis for this payment.
        x402.breaker.record_failure(payer, ip)
        headers = {
            PAYMENT_RESPONSE: _settlement(
                x402, success=False, transaction=exc.signature, payer=payer, reason="settlement_mismatch"
            )
        }
        return JSONResponse(
            {"detail": "The payment did not settle as required.", "error_code": "SETTLEMENT_MISMATCH"}, 402, headers
        )
    except SettlementPendingError as exc:
        x402.breaker.record_failure(payer, ip)  # it may yet land, or fail at our expense
        task = asyncio.create_task(_credit_if_it_lands(x402, pool, exc.signature, payer, usd_micros))
        _background.add(task)
        task.add_done_callback(_background.discard)
        headers = {
            PAYMENT_RESPONSE: _settlement(
                x402, success=False, transaction=exc.signature, payer=payer, reason="settlement_pending"
            )
        }
        detail = (
            "Payment submitted but not confirmed in time; no check ran. If it lands within two minutes, its price "
            f"is credited to {payer} (sign in at {settings.public_base_url}/connect); otherwise nothing was charged."
        )
        return JSONResponse(
            {"detail": detail, "error_code": "SETTLEMENT_PENDING", "status": "unknown", "transaction": exc.signature},
            402,
            headers,
        )
    except RpcError as exc:
        logger.info("x402_settlement_failed", error=str(exc)[:200])
        if isinstance(exc, TransactionFailedError):  # it landed and failed: the hot wallet paid the fee
            x402.breaker.record_failure(payer, ip)
        headers = {
            PAYMENT_RESPONSE: _settlement(x402, success=False, transaction="", payer=payer, reason="settlement_failed")
        }
        return JSONResponse(
            {"detail": "Payment could not be settled; you were not charged.", "error_code": "SETTLEMENT_FAILED"},
            402,
            headers,
        )


def _usd_amount(usd_micros: int) -> str:
    return format(Decimal(usd_micros).scaleb(-6).normalize(), "f")


def _payment_info() -> dict[str, Any]:
    """
    ``x-payment-info`` of ``POST /v1/check`` in the OpenAPI document: x402 directories (x402scan) look for it.

    The price range is the tiers' (the same settings as ``/.well-known/x402``); the 402 itself has the exact amounts.

    :return: The extension's value.
    """
    low, high = sorted((settings.x402_price_quick_usd_micros, settings.x402_price_deep_usd_micros))
    return {
        "price": {"mode": "dynamic", "currency": "USD", "min": _usd_amount(low), "max": _usd_amount(high)},
        "protocols": [{"x402": {}}],
    }


def _offers_payment(request: Request) -> bool:
    """
    Whether a ``/v1/check`` request pays: an x402 payment (``PAYMENT-SIGNATURE``) or an Unfret pass (a bearer token).

    :param request: The incoming request.
    :return: True if it does; its body is then validated (422) before anything else.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return bool(request.headers.get(PAYMENT_SIGNATURE)) or (scheme.lower() == "bearer" and bool(token.strip()))


def _tier_of(raw: bytes) -> Tier:
    """
    The tier an unvalidated body asks for, to price an unpaid request's 402.

    :param raw: The request body, whatever it is.
    :return: Its ``tier`` when that's a tier, else the default (quick), as for a valid body without one.
    """
    try:
        sent = json.loads(raw)
    except ValueError:  # not JSON (or not UTF-8), or empty
        return "quick"
    tier = sent.get("tier") if isinstance(sent, dict) else None
    return "deep" if tier == "deep" else "quick"


class PaymentFirstRoute(APIRoute):
    """
    ``POST /v1/check``: a request without a payment or a pass gets the 402 whatever its body, before validation.

    x402 directories (x402scan, x402-list.com) probe without a body first and read the price from the 402. The body is
    only validated (422) once a payment or a pass comes with it, so nothing is ever charged for an invalid body.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """
        FastAPI's handler (validation, then the endpoint), behind the unpaid shortcut.

        :return: The handler.
        """
        validate_and_check = super().get_route_handler()

        async def handler(request: Request) -> Response:
            if _offers_payment(request):
                return await validate_and_check(request)
            return await _x402_check(None, _tier_of(await request.body()), request)

        return handler


async def check(body: CheckRequest, request: Request) -> Response:
    """
    Check a store or product page for scam, dropshipping and dark-pattern signals, with an Unfret pass or x402.

    **With a pass** (``Authorization: Bearer ufk_…``; a developer pass from ``GET /v1/keys/challenge?developer=true``
    and ``POST /v1/keys``): a deep check uses one of the pass's checks, free ones first; a quick check is free. The
    verdict adds ``charged``, ``free_check``, ``balance_usd_micros`` and ``free_checks_left``. **With x402** (no
    account): without a payment or a pass the answer is 402 with the options in ``PAYMENT-REQUIRED`` (USDC and wSOL on
    Solana, and USDC on Base where it is on: ``/.well-known/x402`` lists them); pay one and send the request again
    with ``PAYMENT-SIGNATURE``. Retrying with the same payment returns the same answer (for 15 minutes) and is never
    charged twice. The 402 comes whatever the body; the body is validated (422) only with a payment or a pass. Every
    error is JSON with ``detail`` and ``error_code`` (``/llms.txt`` lists them, and whether each was charged).

    :param body: URL, optional page HTML, tier and policy.
    :param request: The incoming request (payment header or pass, app state).
    :return: The verdict, or an x402 payment-required / error response.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not request.headers.get(PAYMENT_SIGNATURE) and scheme.lower() == "bearer" and token.strip():
        # A pass is limited per pass rather than per network (agents behind one host, e.g. a hosted MCP client).
        from unfret.routes import check_pass  # noqa: PLC0415 - it imports analyze → auth → this module

        return await check_pass.pass_check(body, request, token.strip())
    return await _x402_check(body, body.tier, request)


router.add_api_route(
    "/v1/check",
    check,
    methods=["POST"],
    response_model=Verdict,
    responses={
        401: {"description": "Not a valid Unfret pass (only with Authorization): INVALID_API_KEY"},
        402: {
            "description": "Payment required (x402: the options are in the PAYMENT-REQUIRED header), a refused "
            "payment, or the pass has no checks left (INSUFFICIENT_CREDITS)"
        },
        422: {"description": "An invalid body, or a page Unfret couldn't fetch (PAGE_UNAVAILABLE, PAGE_BLOCKED)"},
        429: {"description": "Over a rate limit (RATE_LIMITED, TOO_MANY_REQUESTS) or FREE_CHECKS_PAUSED"},
        503: {"description": "Payments or the analysis are off for a moment: retry after Retry-After"},
        504: {"description": "The analysis timed out: not charged (x402 on Solana: credited to the paying wallet)"},
    },
    openapi_extra={"x-payment-info": _payment_info()},
    route_class_override=PaymentFirstRoute,
)


async def _x402_check(body: CheckRequest | None, tier: Tier, request: Request) -> Response:  # noqa: PLR0911
    """
    The x402 side of ``/v1/check``: the 402 without a payment, else verify the payment and run the paid check.

    :param body: The validated body, or None for a request without a payment (only its 402 is possible).
    :param tier: The tier to price (the body's, or :func:`_tier_of` the raw body's).
    :param request: The incoming request.
    :return: The verdict, or an x402 payment-required / error response.
    """
    ip = request.client.host if request.client else "unknown"
    if (wait := _rate.retry_after(ip)) is not None:
        return rate_limited(wait)
    x402, base = get_x402(request), get_x402_base(request)
    solana_ok = (await x402.health()).status == "ok"
    base_accepts = await base.accepts(tier)
    if not solana_ok and not base_accepts:
        return JSONResponse(
            {"detail": "Payments are not available right now.", "error_code": "SERVICE_UNAVAILABLE"}, status_code=503
        )

    # Solana's options first (unchanged for clients that only know Solana), then Base's.
    accepts = (await x402.accepts(tier) if solana_ok else []) + base_accepts
    if not accepts:
        return JSONResponse(
            {"detail": "No payment option can be priced right now; retry shortly.", "error_code": "PRICE_UNAVAILABLE"},
            status_code=503,
            headers={"Retry-After": "30"},
        )
    header = request.headers.get(PAYMENT_SIGNATURE)
    if not header or body is None:
        return _payment_required(accepts, f"{PAYMENT_SIGNATURE} header is required")

    try:
        payload = decode_header(header)
        if base_accepts and base.owns(payload):
            base_pre = base.precheck(payload, accepts)
            return await _answer_once(
                base_pre.key, lambda: _paid_check_base(body, request, base, base_pre, accepts=accepts, ip=ip)
            )
        if not solana_ok:  # not a payment on Base, and Solana is off: nothing it could match
            return _payment_required(accepts, "requirements_mismatch")
        pre = x402.precheck(header, tier, accepts)
    except PaymentError as exc:
        if exc.malformed:
            return JSONResponse({"detail": exc.reason, "error_code": "INVALID_PAYMENT"}, status_code=400)
        return _payment_required(accepts, exc.reason, exc.cause)
    return await _answer_once(
        str(pre.transaction.signatures[1]), lambda: _paid_check(body, request, x402, pre, accepts=accepts, ip=ip)
    )


async def _answer_once(
    key: str, run: Callable[[], Awaitable[Response | tuple[int, dict[str, Any], dict[str, str]]]]
) -> Response:
    """
    Run a paid check once per payment: a retry (or a concurrent duplicate) with the same payment gets the same answer.

    :param key: What identifies the payment (Solana: the client's signature; Base: payer and authorization nonce).
    :param run: The check; a :class:`Response` means it was refused before any money moved (a retry starts over),
        a tuple is the answer to keep.
    :return: The response.
    """
    now = time.monotonic()
    for stale in [k for k, (expires, _) in _replays.items() if expires < now]:
        del _replays[stale]
    if key in _replays:
        status, content, headers = await asyncio.shield(_replays[key][1])
        return JSONResponse(content, status_code=status, headers=headers)
    answer: asyncio.Future[tuple[int, dict[str, Any], dict[str, str]]] = asyncio.get_running_loop().create_future()
    _replays[key] = (now + _REPLAY_TTL_S, answer)
    outcome: Response | tuple[int, dict[str, Any], dict[str, str]] | None = None
    try:
        outcome = await run()
    finally:
        if outcome is None or isinstance(outcome, Response):  # refused before money moved: a retry starts over
            _replays.pop(key, None)
            answer.cancel()
    if isinstance(outcome, Response):
        return outcome
    answer.set_result(outcome)
    status, content, headers = outcome
    return JSONResponse(content, status_code=status, headers=headers)


async def _paid_check_base(  # noqa: C901, PLR0911
    body: CheckRequest,
    request: Request,
    base: BaseX402,
    pre: BasePrecheck,
    *,
    accepts: list[dict[str, Any]],
    ip: str,
) -> Response | tuple[int, dict[str, Any], dict[str, str]]:
    """
    A paid check on Base: verify with the facilitator, fetch and analyse, then settle (x402's ``authorization`` flow).

    Nothing is settled unless there is an answer to give, so a page that can't be read or assessed, or an analysis
    that fails, costs the payer nothing (and nothing needs refunding). A settlement that was broadcast but not
    confirmed still gets its answer (the payment may land), with ``PAYMENT-RESPONSE`` saying it is pending.

    :param body: The request body.
    :param request: The incoming request.
    :param base: The Base payment service.
    :param pre: The prechecked payment.
    :param accepts: The options offered.
    :param ip: The visitor's address.
    :return: A response sent without settling, or the answer (status, body, headers) to keep for retries.
    """
    payer = pre.payer
    if (wait := base.breaker.open_for(payer, ip)) is not None:
        logger.warning("x402_breaker_open", network=base.network)
        return JSONResponse(
            {
                "detail": "Too many payments failed to settle recently; payments are paused. You were not charged.",
                "error_code": "SETTLEMENTS_PAUSED",
            },
            status_code=503,
            headers={"Retry-After": str(max(1, int(wait)))},
        )
    resource, extensions = _resource(discovery=True), _base_extensions()
    try:
        await base.verify(pre, resource, extensions)
    except PaymentError as exc:
        return _payment_required(accepts, exc.reason)
    except FacilitatorError as exc:
        logger.warning("x402_base_verify_unavailable", error=str(exc)[:120])
        return JSONResponse(
            {"detail": "Payments on Base are not available right now.", "error_code": "SERVICE_UNAVAILABLE"},
            status_code=503,
            headers={"Retry-After": "30"},
        )

    analysed = await _analyse_before_settling(body, request)
    if isinstance(analysed, Response):
        return analysed
    result, answer, verdict, body = analysed  # body: at the page's own address, if the link redirected

    try:
        settlement = await base.settle(pre, resource, extensions)
    except FacilitatorUnreachableError as exc:  # never sent: nothing settled, and not the payer's doing
        logger.warning("x402_base_settle_unreachable", error=str(exc)[:120])
        return JSONResponse(
            {
                "detail": "Payments on Base are not available right now; you were not charged.",
                "error_code": "SERVICE_UNAVAILABLE",
            },
            status_code=503,
            headers={"Retry-After": "30"},
        )
    except SettlementRefusedError as exc:
        base.breaker.record_failure(payer, ip)  # the work was done for nothing
        headers = {
            PAYMENT_RESPONSE: _settlement(
                base, success=False, transaction=exc.transaction, payer=payer, reason=exc.reason
            )
        }
        return JSONResponse(
            {
                "detail": "Payment could not be settled; you were not charged.",
                "error_code": "SETTLEMENT_FAILED",
                "reason": exc.reason,
            },
            402,
            headers,
        )
    except SettlementUnknownError as exc:
        # It may still land: the payer gets the answer it may have paid for (bounded by the breaker).
        base.breaker.record_failure(payer, ip)
        logger.warning("x402_base_settlement_pending", transaction=exc.transaction)
        receipt = _settlement(
            base, success=False, transaction=exc.transaction, payer=payer, reason="settlement_pending"
        )
    except BaseSettlementMismatchError as exc:
        base.breaker.record_failure(payer, ip)
        headers = {
            PAYMENT_RESPONSE: _settlement(
                base, success=False, transaction=exc.transaction, payer=payer, reason="settlement_mismatch"
            )
        }
        content = {"detail": "The payment did not settle as required.", "error_code": "SETTLEMENT_MISMATCH"}
        return 402, content, headers
    else:
        receipt = _settlement(base, success=True, transaction=settlement.transaction, payer=payer)
    if result is not None and answer is not None:
        await verdict_store.remember(body.url, result, answer, fetched_by_us=not body.page_content)
    return 200, verdict, {PAYMENT_RESPONSE: receipt}


async def _analyse_before_settling(
    body: CheckRequest, request: Request
) -> Response | tuple[dict[str, Any] | None, Verdict | None, dict[str, Any], CheckRequest]:
    """
    The answer to a Base payment's check, before anything is settled: from the verdict store, or a fresh analysis.

    :param body: The request body.
    :param request: The incoming request.
    :return: A response to send without settling (an unreadable page, a failed analysis, a page that can't be
        assessed: none of them is charged), or the analysis result (``None`` from the store), verdict, its JSON and
        the request as analysed (at the page's own address when the link redirected).
    """
    fast = None if body.page_content else await verdict_store.recall(body.url, body.tier, body.policy)
    if fast is not None:
        logger.info("check_fast_path", tier=body.tier)
        return None, None, fast.model_dump(mode="json"), body
    readable = await _readable_page(body)
    if isinstance(readable, Response):
        return readable
    page, body = readable
    graph = request.app.graph if body.tier == "deep" else request.app.quick_graph
    if graph is None:
        return JSONResponse({"detail": "Analysis is not available.", "error_code": "SERVICE_UNAVAILABLE"}, 503)
    try:
        async with asyncio.timeout(min(settings.analysis_timeout_s, settings.x402_analysis_timeout_s)):
            with observations.observed_as(_provenance(request, body)):
                result = await graph.ainvoke({"page_content": page, "url": body.url, "flags": [], "explain": False})
    except Exception as exc:  # noqa: BLE001
        where = _failure_site(exc)
        logger.warning("check_analysis_failed_before_settlement", error=type(exc).__name__, where=where)
        timed_out = isinstance(exc, TimeoutError)
        return JSONResponse(
            {
                "detail": "The analysis " + ("timed out" if timed_out else "failed") + "; you were not charged.",
                "error_code": "GATEWAY_TIMEOUT" if timed_out else "INTERNAL_ERROR",
            },
            status_code=504 if timed_out else 500,
        )
    answer = to_verdict(result, policy=body.policy, tier=body.tier)
    if result.get("score") == -1:  # the page couldn't be assessed: not settled, so not charged
        return JSONResponse({**answer.model_dump(mode="json"), "charged": False})
    return result, answer, answer.model_dump(mode="json"), body


# What /v1/check does, in the 402's resource (the Bazaar and x402scan show it): what it checks and when it's useful.
CHECK_DESCRIPTION = (
    "Risk check of a store, marketplace seller or product page before an agent buys: signs of scams, fake sellers, "
    "dropshipping of generic goods, fake reviews and dark patterns, with the evidence and an allow, warn or block "
    "action"
)
BAZAAR_TAGS = ("shopping", "scam-detection", "e-commerce", "risk", "agents")
# What a verdict looks like, for the Bazaar's catalogue (a made-up page; validated against Verdict in the tests).
VERDICT_EXAMPLE: dict[str, Any] = {
    "action": "warn",
    "assessed": True,
    "risk_score": 0.62,
    "confidence": 0.8,
    "flags": [
        {
            "type": "generic_product_markup",
            "evidence": "The same product is sold elsewhere for a fraction of the price.",
            "weight": 0.3,
        }
    ],
    "policy_applied": "balanced",
    "tier": "quick",
    "cached": False,
    "source": "analysis",
    "checked_at": "2026-11-17T12:00:00Z",
    "model_version": "v1-quick-20261109",
    "band": "careful",
    "summary": "Signs of a dropshipped generic product at a markup; ask before buying.",
}


def _base_extensions() -> dict[str, Any]:
    """
    The x402 extensions Unfret declares with Base payments: ``bazaar``, so the facilitator's catalogue lists it.

    The facilitator catalogues an endpoint after a settled payment whose payload carries this (Unfret sends its own
    copy with every settlement, whatever the client echoed). ``info`` describes the call; ``schema`` (JSON Schema
    2020-12, no external references) validates ``info``.

    :return: The ``extensions`` object.
    """
    body_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The store or product page to check."},
            "page_content": {"type": "string", "description": "The page's HTML (for marketplaces such as Amazon)."},
            "tier": {"type": "string", "enum": ["quick", "deep"]},
            "policy": {"type": "string", "enum": ["strict", "balanced", "lenient"]},
        },
        "required": ["url"],
    }
    return {
        "bazaar": {
            "info": {
                "input": {
                    "type": "http",
                    "method": "POST",
                    "bodyType": "json",
                    "body": {"url": "https://shop.example/p/42", "tier": "quick", "policy": "balanced"},
                },
                "output": {"type": "json", "example": VERDICT_EXAMPLE},
            },
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "const": "http"},
                            "method": {"type": "string", "enum": ["POST"]},
                            "bodyType": {"type": "string", "enum": ["json"]},
                            "body": body_schema,
                        },
                        "required": ["type", "method", "bodyType", "body"],
                        "additionalProperties": False,
                    },
                    "output": {
                        "type": "object",
                        "properties": {"type": {"type": "string"}, "example": {"type": "object"}},
                        "required": ["type"],
                    },
                },
                "required": ["input"],
            },
        }
    }


async def _paid_check(  # noqa: PLR0911
    body: CheckRequest, request: Request, x402: X402, pre: Precheck, *, accepts: list[dict[str, Any]], ip: str
) -> Response | tuple[int, dict[str, Any], dict[str, str]]:
    """
    Fetch, settle and analyse; the part of a check that a retry with the same payment must not repeat.

    :param body: The request body.
    :param request: The incoming request.
    :param x402: The payment service.
    :param pre: The prechecked payment.
    :param accepts: The options offered.
    :param ip: The visitor's address.
    :return: A response sent before any money moved, or the paid answer (status, body, headers) to keep for retries.
    """
    payer = str(pre.payer)
    if (wait := x402.breaker.open_for(payer, ip)) is not None:
        logger.warning("x402_breaker_open")
        return JSONResponse(
            {
                "detail": "Too many payments failed to settle recently; payments are paused. You were not charged.",
                "error_code": "SETTLEMENTS_PAUSED",
            },
            status_code=503,
            headers={"Retry-After": str(max(1, int(wait)))},
        )

    # A listing analysed from its own page in the last day, at an unchanged shop, is answered again (only when the
    # caller doesn't send the page: an answer is never kept from, or served instead of, caller-supplied content).
    fast = None if body.page_content else await verdict_store.recall(body.url, body.tier, body.policy)
    html: str = ""
    graph: Any = None
    if fast is None:
        # Get the page before any money moves, so a page we can't read costs the caller nothing.
        readable = await _readable_page(body)
        if isinstance(readable, Response):
            return readable
        html, body = readable  # at the page's own address, if the link redirected
        graph = request.app.graph if body.tier == "deep" else request.app.quick_graph
        if graph is None:
            return JSONResponse({"detail": "Analysis is not available.", "error_code": "SERVICE_UNAVAILABLE"}, 503)

    price = x402.usd_price(body.tier)  # the tier's dollar price, whichever currency paid it
    settlement = await _settle(x402, pre, accepts, payer, ip, pool=request.app.db_pool, usd_micros=price)
    if isinstance(settlement, Response):
        receipt = settlement.headers.get(PAYMENT_RESPONSE)
        if receipt and decode_header(receipt).get("transaction"):  # submitted: a retry must get this same answer
            return settlement.status_code, _json(settlement), {PAYMENT_RESPONSE: receipt}
        return settlement
    signature = settlement.signature
    paid = {PAYMENT_RESPONSE: _settlement(x402, success=True, transaction=signature, payer=payer)}
    if fast is not None:
        logger.info("check_fast_path", tier=body.tier)
        return 200, fast.model_dump(mode="json"), paid
    try:
        async with asyncio.timeout(min(settings.analysis_timeout_s, settings.x402_analysis_timeout_s)):
            with observations.observed_as(_provenance(request, body)):
                result = await graph.ainvoke({"page_content": html, "url": body.url, "flags": [], "explain": False})
    except Exception as exc:  # noqa: BLE001
        # The payment settled but the analysis failed: refund it as credits to the paying wallet (never an
        # outgoing transfer: the hot wallet only ever sends to cold), usable after signing in at /connect.
        logger.warning("check_analysis_failed_after_payment", error=type(exc).__name__, where=_failure_site(exc))
        timed_out = isinstance(exc, TimeoutError)
        content: dict[str, Any] = {
            "detail": "Payment settled, but the analysis " + ("timed out." if timed_out else "failed."),
            "error_code": "GATEWAY_TIMEOUT" if timed_out else "INTERNAL_ERROR",
            "transaction": signature,
        }
        _add_refund(content, await _refund_as_credits(request, payer, price), payer)
        return 504 if timed_out else 500, content, paid

    answer = to_verdict(result, policy=body.policy, tier=body.tier)
    verdict = answer.model_dump(mode="json")
    if result.get("score") == -1:  # the page couldn't be assessed: the caller isn't charged for it
        _add_refund(verdict, await _refund_as_credits(request, payer, price), payer)
    else:
        await verdict_store.remember(body.url, result, answer, fetched_by_us=not body.page_content)
    return 200, verdict, paid


def _failure_site(exc: BaseException, frames: int = 4) -> dict[str, Any]:
    """
    Say where an exception was raised, without anything that could carry the analysed URL.

    Exception messages can name the request (HTTP errors do), so they are never logged. Kept: our own innermost
    frames (which step failed) and the innermost frame overall, as ``file:line:function``; for an API error from
    the LLM provider, its status and error code/type, and its message with any URL removed.

    :param exc: The exception.
    :param frames: How many of our own innermost frames to keep.
    :return: ``{"ours": [...], "raised_in": ..., "provider": {...}?}``.
    """
    stack = traceback.extract_tb(exc.__traceback__)

    def site(f: traceback.FrameSummary) -> str:
        return f"{f.filename.rsplit('/site-packages/', 1)[-1].rsplit('/src/', 1)[-1]}:{f.lineno}:{f.name}"

    ours = [site(f) for f in stack if "/unfret/" in f.filename and "/site-packages/" not in f.filename]
    found: dict[str, Any] = {"ours": ours[-frames:], "raised_in": site(stack[-1]) if stack else None}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        body = getattr(exc, "body", None)
        error = body.get("error", body) if isinstance(body, dict) else None
        error = error if isinstance(error, dict) else {}
        message = str(error.get("message") or (body if isinstance(body, str) else ""))
        found["provider"] = {
            "status": status,
            "code": error.get("code"),
            "type": error.get("type"),
            "message": re.sub(r"(?:https?://|www\.)\S+", "<url>", message)[:200],
        }
    return found


def _add_refund(content: dict[str, Any], refunded: int, payer: str) -> None:
    if refunded:
        content["refunded_usd_micros"] = refunded
        content["refunded_lamports"] = refunded  # deprecated name of the same micro-dollar credit
        content["refund"] = f"Credited to {payer}; sign in with that wallet at {settings.public_base_url}/connect."


def _json(response: Response) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(bytes(response.body))
    return decoded


# error_code → (HTTP status, charged?, what to do). Published in the README, and in /llms.txt while Base is on.
ERRORS: dict[str, tuple[int, str, str]] = {
    "PAYMENT_REQUIRED": (
        402,
        "no",
        "pay one of `accepts` and retry with PAYMENT-SIGNATURE; `error` says why (`reason`: why a simulation failed)",
    ),
    "INVALID_PAYMENT": (400, "no", "the PAYMENT-SIGNATURE header couldn't be decoded"),
    "INVALID_API_KEY": (
        401,
        "no",
        (
            "the Authorization header isn't a valid Unfret pass (unknown, replaced with its backup code, or retired): "
            "fix it, or pay with x402"
        ),
    ),
    "INSUFFICIENT_CREDITS": (402, "no", "the pass has no checks left: top it up, or pay with x402"),
    "FREE_CHECKS_PAUSED": (429, "no", "the pass has only free checks and today's are used: after 00:00 UTC, or pay"),
    "VALIDATION_ERROR": (422, "no", "fix the request body (see `errors`); checked only with a payment or a pass"),
    "PAGE_UNAVAILABLE": (422, "no", "the page couldn't be fetched: send page_content"),
    "PAGE_BLOCKED": (422, "no", "the page is a bot check: send the real page as page_content"),
    "RATE_LIMITED": (429, "no", "wait Retry-After seconds"),
    "PRICE_UNAVAILABLE": (503, "no", "nothing can be priced right now: retry after Retry-After"),
    "SETTLEMENTS_PAUSED": (503, "no", "too many failed settlements: retry after Retry-After"),
    "SERVICE_UNAVAILABLE": (503, "no", "payments or analysis are off: retry later"),
    "SETTLEMENT_FAILED": (402, "no", "the network refused the payment (`reason` on Base): pay again"),
    "SETTLEMENT_PENDING": (402, "maybe", "Solana: don't pay again; if it lands within 2 min its price is credited"),
    "SETTLEMENT_MISMATCH": (402, "yes", "it landed differently than required: no verdict (alerted)"),
    "GATEWAY_TIMEOUT": (
        504,
        "refunded / no",
        "the analysis timed out: Solana credits the price to the paying wallet; Base never settled it",
    ),
    "INTERNAL_ERROR": (
        500,
        "refunded / no",
        "the analysis failed: Solana credits the price to the paying wallet; Base never settled it",
    ),
}


# The same rows for a server that takes Solana only (Base off, as on unfret.net for now): nothing about Base.
SOLANA_ONLY_ERRORS: dict[str, tuple[int, str, str]] = {
    **ERRORS,
    "SETTLEMENT_FAILED": (402, "no", "the network refused the payment: pay again"),
    "SETTLEMENT_PENDING": (402, "maybe", "don't pay again; if it lands within 2 min its price is credited"),
    "GATEWAY_TIMEOUT": (504, "refunded", "the analysis timed out: the price is credited to the paying wallet"),
    "INTERNAL_ERROR": (500, "refunded", "the analysis failed: the price is credited to the paying wallet"),
}


def _errors_table(base_on: bool) -> str:
    errors = ERRORS if base_on else SOLANA_ONLY_ERRORS
    rows = [f"| `{code}` | {status} | {charged} | {action} |" for code, (status, charged, action) in errors.items()]
    return "\n".join(["| error_code | HTTP | charged | what to do |", "| --- | --- | --- | --- |", *rows])


def _networks(base: BaseX402 | None) -> list[dict[str, Any]]:
    """
    Every network ``/v1/check`` takes payments on, for ``/.well-known/x402``: Solana, and Base while it is set up.

    :param base: The Base payment service (``None``: Solana only).
    :return: One entry per network.
    """
    networks: list[dict[str, Any]] = [
        {
            "network": CAIP2[settings.solana_network],
            "network_name": settings.solana_network,
            "assets": ["USDC", "wSOL"],
            "feePayer": "Unfret (the hot wallet co-signs and pays the fee)",
            "paymentFlow": "upfront",
        }
    ]
    if base is not None and base.configured:
        name, version = base.domain
        networks.append(
            {
                "network": base.network,
                "network_name": settings.base_network,
                "assets": {
                    "USDC": {
                        "address": base.usdc,
                        "decimals": 6,
                        "eip712": {"name": name, "version": version},
                        "assetTransferMethod": "eip3009",
                        "pricing": "exactly the USD price (1 USDC = $1)",
                    }
                },
                "payTo": base.pay_to,
                "feePayer": "the facilitator pays the gas: you sign a transferWithAuthorization, nothing else",
                "paymentFlow": "authorization (settled only when the answer is ready: a failed check costs nothing)",
            }
        )
    return networks


def _discovery(base: BaseX402 | None = None) -> dict[str, Any]:
    mint = usdc_mint(settings.solana_network, settings.usdc_mint)
    return {
        "x402Version": 2,
        "service": "Unfret",
        "version": __version__,
        "endpoint": f"{settings.public_base_url}/v1/check",
        "method": "POST",
        "scheme": "exact",
        # Solana's network (as before); `networks` lists every network, Base included while it is on.
        "network": CAIP2[settings.solana_network],  # CAIP-2, the same identifier as accepts[].network
        "network_name": settings.solana_network,
        "networks": _networks(base),
        "pricing": "canonical in USD",
        "assets": {
            "USDC": {
                "mint": str(mint),
                "decimals": decimals(mint),
                "pricing": "exactly the USD price (1 USDC = $1)",
                "quote_tolerance_bps": 0,
            },
            "wSOL": {
                "mint": str(WSOL_MINT),
                "decimals": decimals(WSOL_MINT),
                "pricing": "the USD price converted at quote time with Pyth's SOL/USD price; a payment may differ "
                f"from the quote by up to {settings.quote_tolerance_bps / 100:g}%; offered only while the oracle is "
                "fresh, otherwise pay in USDC. Re-fetch the 402 right before paying",
                "quote_tolerance_bps": settings.quote_tolerance_bps,
            },
        },
        "feePayer": "Unfret pays the network fee",
        "feeCap": "extra.constraints.maxPriorityFeeLamports caps the priority fee a payment may make Unfret pay. It "
        "is in lamports (SOL) for every option, USDC included, since the fee is paid in SOL: "
        f"{settings.x402_max_priority_fee_bps / 100:g}% of the tier's dollar price converted at the SOL/USD quote",
        "wallets": "standard wallets and smart wallets (Squads and other program-controlled wallets paying by CPI)",
        "tiers": {
            "quick": {
                "price_usd": format_usd(settings.x402_price_quick_usd_micros),
                "price_usd_micros": settings.x402_price_quick_usd_micros,
                "skills": sorted(QUICK_SKILLS),
                "includes": tier_description("quick"),
            },
            "deep": {
                "price_usd": format_usd(settings.x402_price_deep_usd_micros),
                "price_usd_micros": settings.x402_price_deep_usd_micros,
                "skills": "all",
                "includes": tier_description("deep"),
            },
        },
        "policies": {
            name: {
                "warn_at": w,
                "block_at": b,
                "never_warns_on_bands": [] if name == "strict" else ["reliable", "concerns"],
            }
            for name, (w, b) in POLICY_THRESHOLDS.items()
        },
        "auth": {
            "bearer": "Authorization: Bearer <Unfret pass> instead of an x402 payment: a deep check uses one of the "
            "pass's checks (free ones first), a quick check is free; the verdict adds charged, free_check, "
            "balance_usd_micros and free_checks_left",
            "developer_pass": {
                "challenge": f"{settings.public_base_url}/v1/keys/challenge?developer=true",
                "create": f"{settings.public_base_url}/v1/keys",
                "body": {"challenge": "string", "solution": "string", "developer": True},
                "free_deep_checks": settings.developer_free_checks,
                "per_network_per_day": settings.developer_passes_per_ip_per_day,
                "pow_bits": settings.key_pow_bits + settings.developer_pow_extra_bits,
                "free_checks_count_on": "POST /v1/check only (the API, the MCP server, the clients); bought checks "
                "on the pass work everywhere",
            },
        },
        # The other ways in, for an agent that found Unfret here: the MCP server and the docs.
        "mcp": {
            "remote": f"{settings.public_base_url}/mcp (streamable HTTP, Authorization: Bearer <Unfret pass>)",
            "stdio": "uvx unfret-mcp (PyPI unfret-mcp, MCP Registry net.unfret/mcp; pays from a pass or Solana)",
        },
        "docs": {
            "llms_txt": f"{settings.public_base_url}/llms.txt",
            "openapi": f"{settings.public_base_url}/openapi.json",
            "guide": f"{settings.public_base_url}/developers",
        },
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
async def discovery(request: Request) -> JSONResponse:
    """
    Describe the paid endpoint, its tiers and prices for x402 directories and agents.

    :param request: The incoming request (for the payment services).
    :return: The discovery document, with ``ownershipProofs`` (x402scan's) while a payment service holds a key.
    """
    d = _discovery(get_x402_base(request))
    if proofs := ownership_proofs(settings.public_base_url, (get_x402(request), get_x402_base(request))):
        d["ownershipProofs"] = proofs
    return JSONResponse(d)


def _llms_base(d: dict[str, Any]) -> str:
    base = next((n for n in d["networks"] if n["network"].startswith("eip155:")), None)
    if base is None:
        return ""
    usdc = base["assets"]["USDC"]
    return f"""
### On Base

USDC on Base (network {base["network"]}, i.e. {base["network_name"]}; USDC contract {usdc["address"]}, 6 decimals)
is offered after the Solana options. Sign an EIP-3009 `transferWithAuthorization` of exactly `amount` to `payTo`
(`extra.name` and `extra.version` are the token's EIP-712 domain) with `validBefore` at least two minutes ahead
(`maxTimeoutSeconds` is 300), and retry with PAYMENT-SIGNATURE as in the x402 `exact` EVM scheme. The facilitator pays
the gas. Base is settled only once the answer is ready: a page we can't read or assess, or a failed analysis, costs
nothing (no PAYMENT-RESPONSE, `"charged": false` on an unassessable page). If the settlement is broadcast but not
confirmed you still get the answer, with PAYMENT-RESPONSE `errorReason: "settlement_pending"`. Retrying with the same
authorization returns the same answer and is never settled twice.
"""


def mcp_oauth_on() -> bool:
    """
    Whether the remote MCP server takes OAuth sign-in, for the pages and ``/llms.txt`` that describe it.

    :return: ``mcp_oauth_enabled``; ``False`` on a server without the OAuth setting (it lands with the OAuth server).
    """
    return bool(getattr(settings, "mcp_oauth_enabled", False))


def _llms_mcp(base_url: str, oauth_on: bool) -> str:
    """
    The MCP way in, for ``/llms.txt``: the remote server and the local package.

    :param base_url: The public base URL, without a trailing slash.
    :param oauth_on: Whether the remote server also takes OAuth sign-in (``mcp_oauth_enabled``).
    :return: Markdown.
    """
    auth = "Authorization: Bearer ufk_... (an Unfret pass, see 2)"
    if oauth_on:
        auth += (
            ", or OAuth: a client that speaks MCP's OAuth adds the URL with no header and signs in (discovery at "
            f"{base_url}/.well-known/oauth-protected-resource/mcp; public clients, PKCE S256). Without a credential "
            "it can still connect, list the tools and call get_pricing; a check answers 401 and starts the sign-in"
        )
    remote = {"type": "http", "url": f"{base_url}/mcp", "headers": {"Authorization": "Bearer ufk_..."}}
    config = {"mcpServers": {"unfret": remote}}
    return f"""### 1. MCP server

- Remote: streamable HTTP at {base_url}/mcp, with {auth}. Tools: `check_store_quick` (free), `check_store_deep`
  (uses one of the pass's checks), `get_checks_left`, `get_pricing`. It never pays from a wallet. Claude Code:
  `claude mcp add --transport http unfret {base_url}/mcp --header "Authorization: Bearer $UNFRET_PASS"`; JSON:
  {json.dumps(config)}
- Local (stdio): `uvx unfret-mcp` (PyPI `unfret-mcp`; MCP Registry `net.unfret/mcp`). Tools: `check_store` (with a
  `tier`), `explain_flag`, `get_pricing`. Pays from a pass (env `UNFRET_PASS`) or over x402 from the agent's own
  Solana wallet (env `UNFRET_MCP_KEYPAIR`, capped by `UNFRET_MCP_MAX_PRICE_USD` and `UNFRET_MCP_SESSION_CAP_USD`);
  it pays on Solana only.
"""


@router.get("/llms.txt", include_in_schema=False, response_class=PlainTextResponse)
async def llms_txt(request: Request) -> str:
    """
    Describe the service for language-model agents: the three ways in (MCP, a pass, x402), then the details.

    :param request: The incoming request (for the Base payment service).
    :return: Plain text.
    """
    base = get_x402_base(request)
    d, base_on = _discovery(base), base.configured
    url = settings.public_base_url.rstrip("/")
    quick, deep = d["tiers"]["quick"]["price_usd"], d["tiers"]["deep"]["price_usd"]
    dev = d["auth"]["developer_pass"]
    networks = " and ".join(f"{'/'.join(n['assets'])} on {n['network_name']} ({n['network']})" for n in d["networks"])
    oauth_on = mcp_oauth_on()
    return f"""# Unfret

> Risk signals for AI shopping agents: is this store, marketplace listing, second-hand ad or service provider's
> page showing signs of scams, dropshipping of generic goods, fake reviews or dark patterns? Returns evidence and an
> action (allow, warn or block), never a "scam" label.

## Three ways to use Unfret

All three run the same check at the same prices: quick {quick} (free with a pass), deep {deep} (one check with a
pass).

{_llms_mcp(url, oauth_on)}
### 2. HTTP API with an Unfret pass

POST {d["endpoint"]} with `Authorization: Bearer ufk_...` and JSON {{"url": "...", "tier": "quick|deep"}}:

    curl {d["endpoint"]} -H "Authorization: Bearer $UNFRET_PASS" -H "Content-Type: application/json" \\
      -d '{{"url": "https://shop.example/p/42", "tier": "deep"}}'

Get a pass without an account (a developer pass, {dev["free_deep_checks"]} free deep checks for POST /v1/check):
1. GET {dev["challenge"]} → {{"challenge", "bits", "expires_at", "developer": true}} (use once, within
   {settings.key_pow_ttl_s // 60} minutes).
2. Find a `solution` (1 to 32 characters; a decimal counter 0, 1, 2, … works) such that SHA-256 of the UTF-8 bytes of
   challenge + ":" + solution starts with `bits` zero bits ({dev["pow_bits"]} today; a few seconds).
3. POST {dev["create"]} with {{"challenge", "solution", "developer": true}} → 201 {{"key": "ufk_...",
   "recovery_code", "free_checks", …}}. Both secrets are shown once: save them.
Or buy checks for any pass at {url}/checkout. GET {url}/v1/credits with the pass says what is left; GET
{url}/v1/pricing has every price.

### 3. x402 pay-per-call (no account)

POST {d["endpoint"]} without Authorization answers 402 with a PAYMENT-REQUIRED header (base64 JSON, also the body)
whose `accepts` lists the options: {networks}, scheme `exact`. Pay one with an x402 client and send the same request
again with the payment in PAYMENT-SIGNATURE; the answer carries PAYMENT-RESPONSE. Details below.

## Endpoint

POST {d["endpoint"]} with JSON {{"url": "...", "page_content": "<html> (optional)", "tier": "quick|deep",
"policy": "strict|balanced|lenient"}}. Send page_content for marketplaces (Amazon blocks server-side fetches).

## Tiers

- quick ({quick}): {d["tiers"]["quick"]["includes"]} Use it to screen many pages; it misses what only reading
  the listing, its reviews or its photos shows, so check what matters with deep. A page without a product and a
  price can't be assessed by quick (you are refunded). A quick answer's `deep_check.suggested` says whether deep is
  worth it for that page, and `deep_check.adds` what it would add, so you can escalate only when it helps.
- deep ({deep}): {d["tiers"]["deep"]["includes"]}

## Payment (x402 v2)

The first call answers 402 with a PAYMENT-REQUIRED header listing the options: wSOL (wrapped SOL) and USDC. Pay one
with the `exact` scheme on Solana (network {d["network"]}, i.e. {d["network_name"]}; the same CAIP-2 identifier as
`accepts[].network`) and retry with PAYMENT-SIGNATURE. Prices are in US dollars: quick = {quick}, deep = {deep}.
USDC (6 decimals) pays exactly that; the wSOL amount (9 decimals; `extra.decimals` in each option) is the dollar price
converted at quote time with Pyth's SOL/USD price (wSOL isn't offered while the oracle is stale). A wSOL payment may
differ from the quote by up to `extra.quoteToleranceBps` ({d["assets"]["wSOL"]["quote_tolerance_bps"]} bps); the
price moves, so re-fetch the 402 right before paying. Standard wallets and smart wallets (Squads and other
program-controlled wallets paying by CPI) both work, as long as the transaction carries one memo and pays the quoted
amount into payTo's token account for that mint (all rules: `extra.constraints` in each option). Unfret is the fee
payer, so you only pay the price. `extra.constraints.maxPriorityFeeLamports` caps the priority fee you may set; it is
in lamports (SOL) on every option, USDC included, because the fee is paid in SOL: a share of the tier's dollar price
converted at the SOL/USD quote (`extra.feeCapNote`). A refused payment answers 402 again with `error` (why) and, when
the payment's simulation failed, `reason` (e.g. `insufficient_funds`, `source_token_account_missing`,
`blockhash_not_found`). A page we can't fetch costs nothing. Retrying with the same PAYMENT-SIGNATURE returns the
same answer and is never charged twice. Without a payment (or a pass) the answer is this 402 whatever the body,
even none; the body is validated (422) once a payment or a pass comes with it, and an invalid one is never charged.
{_llms_base(d)}
## With an Unfret pass

Send `Authorization: Bearer ufk_...` (an Unfret pass) instead of PAYMENT-SIGNATURE: a deep check then uses one of the
pass's checks (free ones first), a quick check is free (a few a minute), and the verdict adds `charged`, `free_check`,
`balance_usd_micros` and `free_checks_left`. A developer pass (steps above) comes with {dev["free_deep_checks"]} free
deep checks, for the API and the MCP server: they count on POST /v1/check only, not on /v1/analyze (the website, the
browser extension, the phone app). It is for trying Unfret out: one per network a day, and developer passes' free
checks have their own daily budget: once it is used, a developer pass with only free checks gets 402
INSUFFICIENT_CREDITS until 00:00 UTC (other passes with only free checks: 429 FREE_CHECKS_PAUSED). Buy more checks for
a pass at {url}/checkout; bought checks work everywhere. GET {url}/v1/credits with the pass answers
`checks_remaining`, `free_checks_remaining`, `api_free_checks_remaining` (a developer pass's), `free_checks_paused`
and `balance_usd_micros`.

## Response

{{"action": "allow|warn|block", "risk_score": 0..1, "confidence": 0..1, "flags": [{{"type", "evidence", "weight"}}],
"policy_applied", "tier", "cached", "checked_at", "model_version", "band"}}. "warn" means: ask your human before
buying.
Under the balanced and lenient policies you are never told to warn about a page a shopper would be shown as
"reliable" or "concerns" (the band); choose strict to be warned about those too. A shopper reads the four bands
reliable, concerns, careful and junk_or_scam as "Looks fine", "Worth a look", "Think twice" and "Serious concerns".

## Errors

Every error is JSON with `detail` and `error_code`.

{_errors_table(base_on)}

With a pass, also: `TOO_MANY_REQUESTS` (429, not charged: over a pass's per-minute limit for quick checks or pages
Unfret fetches; wait `metadata.retry_after_s`). POST /v1/keys: `BAD_CHALLENGE` (400: wrong, expired, used or unsolved
challenge; get a new one) and `TOO_MANY_REQUESTS` (429: too many new passes from this network today).

## Clients

- Python: `unfret-client`: check(url) gives allow, warn or block, and a developer pass takes one call.
  {clients_info.python_install_note()}
- Any other language: plain HTTPS, as above. {url}/developers/http has the request, the response, the errors, the
  proof of work for a developer pass and examples in curl, JavaScript, Go and PHP. A TypeScript reference client
  without dependencies is in the repository, to copy: {clients_info.TYPESCRIPT_CLIENT_URL} (mirror:
  {clients_info.TYPESCRIPT_CLIENT_MIRROR}); it is not on npm.

## Shared results (shoppers)

A shopper's result from POST /v1/analyze carries a share_token. POST /v1/shares/preview, then POST /v1/shares,
with {{"share_token", "url", "result"}} and without Authorization, make the verdict public at /r/<id> for 90 days
(no score, no quotes from the page, nothing about who checked); DELETE /v1/shares/<id> with X-Delete-Code removes
it. Shared pages are noindex. Don't present a shared result as Unfret's own judgement of the shop.

## Machine-readable

- {url}/.well-known/x402: x402 discovery (tiers, prices, networks, assets, policies, the pass)
- {url}/openapi.json (and {url}/docs): every route
- {url}/developers: the developer guide, for people
"""
