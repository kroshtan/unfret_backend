import asyncio
from collections.abc import Awaitable, Callable

import structlog
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from veridex.config import settings
from veridex.graph.nodes import classify_node, early_exit_node, judge_node, preprocess_node
from veridex.graph.skills import Skill
from veridex.graph.skills.claims_verification import ClaimsVerificationSkill
from veridex.graph.skills.domain_age import DomainAgeSkill
from veridex.graph.skills.exif_check import ExifCheckSkill
from veridex.graph.skills.html_source_signals import HtmlSourceSignalsSkill
from veridex.graph.skills.page_content import PageContentSkill
from veridex.graph.skills.reverse_image_search import ReverseImageSearchSkill
from veridex.graph.skills.review_integrity import ReviewIntegritySkill
from veridex.graph.state import AnalysisState

logger = structlog.get_logger("veridex")

# ── Skill registry ────────────────────────────────────────────────────────────
# Add new Skill instances here to make them available to the graph.
SKILLS: list[Skill] = [
    PageContentSkill(),
    ReverseImageSearchSkill(),
    ExifCheckSkill(),
    HtmlSourceSignalsSkill(),
    DomainAgeSkill(),
    ReviewIntegritySkill(),
    ClaimsVerificationSkill(),
]


def _isolated(skill: Skill) -> Callable[..., Awaitable[dict[str, list[str]]]]:
    """
    Wrap a skill so that a failure or timeout degrades to an "unavailable" finding.

    One flaky skill (an upstream API error, a slow WHOIS server) should cost the judge one piece of evidence,
    not fail the whole analysis.

    :param skill: The skill to wrap.
    :return: An async graph node with the same input and output as ``skill.run``.
    """

    async def run(state: AnalysisState) -> dict[str, list[str]]:
        try:
            async with asyncio.timeout(settings.skill_timeout_s):
                return await skill.run(state)
        except TimeoutError:
            logger.warning("skill_timed_out", skill=skill.name, timeout_s=settings.skill_timeout_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_failed", skill=skill.name, error=type(exc).__name__)
        return {"skill_results": [f"[{skill.name}]\nSkill unavailable."]}

    return run


def build_graph(skills: list[Skill] = SKILLS) -> CompiledStateGraph:
    """
    Compile the analysis graph.

    The graph flows as follows::

        preprocess → classify ─┬→ [skill_A, skill_B, …] (parallel) → judge → END
                               └→ early_exit → END

    ``classify`` determines the page type. Marketplace listings and storefronts
    fan out to the skills; articles and non-product pages exit early with a
    score of ``-1``. Skills with ``always_run=True`` fire on every analysed
    page; others only when their ``name`` appears in ``state["flags"]``.

    To extend: add a :class:`~veridex.graph.skills.Skill` subclass and register
    an instance in :data:`SKILLS`.

    :param skills: List of skill instances to wire into the graph.
    :return: A compiled LangGraph ``StateGraph``.
    """
    builder = StateGraph(AnalysisState)

    # Nodes
    builder.add_node("preprocess", preprocess_node)
    builder.add_node("classify", classify_node)
    builder.add_node("early_exit", early_exit_node)
    for skill in skills:
        builder.add_node(skill.name, _isolated(skill))
    builder.add_node("judge", judge_node)

    # Edges
    builder.set_entry_point("preprocess")
    builder.add_edge("preprocess", "classify")

    def _route_after_classify(state: AnalysisState) -> list[Send] | str:
        if state["page_type"] not in {"aggregate_listing", "storefront"}:
            return "early_exit"
        active = [s for s in skills if s.always_run or s.name in state["flags"]]
        return [Send(s.name, state) for s in active] or "judge"

    builder.add_conditional_edges("classify", _route_after_classify)
    builder.add_edge("early_exit", END)
    for skill in skills:
        builder.add_edge(skill.name, "judge")
    builder.add_edge("judge", END)

    return builder.compile()
