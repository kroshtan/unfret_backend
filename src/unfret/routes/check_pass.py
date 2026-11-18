"""
``POST /v1/check`` paid with an Unfret pass (``Authorization: Bearer ufk_…``) instead of an x402 payment.

For agents that have a pass rather than a wallet. The request and the verdict are the same as with x402; the charge
follows ``POST /v1/analyze`` exactly (:mod:`unfret.routes.analyze`, whose helpers this uses, so the two can't drift):

- a **deep** check uses one check: the pass's free checks first (:mod:`unfret.credits.free`, the one daily budget),
  then its balance. Without either: 402 ``INSUFFICIENT_CREDITS``; with only free checks while today's budget is
  spent: 429 ``FREE_CHECKS_PAUSED``. A page that can't be read (422), assessed (score -1) or analysed (500/504) is
  given back.
- a **quick** check is free, within the per-minute limit quick checks have on ``/v1/analyze`` (shared per pass).
- ``/v1/check``'s own per-minute limit counts per pass here, not per visitor network, so agents behind one host (a
  hosted MCP client, the remote MCP server) don't share it; bad passes count per network.
- a **cached** answer (the same input within the hour, or the listing's answer of the last day, the x402 fast path)
  costs a deep check like a fresh one, except for the pass that paid for that listing within the hour.

The verdict carries four more fields than the x402 one: ``charged``, ``free_check``, ``balance_usd_micros`` and
``free_checks_left``. Nothing about the page or the caller is stored beyond what ``/v1/analyze`` keeps.
"""

import asyncio
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from unfret import card, result_cache, verdict_store
from unfret.auth import get_credits
from unfret.config import settings
from unfret.credits import affiliates, free, keys
from unfret.routes import analyze
from unfret.routes.check import CheckRequest, _failure_site, _rate, _readable_page, rate_limited
from unfret.schemas.errors import ServiceUnavailableError, TooManyRequestsError, UnauthorizedError
from unfret.visitors import RateLimit
from unfret.x402.verdict import Verdict, action_for, to_verdict

logger = structlog.get_logger("unfret")

# Requests with a pass that didn't authenticate, per visitor network: past it, 429 without looking passes up.
_bad_passes = RateLimit(settings.x402_check_per_minute)


async def authenticate(request: Request, token: str) -> str:
    """
    The pass behind a bearer token.

    :param request: The incoming request.
    :param token: What followed ``Bearer``.
    :return: The pass's ledger account (``key:<id>``).
    :raises ServiceUnavailableError: While passes are off.
    :raises UnauthorizedError: For anything but a valid pass (a wallet's session token doesn't pay ``/v1/check``).
    """
    if request.app.db_pool is None or not get_credits(request).sign_in_enabled:
        raise ServiceUnavailableError("Unfret passes are not available right now.")
    try:
        return await keys.authenticate(request.app.db_pool, token)
    except keys.ApiKeyError as exc:
        raise UnauthorizedError(
            f"{exc} Send an Unfret pass (Authorization: Bearer ufk_…), or pay with x402 instead.",
            error_code="INVALID_API_KEY",
        ) from exc


def _cache_key(body: CheckRequest) -> str:
    # Exactly what was sent (as /v1/analyze keys it), in its own tier namespace: a verdict, not an analysis result.
    return result_cache.key_for(url=body.url, page_content=body.page_content or "", tier=f"check-{body.tier}")


def _recall_cached(body: CheckRequest, key: str) -> Verdict | None:
    kept = result_cache.get(key)
    if kept is None or "verdict" not in kept:
        return None
    verdict = Verdict.model_validate(kept["verdict"])
    risk = verdict.risk_score if verdict.risk_score is not None else 1.0
    action = action_for(risk, body.policy, verdict.band) if verdict.assessed else verdict.action
    return verdict.model_copy(update={"action": action, "policy_applied": body.policy, "cached": True})


async def _answer(
    request: Request, verdict: Verdict, charge: analyze.Charge, *, account: str, charged: bool
) -> JSONResponse:
    """
    The verdict, with what it cost the pass and what the pass has left.

    :param request: The incoming request.
    :param verdict: The verdict.
    :param charge: What was taken.
    :param account: The pass's account.
    :param charged: Whether the check counted (``False`` when it was given back or free).
    :return: The response.
    """
    balance = await analyze._balance(request, account)
    if verdict.deep_check is not None and verdict.deep_check.suggested:
        # A quick answer's suggestion, priced for this pass rather than at the x402 price.
        cost = await analyze.deep_cost(request, account, balance)
        verdict = verdict.model_copy(update={"deep_check": card.with_cost(verdict.deep_check, cost)})
    content: dict[str, Any] = verdict.model_dump(mode="json")
    content["charged"] = charged
    content["free_check"] = charged and charge.spent is not None
    content["balance_usd_micros"] = balance
    content["free_checks_left"] = await free.left(request.app.db_pool, account.removeprefix(keys.ACCOUNT_PREFIX))
    return JSONResponse(content)


async def _pay_for_hit(request: Request, body: CheckRequest, account: str, hit: Verdict) -> JSONResponse:
    """
    Answer from the cache, charged like a fresh check (see the module docstring).

    :param request: The incoming request.
    :param body: The request body.
    :param account: The pass's account.
    :param hit: The cached verdict.
    :return: The response.
    """
    charge = analyze.Charge(account)
    if body.tier == "deep" and not result_cache.paid_for(account, body.url):
        if not hit.assessed:
            await analyze._can_pay(request, account)  # never charged, but only for a pass that could pay
        else:
            charge = await analyze._debit(request, account)
            await affiliates.first_check(request.app.db_pool, account)
            result_cache.mark_paid(account, body.url)
    logger.info("check_pass_cache_hit", tier=body.tier, charged=charge.charged)
    return await _answer(request, hit, charge, account=account, charged=charge.charged)


async def _pass(request: Request, body: CheckRequest, token: str) -> str | Response:
    """
    The pass behind the request, within its limits.

    :param request: The incoming request.
    :param body: The request body (its tier).
    :param token: The bearer token.
    :return: The pass's account, or a 429.
    :raises UnauthorizedError: For anything but a valid pass (counted per network).
    :raises TooManyRequestsError: Over the per-pass quick-check limit.
    """
    ip = request.client.host if request.client else "unknown"
    if (wait := _bad_passes.waiting(ip)) is not None:  # this network sent too many bad passes: no more lookups
        return rate_limited(wait)
    try:
        account = await authenticate(request, token)
    except UnauthorizedError:
        _bad_passes.retry_after(ip)
        raise
    if (wait := _rate.retry_after(account)) is not None:  # /v1/check's limit, per pass
        return rate_limited(wait)
    if body.tier == "quick" and (wait := analyze._quick_rate.retry_after(account)) is not None:
        raise TooManyRequestsError(
            "Too many quick checks in a minute; wait a moment.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )
    return account


async def pass_check(body: CheckRequest, request: Request, token: str) -> Response:
    """
    Run ``POST /v1/check`` for an Unfret pass.

    :param body: The request body.
    :param request: The incoming request.
    :param token: The bearer token (the pass).
    :return: The verdict (with the pass's fields), or a free error response.
    """
    account = await _pass(request, body, token)
    if isinstance(account, Response):
        return account
    return await _checked(body, request, account)


async def _checked(body: CheckRequest, request: Request, account: str) -> Response:  # noqa: C901
    """
    Answer the check for a pass: from the cache, or a fresh analysis.

    :param body: The request body.
    :param request: The incoming request.
    :param account: The pass's account.
    :return: The verdict (with the pass's fields), or a free error response.
    :raises TooManyRequestsError: Over a per-pass limit, or ``FREE_CHECKS_PAUSED``.
    :raises asyncio.CancelledError: If the client hung up (after the refund).
    """
    key = _cache_key(body)
    hit = _recall_cached(body, key)
    if hit is None and not body.page_content:
        hit = await verdict_store.recall(body.url, body.tier, body.policy)
    if hit is not None:
        return await _pay_for_hit(request, body, account, hit)

    if body.tier == "deep":
        analyze._check_free_outcomes(account)
        await analyze._can_pay(request, account)  # an empty pass gets its 402 before any fetch
    if not body.page_content and (wait := analyze._fetch_rate.retry_after(account)) is not None:
        raise TooManyRequestsError(
            "Too many links checked in a minute; wait a moment.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )
    page = await _readable_page(body)
    if isinstance(page, Response):
        return page
    graph = request.app.graph if body.tier == "deep" else request.app.quick_graph
    if graph is None:
        return JSONResponse({"detail": "Analysis is not available.", "error_code": "SERVICE_UNAVAILABLE"}, 503)

    charge = await analyze._debit(request, account) if body.tier == "deep" else analyze.Charge(account)
    await affiliates.first_check(request.app.db_pool, account)
    try:
        async with asyncio.timeout(settings.analysis_timeout_s):
            result = await graph.ainvoke({"page_content": page, "url": body.url, "flags": [], "explain": False})
    except asyncio.CancelledError:
        analyze._count_free_outcome(charge)
        await analyze._refund(request, charge)  # the client went away before the result
        raise
    except Exception as exc:  # noqa: BLE001 - answered as a free error, like /v1/analyze
        logger.warning("check_pass_analysis_failed", error=type(exc).__name__, where=_failure_site(exc))
        await analyze._refund(request, charge)
        timed_out = isinstance(exc, TimeoutError)
        return JSONResponse(
            {
                "detail": "The analysis " + ("timed out" if timed_out else "failed") + "; no check was used.",
                "error_code": "GATEWAY_TIMEOUT" if timed_out else "INTERNAL_ERROR",
            },
            status_code=504 if timed_out else 500,
        )

    verdict = to_verdict(result, policy=body.policy, tier=body.tier)
    result_cache.put(key, {"verdict": verdict.model_dump(mode="json")})
    if result.get("score") == -1:  # the page couldn't be assessed: given back
        analyze._count_free_outcome(charge)
        await analyze._refund(request, charge)
        return await _answer(request, verdict, charge, account=account, charged=False)
    await verdict_store.remember(body.url, result, verdict, fetched_by_us=not body.page_content)
    if charge.charged:
        result_cache.mark_paid(account, body.url)  # its repeat within the hour is free for this pass
    return await _answer(request, verdict, charge, account=account, charged=charge.charged)
