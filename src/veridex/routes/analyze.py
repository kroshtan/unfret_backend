import asyncio
import re
from collections import Counter

import structlog
from fastapi import APIRouter, Depends, Request
from langchain_core.callbacks import UsageMetadataCallbackHandler

from veridex.auth import Account, verify_credentials
from veridex.config import settings
from veridex.db import count_analyses_today, log_analyze
from veridex.schemas.errors import GatewayTimeoutError, InternalServerError, TooManyRequestsError
from veridex.schemas.requests import AnalyzeRequest
from veridex.schemas.responses import AnalyzeResponse

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/analyze", tags=["Analyze"])

_SKILL_TAG_RE = re.compile(r"\[([^\]]+)\]")


@router.post("", response_model=AnalyzeResponse)
async def analyze_page(
    body: AnalyzeRequest,
    request: Request,
    account: Account = Depends(verify_credentials),
) -> AnalyzeResponse:
    """
    Analyse a product web page for legitimacy using the LangGraph analysis pipeline.

    Passes the raw HTML and active flags through the graph, which cleans the content,
    runs all active skills in parallel, then synthesises a reliability score and explanation.
    Results with a score other than -1 (not analysed) are persisted to the audit log and
    count towards the daily limit.

    :param body: The request body containing the page content and flags.
    :param request: The FastAPI request (used to access the compiled graph).
    :param account: The authenticated account (injected by verify_credentials).
    :return: Reliability score (0–100) and one-line explanation.
    :raises InternalServerError: If the graph is not initialised or fails.
    :raises GatewayTimeoutError: If the analysis takes longer than ``analysis_timeout_s``.
    :raises TooManyRequestsError: If the account's daily limit is reached.
    """
    graph = request.app.graph
    if graph is None:
        raise InternalServerError("Analysis graph is not initialised.")

    pool = request.app.db_pool
    limit = account.daily_limit
    if limit is not None and await count_analyses_today(pool, account.username) >= limit:
        raise TooManyRequestsError(f"Daily limit of {limit} analyses reached.")

    state: dict[str, object] = {"page_content": body.page_content, "url": body.url, "flags": body.flags}
    if body.page_facts is not None:
        state["page_facts"] = body.page_facts
    if body.checkout_content:
        state["checkout_content"] = body.checkout_content
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
        # Token counts per model, for pricing. Deliberately nothing that identifies the page or the caller.
        for model, tokens in usage.usage_metadata.items():
            logger.info(
                "analysis_llm_usage",
                model=model,
                input_tokens=tokens.get("input_tokens", 0),
                output_tokens=tokens.get("output_tokens", 0),
            )

    skill_counts = Counter(
        m.group(1) for entry in result.get("skill_results", []) if (m := _SKILL_TAG_RE.match(entry))
    )
    logger.info("skills_used", **skill_counts)

    score: int = result["score"]
    explanation: str = result["explanation"]

    if score != -1:
        await log_analyze(pool, account.username, body.url, score, explanation)

    return AnalyzeResponse(score=score, explanation=explanation)
