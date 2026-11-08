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
from veridex.auth import Account, verify_credentials
from veridex.config import settings
from veridex.db import count_analyses_today, log_analyze
from veridex.graph.state import Finding
from veridex.schemas.errors import GatewayTimeoutError, InternalServerError, TooManyRequestsError
from veridex.schemas.requests import AnalyzeRequest
from veridex.schemas.responses import AnalyzeResponse

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/analyze", tags=["Analyze"])

_SKILL_TAG_RE = re.compile(r"\[([^\]]+)\]")


async def _prepare(body: AnalyzeRequest, request: Request, account: Account) -> tuple[Any, dict[str, object]]:
    """
    Check the graph and the account's quota, and build the graph input.

    :param body: The request body.
    :param request: The FastAPI request.
    :param account: The authenticated account.
    :return: The compiled graph and its input state.
    :raises InternalServerError: If the graph is not initialised.
    :raises TooManyRequestsError: If the account's daily limit is reached.
    """
    graph = request.app.graph
    if graph is None:
        raise InternalServerError("Analysis graph is not initialised.")
    limit = account.daily_limit
    if limit is not None and await count_analyses_today(request.app.db_pool, account.username) >= limit:
        raise TooManyRequestsError(f"Daily limit of {limit} analyses reached.")
    state: dict[str, object] = {"page_content": body.page_content, "url": body.url, "flags": body.flags}
    if body.page_facts is not None:
        state["page_facts"] = body.page_facts
    if body.checkout_content:
        state["checkout_content"] = body.checkout_content
    return graph, state


def _cache_key(body: AnalyzeRequest) -> str | None:
    # A supplied checkout page changes the result, so those analyses aren't cached.
    if body.checkout_content:
        return None
    return f"{result_cache.listing_key(body.url, body.page_content)}|{','.join(sorted(body.flags))}"


def _log_usage(usage: UsageMetadataCallbackHandler) -> None:
    # Token counts per model, for pricing. Deliberately nothing that identifies the page or the caller.
    for model, tokens in usage.usage_metadata.items():
        logger.info(
            "analysis_llm_usage",
            model=model,
            input_tokens=tokens.get("input_tokens", 0),
            output_tokens=tokens.get("output_tokens", 0),
        )


@router.post("", response_model=AnalyzeResponse)
async def analyze_page(
    body: AnalyzeRequest,
    request: Request,
    account: Account = Depends(verify_credentials),
) -> AnalyzeResponse:
    """
    Analyse a product web page for legitimacy using the LangGraph analysis pipeline.

    Passes the raw HTML (or client-supplied page facts) and active flags through the graph, which extracts the
    facts, runs the cheap skills in parallel, lets the triage decide on expensive ones, then synthesises a
    reliability score and explanation. The same listing analysed again within the hour is served from an
    in-memory cache. Results with a score other than -1 (not analysed) are persisted to the audit log and count
    towards the daily limit.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param account: The authenticated account (injected by verify_credentials).
    :return: Reliability score (0–100) and one-line explanation.
    :raises InternalServerError: If the graph fails.
    :raises GatewayTimeoutError: If the analysis takes longer than ``analysis_timeout_s``.
    """
    graph, state = await _prepare(body, request, account)
    key = _cache_key(body)
    result = result_cache.get(key) if key else None
    if result is None:
        usage = UsageMetadataCallbackHandler()
        try:
            async with asyncio.timeout(settings.analysis_timeout_s):
                result = await graph.ainvoke(state, config={"callbacks": [usage]})
        except TimeoutError as exc:
            logger.warning("graph_timed_out", timeout_s=settings.analysis_timeout_s)
            raise GatewayTimeoutError("Analysis took too long.") from exc
        except Exception as exc:
            logger.exception("graph_execution_failed")
            raise InternalServerError("Analysis failed.") from exc
        finally:
            _log_usage(usage)
        if key:
            result_cache.put(key, {"score": result["score"], "explanation": result["explanation"]})
        skill_counts = Counter(
            m.group(1) for entry in result.get("skill_results", []) if (m := _SKILL_TAG_RE.match(entry))
        )
        logger.info("skills_used", **skill_counts)
    else:
        logger.info("analysis_cache_hit")

    score: int = result["score"]
    explanation: str = result["explanation"]
    if score != -1:
        await log_analyze(request.app.db_pool, account.username, body.url, score, explanation)
    return AnalyzeResponse(score=score, explanation=explanation)


def _event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _update_events(update: dict[str, Any], final: dict[str, Any]) -> list[str]:
    # One LangGraph "updates" chunk ({node: state update}) → server-sent events; the score is collected in `final`.
    events: list[str] = []
    for values in update.values():
        if not isinstance(values, dict):
            continue
        if "page_type" in values:
            events.append(_event("page_type", {"page_type": values["page_type"]}))
        for finding in values.get("findings", []) or []:
            payload = finding.model_dump() if isinstance(finding, Finding) else dict(finding)
            events.append(_event("finding", payload))
        final |= {k: values[k] for k in ("score", "explanation") if k in values}
    return events


@router.post("/stream", response_class=StreamingResponse)
async def analyze_page_stream(
    body: AnalyzeRequest,
    request: Request,
    account: Account = Depends(verify_credentials),
) -> StreamingResponse:
    """
    Analyse a page like ``POST /v1/analyze``, streaming the findings as server-sent events while skills finish.

    Events: ``page_type`` once classified, one ``finding`` per factual observation as each skill completes, then a
    final ``result`` (``score``, ``explanation``) or ``error``. A client (the browser extension) can show progress
    instead of a spinner.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param account: The authenticated account (injected by verify_credentials).
    :return: A ``text/event-stream`` response.
    """
    graph, state = await _prepare(body, request, account)

    async def events() -> AsyncIterator[str]:
        usage = UsageMetadataCallbackHandler()
        final: dict[str, Any] = {}
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
        if final["score"] != -1:
            await log_analyze(request.app.db_pool, account.username, body.url, final["score"], final["explanation"])
        yield _event("result", {"score": final["score"], "explanation": final["explanation"]})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})
