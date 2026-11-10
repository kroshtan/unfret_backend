import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import anyio
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.callbacks import UsageMetadataCallbackHandler

from unfret import page_fetch, reports, result_cache, shopper
from unfret.auth import require_account
from unfret.config import settings
from unfret.credits import ledger
from unfret.graph.state import Finding
from unfret.schemas.errors import (
    GatewayTimeoutError,
    InsufficientCreditsError,
    InternalServerError,
    TooManyRequestsError,
    UnreadablePageError,
)
from unfret.schemas.requests import AnalyzeRequest
from unfret.schemas.responses import AnalyzeResponse
from unfret.visitors import RateLimit

logger = structlog.get_logger("unfret")

router = APIRouter(prefix="/v1/analyze", tags=["Analyze"])

_SKILL_TAG_RE = re.compile(r"\[([^\]]+)\]")
_RESULT_KEYS = ("score", "explanation", "assessment", "verdict")
# Pages fetched for URL-only requests, per account: a page we can't read is free, so this bounds the free fetches.
_fetch_rate = RateLimit(settings.analyze_fetch_per_minute)


def _graph_input(body: AnalyzeRequest, request: Request) -> tuple[Any, dict[str, object]]:
    """
    Check the graph and build its input state.

    :param body: The request body.
    :param request: The FastAPI request.
    :return: The compiled graph and its input state.
    :raises InternalServerError: If the graph is not initialised.
    """
    graph = request.app.graph
    if graph is None:
        raise InternalServerError("Analysis graph is not initialised.")
    state: dict[str, object] = {
        "page_content": body.page_content or "",
        "url": body.url,
        "flags": body.flags,
        "language": shopper.pick_language(request.headers.get("accept-language")),
    }
    if body.page_facts is not None:
        state["page_facts"] = body.page_facts
    if body.checkout_content:
        state["checkout_content"] = body.checkout_content
    return graph, state


_NOT_ENOUGH = "Not enough credits for an analysis."


def _shortfall(balance: int) -> dict[str, int]:
    price = settings.analysis_price_usd_micros
    return {
        "balance_usd_micros": balance,
        "analysis_usd_micros": price,
        # Deprecated aliases (same values), kept for extension versions up to 0.5.0.
        "balance_lamports": balance,
        "analysis_lamports": price,
    }


async def _debit(request: Request, address: str) -> int:
    """
    Take the analysis price from the account's balance.

    :param request: The FastAPI request.
    :param address: The account (a signed-in wallet or an API key).
    :return: The balance left after the debit.
    :raises InsufficientCreditsError: If the balance doesn't cover the analysis.
    """
    left = await ledger.debit(request.app.db_pool, address, settings.analysis_price_usd_micros)
    if left is None:
        balance = await ledger.balance(request.app.db_pool, address) or 0
        raise InsufficientCreditsError(_NOT_ENOUGH, metadata=_shortfall(balance))
    return left


async def _fetch_if_url_only(request: Request, body: AnalyzeRequest, address: str, state: dict[str, object]) -> None:
    """
    For a request with only a URL, fetch the page into the graph input, before anything is charged.

    The balance is checked first (an empty account gets its 402 without a fetch), and a page that can't be read
    (unreachable, refused, a bot check, an empty shell) is answered with a free 422. Nothing about the URL is logged.

    :param request: The FastAPI request.
    :param body: The request body.
    :param address: The account.
    :param state: The graph input; its ``page_content`` is set to the fetched HTML.
    :raises InsufficientCreditsError: If the balance doesn't cover the analysis.
    :raises TooManyRequestsError: If the account fetched too many pages in the last minute.
    :raises UnreadablePageError: If the page can't be checked from a server-side fetch (``error_code`` says why).
    """
    if not body.url_only:
        return
    balance = await _balance(request, address)
    if balance < settings.analysis_price_usd_micros:
        raise InsufficientCreditsError(_NOT_ENOUGH, metadata=_shortfall(balance))
    if (wait := _fetch_rate.retry_after(address)) is not None:
        raise TooManyRequestsError(
            "Too many links checked in a minute; wait a moment.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )
    try:
        state["page_content"] = await page_fetch.fetch_page(body.url.strip())
    except page_fetch.PageUnreadableError as exc:
        logger.info("analysis_page_unreadable", reason=exc.reason)
        raise UnreadablePageError(f"{exc.detail} No check was used.", error_code=exc.reason) from exc


async def _refund(request: Request, address: str) -> int:
    # Pages that couldn't be analysed (score -1) and failed analyses cost nothing. Shielded: this often runs while
    # a client disconnect is cancelling the request, and a cancelled refund would keep the money.
    with anyio.CancelScope(shield=True):
        balance = await ledger.credit(request.app.db_pool, address, settings.analysis_price_usd_micros)
    return balance


async def _balance(request: Request, address: str) -> int:
    return await ledger.balance(request.app.db_pool, address) or 0


def _cache_key(body: AnalyzeRequest, language: str) -> str:
    # Keyed on exactly what was sent (see unfret.result_cache): a result is only reused for identical input, so
    # one caller's crafted facts are never served to another.
    return result_cache.key_for(
        url=body.url,
        page_content=body.page_content or "",
        page_facts_json=body.page_facts.model_dump_json() if body.page_facts else "",
        checkout_content=body.checkout_content or "",
        tier="full",
        language=language,
        flags=body.flags,
    )


def _log_usage(usage: UsageMetadataCallbackHandler) -> None:
    # Token counts per model, for pricing. Deliberately nothing that identifies the page or the caller.
    for model, tokens in usage.usage_metadata.items():
        logger.info(
            "analysis_llm_usage",
            model=model,
            input_tokens=tokens.get("input_tokens", 0),
            output_tokens=tokens.get("output_tokens", 0),
        )


def _report_token(body: AnalyzeRequest, address: str, result: dict[str, Any]) -> str | None:
    # Lets this account report the result later (POST /v1/reports) without anything being stored now.
    return reports.issue_token(
        settings.session_secret.get_secret_value(),
        address,
        body,
        score=int(result["score"]),
        explanation=str(result["explanation"]),
        ttl_s=settings.report_window_s,
    )


def _response(
    result: dict[str, Any], balance: int, *, charged: bool, report_token: str | None = None
) -> AnalyzeResponse:
    return AnalyzeResponse(
        score=result["score"],
        explanation=result["explanation"],
        assessment=result.get("assessment"),
        verdict=result.get("verdict"),
        balance_usd_micros=balance,
        balance_lamports=balance,
        charged=charged,
        report_token=report_token,
    )


@router.post("", response_model=AnalyzeResponse, response_model_exclude_none=True)
async def analyze_page(
    body: AnalyzeRequest,
    request: Request,
    address: str = Depends(require_account),
) -> AnalyzeResponse:
    """
    Analyse a product web page for legitimacy using the LangGraph analysis pipeline, paid from the account's credits.

    Passes the page (client-supplied facts and/or HTML or cleaned text) through the graph, which extracts the
    facts, runs the cheap skills in parallel, lets the triage decide on expensive ones, then synthesises a
    reliability score, an explanation and the shopper verdict (in the ``Accept-Language`` language). The analysis
    price is taken from the balance up front and refunded when the page can't be analysed (score -1) or the
    analysis fails. The same input analysed again within the hour is served from an in-memory cache, free of
    charge. Nothing about the page or the result is stored; the ``report_token`` lets the caller report a wrong
    result later (``POST /v1/reports``), sharing the page only then and only with their consent.

    A request with only a ``url`` has the server fetch the page first (the web link checker). A page it can't check
    that way is answered with a free 422: ``PAGE_UNAVAILABLE`` (couldn't be fetched), ``PAGE_BLOCKED`` (the shop
    refuses automatic visitors, or answered with a bot check), ``PAGE_TOO_THIN`` (an empty shell that needs a
    browser) or ``INVALID_URL``.

    The caller is a wallet's session (``Authorization: Bearer <session token>``) or an API key (``Bearer ufk_…``).

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param address: The account (injected by require_account).
    :return: Reliability score (0–100), explanation, verdict, the scorer's assessment, the balance left and a
        report token.
    :raises InternalServerError: If the graph fails.
    :raises GatewayTimeoutError: If the analysis takes longer than ``analysis_timeout_s``.
    :raises asyncio.CancelledError: If the client hung up (after the refund).
    """
    graph, state = _graph_input(body, request)
    key = _cache_key(body, str(state["language"]))
    cached = result_cache.get(key)
    if cached is not None:
        logger.info("analysis_cache_hit")
        token = _report_token(body, address, cached)
        return _response(cached, await _balance(request, address), charged=False, report_token=token)

    await _fetch_if_url_only(request, body, address, state)
    left = await _debit(request, address)
    usage = UsageMetadataCallbackHandler()
    try:
        async with asyncio.timeout(settings.analysis_timeout_s):
            result = await graph.ainvoke(state, config={"callbacks": [usage]})
    except TimeoutError as exc:
        logger.warning("graph_timed_out", timeout_s=settings.analysis_timeout_s)
        await _refund(request, address)
        raise GatewayTimeoutError("Analysis took too long.") from exc
    except asyncio.CancelledError:
        await _refund(request, address)  # the client went away before the result
        raise
    except Exception as exc:
        logger.exception("graph_execution_failed")
        await _refund(request, address)
        raise InternalServerError("Analysis failed.") from exc
    finally:
        _log_usage(usage)
    result_cache.put(key, {k: result[k] for k in _RESULT_KEYS if k in result})
    skill_counts = Counter(
        m.group(1) for entry in result.get("skill_results", []) if (m := _SKILL_TAG_RE.match(entry))
    )
    logger.info("skills_used", **skill_counts)

    if result["score"] == -1:
        return _response(result, await _refund(request, address), charged=False)
    return _response(result, left, charged=True, report_token=_report_token(body, address, result))


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _update_events(update: dict[str, Any], final: dict[str, Any]) -> list[str]:
    # One LangGraph "updates" chunk ({node: state update}) → one `step` event per finished node (contract v1:
    # {"skill", "findings"}, plus "page_type" from the classifier); the score is collected in `final` for `result`.
    # Steps carry progress only: which skill finished and the types of what it found, never the evidence. The
    # evidence comes with the (charged) result, so reading the steps and hanging up early gets nothing for free.
    events: list[str] = []
    for node, values in update.items():
        if not isinstance(values, dict):
            continue
        final |= {k: values[k] for k in _RESULT_KEYS if k in values}
        if "score" in values:
            continue  # the judge (or the early exit): reported as the `result` event
        found = [f.model_dump() if isinstance(f, Finding) else dict(f) for f in values.get("findings", []) or []]
        step: dict[str, Any] = {
            "skill": node,
            "findings": [{"skill": f.get("skill", node), "type": f.get("type", "")} for f in found],
        }
        if "page_type" in values:
            step["page_type"] = values["page_type"]
        events.append(_event("step", step))
    return events


@router.post("/stream", response_class=StreamingResponse)
async def analyze_page_stream(
    body: AnalyzeRequest,
    request: Request,
    address: str = Depends(require_account),
) -> StreamingResponse:
    """
    Analyse a page like ``POST /v1/analyze``, streaming progress as server-sent events while skills finish.

    Events: one ``step`` per finished node (``{"skill", "findings"}`` with the finding types, plus ``page_type``
    once classified), then a final ``result`` (the ``POST /v1/analyze`` body, with all the evidence) or ``error``.
    Charged like ``POST /v1/analyze``: free on a cache hit (the result comes at once), refunded on ``error``, a score
    of -1, or when the client hangs up before the analysis finished. A finished result is cached first, so a client
    that lost the connection gets it again for free by repeating the request. A URL-only request fetches the page
    before the stream starts; a page that can't be checked is a free 422 (no stream), as for ``POST /v1/analyze``.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param address: The account (injected by require_account).
    :return: A ``text/event-stream`` response.
    """
    graph, state = _graph_input(body, request)
    key = _cache_key(body, str(state["language"]))
    cached = result_cache.get(key)
    if cached is not None:
        logger.info("analysis_cache_hit")
        token = _report_token(body, address, cached)
        hit = _response(cached, await _balance(request, address), charged=False, report_token=token)
        return _stream(_replay(hit))

    await _fetch_if_url_only(request, body, address, state)
    left = await _debit(request, address)
    return _stream(_live_events(request, body, address, graph, state, key=key, left=left))


def _stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-store"})


async def _replay(hit: AnalyzeResponse) -> AsyncIterator[str]:
    yield _event("result", hit.model_dump(mode="json", exclude_none=True))


async def _live_events(
    request: Request,
    body: AnalyzeRequest,
    address: str,
    graph: Any,
    state: dict[str, object],
    *,
    key: str,
    left: int,
) -> AsyncGenerator[str]:
    """
    Run the graph and stream its steps, then the result; refund unless the analysis finished.

    :param request: The FastAPI request.
    :param body: The request body (for the report token).
    :param address: The account (already debited).
    :param graph: The compiled graph.
    :param state: The graph input.
    :param key: The result cache key.
    :param left: The balance after the debit.
    :yield: Server-sent events.
    """
    usage = UsageMetadataCallbackHandler()
    final: dict[str, Any] = {}
    refund = True  # until the analysis has finished (its result is then cached and handed over)
    try:
        try:
            async with asyncio.timeout(settings.analysis_timeout_s):
                async for update in graph.astream(state, config={"callbacks": [usage]}, stream_mode="updates"):
                    for event in _update_events(update, final):
                        yield event
        except TimeoutError:
            logger.warning("graph_timed_out", timeout_s=settings.analysis_timeout_s)
            yield _event("error", {"detail": "Analysis took too long.", "error_code": "GATEWAY_TIMEOUT"})
            return
        except Exception:
            logger.exception("graph_execution_failed")
            yield _event("error", {"detail": "Analysis failed.", "error_code": "INTERNAL_ERROR"})
            return
        finally:
            _log_usage(usage)
        if "score" not in final:
            yield _event("error", {"detail": "Analysis failed.", "error_code": "INTERNAL_ERROR"})
            return
        result_cache.put(key, dict(final))
        refund = False
        if final["score"] == -1:
            result = _response(final, await _refund(request, address), charged=False)
        else:
            result = _response(final, left, charged=True, report_token=_report_token(body, address, final))
        yield _event("result", result.model_dump(mode="json", exclude_none=True))
    finally:
        if refund:
            await _refund(request, address)
