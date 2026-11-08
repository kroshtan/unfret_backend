import asyncio
from collections.abc import Awaitable, Callable

import structlog
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from veridex.config import settings
from veridex.graph.nodes import classify_node, early_exit_node, judge_node, preprocess_node
from veridex.graph.skills import Skill, SkillOutput
from veridex.graph.skills.content import ContentSkill
from veridex.graph.skills.dark_patterns import DarkPatternsSkill
from veridex.graph.skills.domain_age import DomainAgeSkill
from veridex.graph.skills.eu_registry import EuRegistrySkill
from veridex.graph.skills.html_source_signals import HtmlSourceSignalsSkill
from veridex.graph.skills.injection import InjectionSkill
from veridex.graph.skills.listing import ListingSkill
from veridex.graph.skills.reverse_image import ReverseImageSkill
from veridex.graph.skills.reviews import ReviewsSkill
from veridex.graph.state import AnalysisState

logger = structlog.get_logger("veridex")

# ── Skill registry ────────────────────────────────────────────────────────────
# Add new Skill instances here to make them available to the graph.
SKILLS: list[Skill] = [
    ContentSkill(),
    ListingSkill(),
    ReviewsSkill(),
    HtmlSourceSignalsSkill(),
    DarkPatternsSkill(),
    InjectionSkill(),
    EuRegistrySkill(),
    DomainAgeSkill(),
    ReverseImageSkill(),
]


def _isolated(skill: Skill) -> Callable[..., Awaitable[SkillOutput]]:
    """
    Wrap a skill so that a failure or timeout degrades to an "unavailable" finding.

    One flaky skill (an upstream API error, a slow registry) should cost the analysis one piece of evidence,
    not fail the whole analysis.

    :param skill: The skill to wrap.
    :return: An async graph node with the same input and output as ``skill.run``.
    """

    async def run(state: AnalysisState) -> SkillOutput:
        try:
            async with asyncio.timeout(settings.skill_timeout_s):
                return await skill.run(state)
        except TimeoutError:
            logger.warning("skill_timed_out", skill=skill.name, timeout_s=settings.skill_timeout_s)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_failed", skill=skill.name, error=type(exc).__name__)
        return skill.unavailable()

    return run


def active_skills(skills: list[Skill], state: AnalysisState) -> list[Skill]:
    """
    The skills that apply to this page: its type is one they handle, and they run by default or were flagged.

    :param skills: The registered skills.
    :param state: The graph state after classification.
    :return: The skills to run.
    """
    return [s for s in skills if state["page_type"] in s.page_types and (s.always_run or s.name in state["flags"])]


def build_graph(skills: list[Skill] = SKILLS) -> CompiledStateGraph:
    """
    Compile the analysis graph.

    The graph flows as follows::

        preprocess → classify ─┬→ [skill_A, skill_B, …] (parallel) → judge → END
                               └→ early_exit → END

    ``classify`` determines the page type. Marketplace listings and storefronts
    fan out to the skills that handle that page type; articles and non-product
    pages exit early with a score of ``-1``. Skills with ``always_run=True``
    fire by default; others only when their ``name`` appears in ``state["flags"]``.

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
        return [Send(s.name, state) for s in active_skills(skills, state)] or "judge"

    builder.add_conditional_edges("classify", _route_after_classify)
    builder.add_edge("early_exit", END)
    for skill in skills:
        builder.add_edge(skill.name, "judge")
    builder.add_edge("judge", END)

    return builder.compile()
