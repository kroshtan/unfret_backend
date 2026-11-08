import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from veridex import scoring
from veridex.config import settings
from veridex.graph import triage
from veridex.graph.nodes import classify_node, early_exit_node, make_judge_node, preprocess_node
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
    The cheap skills that apply to this page: its type is one they handle, and they run by default or were flagged.

    Expensive skills never start here; the triage decides on them.

    :param skills: The registered skills.
    :param state: The graph state after classification.
    :return: The skills to run in parallel.
    """
    return [
        s
        for s in skills
        if s.tier == "cheap" and state["page_type"] in s.page_types and (s.always_run or s.name in state["flags"])
    ]


def _triage_node(skills: list[Skill]) -> Callable[..., Awaitable[dict[str, object]]]:
    expensive = {s.name: s for s in skills if s.tier == "expensive"}

    async def triage_node(state: AnalysisState) -> dict[str, object]:
        """
        Decide which expensive skills to run next (see :mod:`veridex.graph.triage`).

        :param state: The graph state after a round of skills.
        :return: The next round number, the planned skills, and their record in ``expensive_runs``.
        """
        round_ = state.get("triage_round", 0)
        done = state.get("expensive_runs", [])
        applicable = {n for n, s in expensive.items() if state["page_type"] in s.page_types}
        flagged = [n for n in state["flags"] if n in applicable and n not in done]
        if round_ >= MAX_TRIAGE_ROUNDS:
            run: tuple[str, ...] = ()
        elif flagged and len(done) < settings.triage_max_expensive_runs:
            run = (flagged[0],)
            logger.info("triage", round=round_, run=run, reason="requested by the caller")
        else:
            try:
                decided = await triage.plan(state, applicable)
            except Exception as exc:  # noqa: BLE001 - a failed triage only means no expensive checks
                logger.warning("triage_failed", error=type(exc).__name__)
                decided = triage.TriagePlan((), "Triage failed.")
            run = decided.run
            logger.info("triage", round=round_, run=run, reason=decided.reason)
        return {"triage_round": round_ + 1, "next_skills": list(run), "expensive_runs": list(run)}

    return triage_node


# Triage runs after the cheap skills and once more after the first expensive round (the bounded re-triage).
MAX_TRIAGE_ROUNDS = 2


def build_graph(skills: list[Skill] = SKILLS, model_path: Path = scoring.MODEL_PATH) -> CompiledStateGraph:
    """
    Compile the analysis graph.

    The graph flows as follows::

        preprocess → classify ─┬→ [cheap skills] (parallel) → triage ─┬→ [expensive skill] → triage (once more)
                               │                                       └→ judge → END
                               └→ early_exit → END

    ``classify`` determines the page type. Marketplace listings and storefronts
    fan out to the cheap skills that handle that page type; articles and
    non-product pages exit early with a score of ``-1``. The triage then decides,
    rules first, whether an expensive skill (a paid reverse image search) is
    worth it, within a per-analysis budget, with one re-triage after it. The
    graph is stream-capable: ``graph.astream(..., stream_mode="updates")``
    yields each skill's findings as it finishes.

    To extend: add a :class:`~veridex.graph.skills.Skill` subclass and register
    an instance in :data:`SKILLS`.

    :param skills: List of skill instances to wire into the graph.
    :param model_path: The scorer artifact the judge uses (the quick tier has its own).
    :return: A compiled LangGraph ``StateGraph``.
    """
    builder = StateGraph(AnalysisState)

    # Nodes
    builder.add_node("preprocess", preprocess_node)
    builder.add_node("classify", classify_node)
    builder.add_node("early_exit", early_exit_node)
    for skill in skills:
        builder.add_node(skill.name, _isolated(skill))
    builder.add_node("triage", _triage_node(skills))
    builder.add_node("judge", make_judge_node(model_path))

    # Edges
    builder.set_entry_point("preprocess")
    builder.add_edge("preprocess", "classify")

    def _route_after_classify(state: AnalysisState) -> list[Send] | str:
        if state["page_type"] not in {"aggregate_listing", "storefront"}:
            return "early_exit"
        return [Send(s.name, state) for s in active_skills(skills, state)] or "triage"

    def _route_after_triage(state: AnalysisState) -> list[Send] | str:
        return [Send(name, state) for name in state.get("next_skills", [])] or "judge"

    builder.add_conditional_edges("classify", _route_after_classify)
    builder.add_conditional_edges("triage", _route_after_triage)
    builder.add_edge("early_exit", END)
    for skill in skills:
        builder.add_edge(skill.name, "triage")
    builder.add_edge("judge", END)

    return builder.compile()
