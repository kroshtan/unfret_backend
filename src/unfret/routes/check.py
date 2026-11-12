"""
``POST /v1/check``: pay-per-check for AI shopping agents over x402 (v2, ``exact`` scheme, wSOL or USDC on Solana).

Flow (``upfront``): no ``PAYMENT-SIGNATURE`` header → 402 with ``PAYMENT-REQUIRED``. With one: verify it strictly,
fetch the page if only a URL was given (before any money moves), co-sign as fee payer (the hot wallet, which is also
``payTo``), submit and confirm, then run the analysis and answer with the verdict and ``PAYMENT-RESPONSE``. Nothing
about the payment or the page is stored.
"""

import asyncio
import json
import time
import traceback
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from unfret import __version__, verdict_store
from unfret.config import settings
from unfret.credits import ledger
from unfret.net import fetch_public
from unfret.page_fetch import is_challenge_page
from unfret.prices import format_usd
from unfret.visitors import RateLimit
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

router = APIRouter(tags=["x402"])

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
        "serviceName": "Unfret",
    }


def _payment_required(accepts: list[dict[str, Any]], error: str, reason: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {
        "x402Version": 2,
        "error": error,
        "error_code": "PAYMENT_REQUIRED",
        "resource": _resource(),
        "accepts": accepts,
    }
    if reason:  # a finer cause the client can act on, e.g. why the payment's simulation failed
        body["reason"] = reason
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


async def _page_html(body: CheckRequest) -> str:
    if body.page_content:
        return body.page_content
    raw = await fetch_public(body.url, max_bytes=settings.max_page_bytes)
    return raw.decode("utf-8", errors="replace")


async def _readable_page(body: CheckRequest) -> str | Response:
    """
    The page to analyse, or the (free) error response when it can't be read.

    :param body: The request body.
    :return: The HTML, or a 422 response.
    """
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
    if is_challenge_page(html):
        return JSONResponse(
            {
                "detail": "The page is a bot check, not the store; send the real page as page_content. "
                "You were not charged.",
                "error_code": "PAGE_BLOCKED",
            },
            status_code=422,
        )
    return html


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


@router.post("/v1/check", response_model=Verdict, responses={402: {"description": "Payment required (x402)"}})
async def check(body: CheckRequest, request: Request) -> Response:  # noqa: C901, PLR0911
    """
    Check a store or product page for scam, dropshipping and dark-pattern signals; paid per call over x402.

    Retrying with the same payment returns the same answer (for 15 minutes) and is never charged twice.

    :param body: URL, optional page HTML, tier and policy.
    :param request: The incoming request (payment header, app state).
    :return: The verdict, or an x402 payment-required / error response.
    """
    ip = request.client.host if request.client else "unknown"
    if (wait := _rate.retry_after(ip)) is not None:
        return JSONResponse(
            {"detail": "Too many requests; slow down.", "error_code": "RATE_LIMITED"},
            status_code=429,
            headers={"Retry-After": str(max(1, int(wait) + 1))},
        )
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
        return _payment_required(accepts, exc.reason, exc.cause)

    key = str(pre.transaction.signatures[1])
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
        outcome = await _paid_check(body, request, x402, pre, accepts=accepts, ip=ip)
    finally:
        if outcome is None or isinstance(outcome, Response):  # refused before money moved: a retry starts over
            _replays.pop(key, None)
            answer.cancel()
    if isinstance(outcome, Response):
        return outcome
    answer.set_result(outcome)
    status, content, headers = outcome
    return JSONResponse(content, status_code=status, headers=headers)


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
        page = await _readable_page(body)
        if isinstance(page, Response):
            return page
        html = page
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


def _failure_site(exc: BaseException, frames: int = 6) -> list[str]:
    """
    Say where an exception was raised, without its message.

    Messages can carry the analysed URL (HTTP errors name the request), which must never be logged; the innermost
    frames' file, line and function are enough to find the cause.

    :param exc: The exception.
    :param frames: How many of the innermost frames to keep.
    :return: ``file:line:function`` for each, outermost first.
    """
    stack = traceback.extract_tb(exc.__traceback__)[-frames:]
    return [f"{f.filename.rsplit('/site-packages/', 1)[-1].rsplit('/src/', 1)[-1]}:{f.lineno}:{f.name}" for f in stack]


def _add_refund(content: dict[str, Any], refunded: int, payer: str) -> None:
    if refunded:
        content["refunded_usd_micros"] = refunded
        content["refunded_lamports"] = refunded  # deprecated name of the same micro-dollar credit
        content["refund"] = f"Credited to {payer}; sign in with that wallet at {settings.public_base_url}/connect."


def _json(response: Response) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(bytes(response.body))
    return decoded


# error_code → (HTTP status, charged?, what to do). Published in /llms.txt and the README.
ERRORS: dict[str, tuple[int, str, str]] = {
    "PAYMENT_REQUIRED": (
        402,
        "no",
        "pay one of `accepts` and retry with PAYMENT-SIGNATURE; `error` says why (`reason`: why a simulation failed)",
    ),
    "INVALID_PAYMENT": (400, "no", "the PAYMENT-SIGNATURE header couldn't be decoded"),
    "VALIDATION_ERROR": (422, "no", "fix the request body (see `errors`)"),
    "PAGE_UNAVAILABLE": (422, "no", "the page couldn't be fetched: send page_content"),
    "PAGE_BLOCKED": (422, "no", "the page is a bot check: send the real page as page_content"),
    "RATE_LIMITED": (429, "no", "wait Retry-After seconds"),
    "PRICE_UNAVAILABLE": (503, "no", "nothing can be priced right now: retry after Retry-After"),
    "SETTLEMENTS_PAUSED": (503, "no", "too many failed settlements: retry after Retry-After"),
    "SERVICE_UNAVAILABLE": (503, "no", "payments or analysis are off: retry later"),
    "SETTLEMENT_FAILED": (402, "no", "the network refused the payment: pay again"),
    "SETTLEMENT_PENDING": (402, "maybe", "don't pay again; if it lands within 2 min its price is credited"),
    "SETTLEMENT_MISMATCH": (402, "yes", "it landed differently than simulated: no verdict (alerted)"),
    "GATEWAY_TIMEOUT": (504, "refunded", "the analysis timed out: the price is credited to the paying wallet"),
    "INTERNAL_ERROR": (500, "refunded", "the analysis failed: the price is credited to the paying wallet"),
}


def _errors_table() -> str:
    rows = [f"| `{code}` | {status} | {charged} | {action} |" for code, (status, charged, action) in ERRORS.items()]
    return "\n".join(["| error_code | HTTP | charged | what to do |", "| --- | --- | --- | --- |", *rows])


def _discovery() -> dict[str, Any]:
    mint = usdc_mint(settings.solana_network, settings.usdc_mint)
    return {
        "x402Version": 2,
        "service": "Unfret",
        "version": __version__,
        "endpoint": f"{settings.public_base_url}/v1/check",
        "method": "POST",
        "scheme": "exact",
        "network": CAIP2[settings.solana_network],  # CAIP-2, the same identifier as accepts[].network
        "network_name": settings.solana_network,
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
    return f"""# Unfret

> Pay-per-check risk signals for AI shopping agents: is this store or product listing showing signs of scams,
> dropshipping of generic goods, fake reviews or dark patterns? Returns evidence and an action, never a "scam" label.

## Endpoint

POST {d["endpoint"]} with JSON {{"url": "...", "page_content": "<html> (optional)", "tier": "quick|deep",
"policy": "strict|balanced|lenient"}}. Send page_content for marketplaces (Amazon blocks server-side fetches).

## Tiers

- quick ({quick}): {d["tiers"]["quick"]["includes"]} Use it to screen many pages; it misses what only reading
  the listing, its reviews or its photos shows, so check what matters with deep. A page without a product and a
  price can't be assessed by quick (you are refunded).
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
same answer and is never charged twice.

## Response

{{"action": "allow|warn|block", "risk_score": 0..1, "confidence": 0..1, "flags": [{{"type", "evidence", "weight"}}],
"policy_applied", "tier", "cached", "checked_at", "model_version", "band"}}. "warn" means: ask your human before
buying.
Under the balanced and lenient policies you are never told to warn about a page a shopper would be shown as
"reliable" or "concerns" (the band); choose strict to be warned about those too.

## Errors

Every error is JSON with `detail` and `error_code`.

{_errors_table()}

## Shared results (shoppers)

A shopper's result from POST /v1/analyze carries a share_token. POST /v1/shares/preview, then POST /v1/shares,
with {{"share_token", "url", "result"}} and without Authorization, make the verdict public at /r/<id> for 90 days
(no score, no quotes from the page, nothing about who checked); DELETE /v1/shares/<id> with X-Delete-Code removes
it. Shared pages are noindex. Don't present a shared result as Unfret's own judgement of the shop.

## Machine-readable

{settings.public_base_url}/.well-known/x402
"""
