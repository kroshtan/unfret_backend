import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.callbacks import UsageMetadataCallbackHandler

from veridex import result_cache
from veridex.auth import require_wallet
from veridex.config import settings
from veridex.credits import ledger
from veridex.graph.state import Finding
from veridex.schemas.errors import GatewayTimeoutError, InsufficientCreditsError, InternalServerError
from veridex.schemas.requests import AnalyzeRequest
from veridex.schemas.responses import AnalyzeResponse

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/analyze", tags=["Analyze"])

_SKILL_TAG_RE = re.compile(r"\[([^\]]+)\]")


async def _prepare(body: AnalyzeRequest, request: Request, address: str) -> tuple[Any, dict[str, object], int]:
    """
    Check the graph, take the analysis price from the wallet's balance, and build the graph input.

    :param body: The request body.
    :param request: The FastAPI request.
    :param address: The signed-in wallet.
    :return: The compiled graph, its input state and the balance left after the debit.
    :raises InternalServerError: If the graph is not initialised.
    :raises InsufficientCreditsError: If the balance doesn't cover the analysis.
    """
    graph = request.app.graph
    if graph is None:
        raise InternalServerError("Analysis graph is not initialised.")
    price = settings.analysis_price_lamports
    left = await ledger.debit(request.app.db_pool, address, price)
    if left is None:
        balance = await ledger.balance(request.app.db_pool, address) or 0
        raise InsufficientCreditsError(
            "Not enough credits for an analysis.", metadata={"balance_lamports": balance, "analysis_lamports": price}
        )
    state: dict[str, object] = {"page_content": body.page_content or "", "url": body.url, "flags": body.flags}
    if body.page_facts is not None:
        state["page_facts"] = body.page_facts
    if body.checkout_content:
        state["checkout_content"] = body.checkout_content
    return graph, state, left


async def _refund(request: Request, address: str) -> int:
    # Pages that couldn't be analysed (score -1) and failed analyses cost nothing.
    return await ledger.credit(request.app.db_pool, address, settings.analysis_price_lamports)


def _cache_key(body: AnalyzeRequest) -> str | None:
    # A supplied checkout page changes the result, so those analyses aren't cached.
    if body.checkout_content:
        return None
    content = body.page_content or (body.page_facts.model_dump_json() if body.page_facts else "")
    return f"{result_cache.listing_key(body.url, content)}|{','.join(sorted(body.flags))}"


def _log_usage(usage: UsageMetadataCallbackHandler) -> None:
    # Token counts per model, for pricing. Deliberately nothing that identifies the page or the caller.
    for model, tokens in usage.usage_metadata.items():
        logger.info(
            "analysis_llm_usage",
            model=model,
            input_tokens=tokens.get("input_tokens", 0),
            output_tokens=tokens.get("output_tokens", 0),
        )


@router.post("", response_model=AnalyzeResponse, response_model_exclude_none=True)
async def analyze_page(
    body: AnalyzeRequest,
    request: Request,
    address: str = Depends(require_wallet),
) -> AnalyzeResponse:
    """
    Analyse a product web page for legitimacy using the LangGraph analysis pipeline, paid from the wallet's credits.

    Passes the page (client-supplied facts and/or HTML or cleaned text) through the graph, which extracts the
    facts, runs the cheap skills in parallel, lets the triage decide on expensive ones, then synthesises a
    reliability score and explanation. The analysis price is taken from the balance up front and refunded when the
    page can't be analysed (score -1) or the analysis fails. The same listing analysed again within the hour is
    served from an in-memory cache. Nothing about the page or the result is stored.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param address: The signed-in wallet (injected by require_wallet).
    :return: Reliability score (0–100), explanation, the scorer's assessment and the balance left.
    :raises InternalServerError: If the graph fails.
    :raises GatewayTimeoutError: If the analysis takes longer than ``analysis_timeout_s``.
    """
    graph, state, left = await _prepare(body, request, address)
    key = _cache_key(body)
    result = result_cache.get(key) if key else None
    if result is None:
        usage = UsageMetadataCallbackHandler()
        try:
            async with asyncio.timeout(settings.analysis_timeout_s):
                result = await graph.ainvoke(state, config={"callbacks": [usage]})
        except TimeoutError as exc:
            logger.warning("graph_timed_out", timeout_s=settings.analysis_timeout_s)
            await _refund(request, address)
            raise GatewayTimeoutError("Analysis took too long.") from exc
        except Exception as exc:
            logger.exception("graph_execution_failed")
            await _refund(request, address)
            raise InternalServerError("Analysis failed.") from exc
        finally:
            _log_usage(usage)
        if key:
            result_cache.put(key, {k: result[k] for k in ("score", "explanation", "assessment") if k in result})
        skill_counts = Counter(
            m.group(1) for entry in result.get("skill_results", []) if (m := _SKILL_TAG_RE.match(entry))
        )
        logger.info("skills_used", **skill_counts)
    else:
        logger.info("analysis_cache_hit")

    score: int = result["score"]
    if score == -1:
        left = await _refund(request, address)
    return AnalyzeResponse(
        score=score, explanation=result["explanation"], assessment=result.get("assessment"), balance_lamports=left
    )


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _update_events(update: dict[str, Any], final: dict[str, Any]) -> list[str]:
    # One LangGraph "updates" chunk ({node: state update}) → one `step` event per finished node (contract v1:
    # {"skill", "findings"}, plus "page_type" from the classifier); the score is collected in `final` for `result`.
    events: list[str] = []
    for node, values in update.items():
        if not isinstance(values, dict):
            continue
        final |= {k: values[k] for k in ("score", "explanation", "assessment") if k in values}
        if "score" in values:
            continue  # the judge (or the early exit): reported as the `result` event
        findings = [f.model_dump() if isinstance(f, Finding) else dict(f) for f in values.get("findings", []) or []]
        step: dict[str, Any] = {"skill": node, "findings": findings}
        if "page_type" in values:
            step["page_type"] = values["page_type"]
        events.append(_event("step", step))
    return events


@router.post("/stream", response_class=StreamingResponse)
async def analyze_page_stream(
    body: AnalyzeRequest,
    request: Request,
    address: str = Depends(require_wallet),
) -> StreamingResponse:
    """
    Analyse a page like ``POST /v1/analyze``, streaming the findings as server-sent events while skills finish.

    Events: one ``step`` per finished node (``{"skill", "findings"}``, with ``page_type`` once classified), then a
    final ``result`` (the ``POST /v1/analyze`` body) or ``error``. A client (the browser extension) can show progress
    instead of a spinner. Charged like ``POST /v1/analyze``: refunded on ``error``, a score of -1, or when the stream
    ends before the result was sent.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param address: The signed-in wallet (injected by require_wallet).
    :return: A ``text/event-stream`` response.
    """
    graph, state, left = await _prepare(body, request, address)

    async def events() -> AsyncIterator[str]:
        usage = UsageMetadataCallbackHandler()
        final: dict[str, Any] = {}
        refund = True  # until a charged result has been handed over
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
            balance = left
            if final["score"] == -1:
                refund = False
                balance = await _refund(request, address)
            result = AnalyzeResponse(
                score=final["score"],
                explanation=final["explanation"],
                assessment=final.get("assessment"),
                balance_lamports=balance,
            )
            refund = False
            yield _event("result", result.model_dump(mode="json", exclude_none=True))
        finally:
            if refund:
                await _refund(request, address)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
