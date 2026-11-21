import asyncio
import weakref
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import httpx
import openai
import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy, Send

from unfret import recording, scoring, shop_store
from unfret.config import settings
from unfret.graph import p2p, services, triage
from unfret.graph.nodes import (
    classify_by_rules_node,
    classify_node,
    early_exit_node,
    make_judge_node,
    preprocess_node,
)
from unfret.graph.skills import Skill, SkillOutput
from unfret.graph.skills.brand_presence import BrandPresenceSkill
from unfret.graph.skills.claim_audit import ClaimAuditSkill
from unfret.graph.skills.claims import ClaimsSkill
from unfret.graph.skills.content import ContentSkill
from unfret.graph.skills.dark_patterns import DarkPatternsSkill
from unfret.graph.skills.domain_age import DomainAgeSkill
from unfret.graph.skills.doorway_pages import DoorwayPagesSkill
from unfret.graph.skills.duplicate_ads import DuplicateAdsSkill
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.graph.skills.html_source_signals import HtmlSourceSignalsSkill
from unfret.graph.skills.injection import InjectionSkill
from unfret.graph.skills.listing import ListingSkill
from unfret.graph.skills.market_price import MarketPriceSkill
from unfret.graph.skills.off_platform import OffPlatformSkill
from unfret.graph.skills.payment_methods import PaymentMethodsSkill
from unfret.graph.skills.photo_claims import PhotoClaimsSkill
from unfret.graph.skills.platform_profile import PlatformProfileSkill
from unfret.graph.skills.price_outlier import PriceOutlierSkill
from unfret.graph.skills.pricing import PricingSkill
from unfret.graph.skills.reverse_image import ReverseImageSkill
from unfret.graph.skills.review_copies import ReviewCopiesSkill
from unfret.graph.skills.reviews import ReviewsSkill
from unfret.graph.skills.safety_gate import SafetyGateSkill
from unfret.graph.skills.seller_profile import SellerProfileSkill
from unfret.graph.skills.service_identity import ServiceIdentitySkill
from unfret.graph.skills.service_terms import ServiceTermsSkill
from unfret.graph.skills.shop_links import ShopLinksSkill
from unfret.graph.skills.shop_policies import ShopPoliciesSkill
from unfret.graph.skills.testimonials import TestimonialsSkill
from unfret.graph.skills.us_identity import UsIdentitySkill
from unfret.graph.skills.variant_check import VariantCheckSkill
from unfret.graph.state import P2P_TYPES, PRODUCT_TYPES, SERVICE_TYPES, AnalysisState, PageType

logger = structlog.get_logger("unfret")

# ── Skill registry ────────────────────────────────────────────────────────────
# Add new Skill instances here to make them available to the graph.
SKILLS: list[Skill] = [
    ContentSkill(),
    ListingSkill(),
    PricingSkill(),
    ReviewsSkill(),
    HtmlSourceSignalsSkill(),
    DarkPatternsSkill(),
    InjectionSkill(),
    EuRegistrySkill(),
    UsIdentitySkill(),
    ShopPoliciesSkill(),
    DomainAgeSkill(),
    ShopLinksSkill(),
    # Product-page upgrades (2026-11): new features the shipped scorer doesn't use until it is retrained; their
    # signals reach the shopper as reasons and positives (unfret.shopper).
    PaymentMethodsSkill(),
    ClaimsSkill(),
    # Every checkable claim on the listing, checked by a stronger model (deep checks only).
    ClaimAuditSkill(),
    MarketPriceSkill(),
    ReviewCopiesSkill(),
    BrandPresenceSkill(),
    SafetyGateSkill(),
    # The listing's piece count and size against its own photos (a vision model; deep checks only).
    PhotoClaimsSkill(),
    # Different products grouped as one listing's sizes, and a price shown for one of them (deep checks only).
    VariantCheckSkill(),
    # Service providers.
    ServiceIdentitySkill(),
    DoorwayPagesSkill(),
    ServiceTermsSkill(),
    TestimonialsSkill(),
    PlatformProfileSkill(),
    ReverseImageSkill(),
    # Peer-to-peer ads
    SellerProfileSkill(),
    OffPlatformSkill(),
    PriceOutlierSkill(),
    DuplicateAdsSkill(),
]

# The page classes, each analysed by its own subgraph: (node name, page types it handles).
PAGE_CLASSES: dict[str, frozenset[PageType]] = {
    "product": PRODUCT_TYPES,
    "p2p": P2P_TYPES,
    "service": SERVICE_TYPES,
}


def transient(exc: BaseException) -> bool:
    """
    Whether an error is worth another attempt: a dropped connection, a provider's 5xx, a rate limit.

    A timeout of the whole skill is not: another attempt would take as long again.

    :param exc: The error.
    :return: ``True`` when the graph's RetryPolicy should run the node again.
    """
    if isinstance(exc, openai.APIConnectionError | openai.RateLimitError | openai.InternalServerError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError | ConnectionError)


def retry_policy() -> RetryPolicy:
    """
    LangGraph's retry policy for the nodes that call the LLM or fetch outside data.

    :return: Up to ``settings.graph_retry_attempts`` attempts on :func:`transient` errors, with jittered backoff.
    """
    return RetryPolicy(max_attempts=settings.graph_retry_attempts, initial_interval=0.5, retry_on=transient)


def _attempt(runtime: Runtime[Any] | None) -> int:
    info = runtime.execution_info if runtime is not None else None
    return info.node_attempt if info is not None else settings.graph_retry_attempts


def _retrying(exc: Exception, runtime: Runtime[Any] | None) -> bool:
    # Raise again (so the RetryPolicy runs the node once more) while attempts are left; degrade after the last one.
    return transient(exc) and _attempt(runtime) < settings.graph_retry_attempts


def _isolated(skill: Skill) -> Callable[..., Awaitable[SkillOutput]]:
    """
    Wrap a skill so that a failure or timeout degrades to an "unavailable" finding.

    One flaky skill (an upstream API error, a slow registry) should cost the analysis one piece of evidence,
    not fail the whole analysis. A skill marked ``retry`` gets another attempt first when the error is transient:
    the error is raised again for the node's RetryPolicy while attempts are left.

    :param skill: The skill to wrap.
    :return: An async graph node with the same input and output as ``skill.run``.
    """

    async def run(state: AnalysisState, runtime: Runtime[Any]) -> SkillOutput:
        try:
            async with asyncio.timeout(settings.skill_timeout_s):
                return await skill.run(state)
        except TimeoutError:
            logger.warning("skill_timed_out", skill=skill.name, timeout_s=settings.skill_timeout_s)
        except Exception as exc:
            if skill.retry and _retrying(exc, runtime):
                logger.info("skill_retrying", skill=skill.name, error=type(exc).__name__)
                raise
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

    async def triage_node(state: AnalysisState, runtime: Runtime[Any]) -> dict[str, object]:
        """
        Decide which expensive skills to run next (see :mod:`unfret.graph.triage`).

        :param state: The graph state after a round of skills.
        :param runtime: The LangGraph runtime (the attempt number, for the retry policy).
        :return: The next round number, the planned skills, and their record in ``expensive_runs``.
        :raises Exception: A transient error of the triage's LLM call, while attempts are left.
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
            except Exception as exc:  # a failed triage only means no expensive checks
                if _retrying(exc, runtime):
                    raise
                logger.warning("triage_failed", error=type(exc).__name__)
                decided = triage.TriagePlan((), "Triage failed.")
            run = decided.run
            logger.info("triage", round=round_, run=run, reason=decided.reason)
        return {"triage_round": round_ + 1, "next_skills": list(run), "expensive_runs": list(run)}

    return triage_node


def _service_triage_node(skills: list[Skill]) -> Callable[..., Awaitable[dict[str, object]]]:
    expensive = {s.name for s in skills if s.tier == "expensive"}

    async def triage_node(state: AnalysisState) -> dict[str, object]:
        """
        The service class's triage: rules only, no LLM.

        An expensive skill the caller asked for runs; otherwise the photo search runs once when the cheap skills
        found something that copied photos would confirm (:func:`unfret.graph.services.suspicion`).

        :param state: The graph state after the cheap skills.
        :return: The next round number, the planned skills, and their record in ``expensive_runs``.
        """
        round_ = state.get("triage_round", 0)
        done = state.get("expensive_runs", [])
        flagged = [n for n in state["flags"] if n in expensive and n not in done]
        run: tuple[str, ...] = ()
        if round_ == 0 and len(done) < settings.triage_max_expensive_runs:
            if flagged:
                run = (flagged[0],)
            elif "reverse_image" in expensive and state["facts"].image_urls and services.suspicion(state["features"]):
                run = ("reverse_image",)
        logger.info("triage", round=round_, run=run, reason="service rules")
        return {"triage_round": round_ + 1, "next_skills": list(run), "expensive_runs": list(run)}

    return triage_node


def _recording(
    judge: Callable[[AnalysisState], Awaitable[dict[str, Any]]], *, page: bool = False
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """
    Wrap the judge so that, once it has answered, the shop is recorded in the fingerprint store in the background.

    :param judge: The judge node.
    :param page: Also record the page's photo, price and reviews in the background (:mod:`unfret.recording`): the
        quick tier, whose skills don't.
    :return: The wrapped node.
    """

    async def node(state: AnalysisState) -> dict[str, Any]:
        update = await judge(state)
        shop_store.observe_later(dict(state))
        if page:
            recording.record_later(dict(state))
        return update

    return node


def _carded(
    judge: Callable[[AnalysisState], Awaitable[dict[str, Any]]], available: list[Skill]
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """
    Wrap a class's judge so that its verdict carries the result card (:func:`unfret.card.complete`).

    :param judge: The judge node.
    :param available: The skills this graph runs (the quick tier's are fewer than :data:`SKILLS`).
    :return: The wrapped node.
    """

    from unfret import card  # noqa: PLC0415 - card reads the shopper catalogue, which imports this package

    async def node(state: AnalysisState) -> dict[str, Any]:
        update = await judge(state)
        if update.get("verdict"):
            # With the judge's own update (the scorer's assessment): the card marks the areas of its small signs.
            judged = cast(AnalysisState, {**state, **update})
            update["verdict"] = card.complete(update["verdict"], judged, available=available, applicable=SKILLS)
        return update

    return node


# Triage runs after the cheap skills and once more after the first expensive round (the bounded re-triage).
MAX_TRIAGE_ROUNDS = 2


def _class_graph(
    skills: list[Skill],
    triage_node: Callable[..., Awaitable[dict[str, object]]],
    judge: Callable[..., Awaitable[dict[str, Any]]],
) -> CompiledStateGraph:
    """
    One page class's subgraph: its cheap skills in parallel, then the triage (and what it plans), then the judge.

    :param skills: The skills that handle at least one of the class's page types.
    :param triage_node: The class's triage.
    :param judge: The class's judge.
    :return: The compiled subgraph.
    """
    builder = StateGraph(AnalysisState)
    for skill in skills:
        builder.add_node(skill.name, _isolated(skill), retry_policy=retry_policy() if skill.retry else None)
    builder.add_node("triage", triage_node, retry_policy=retry_policy())
    builder.add_node("judge", judge, retry_policy=retry_policy())

    def _fan_out(state: AnalysisState) -> list[Send] | str:
        return [Send(s.name, state) for s in active_skills(skills, state)] or "triage"

    def _route_after_triage(state: AnalysisState) -> list[Send] | str:
        return [Send(name, state) for name in state.get("next_skills", [])] or "judge"

    builder.add_conditional_edges(START, _fan_out)
    builder.add_conditional_edges("triage", _route_after_triage)
    for skill in skills:
        builder.add_edge(skill.name, "triage")
    builder.add_edge("judge", END)
    return builder.compile()


# Each compiled graph's class subgraphs (they run inside function nodes, so LangGraph doesn't list them).
_CLASS_GRAPHS: weakref.WeakKeyDictionary[CompiledStateGraph, dict[str, CompiledStateGraph]] = (
    weakref.WeakKeyDictionary()
)


def node_names(graph: CompiledStateGraph) -> set[str]:
    """
    Every node of a graph built by :func:`build_graph`, its class subgraphs' nodes (the skills) included.

    :param graph: The compiled graph.
    :return: The node names.
    """
    names = set(graph.nodes)
    for subgraph in _CLASS_GRAPHS.get(graph, {}).values():
        names |= set(subgraph.nodes)
    return names


# Accumulated state the subgraph hands back: only what it added (the parent's reducers append it).
_APPENDED = ("skill_results", "findings", "expensive_runs")
_REPLACED = ("features", "triage_round", "next_skills", "score", "explanation", "verdict", "assessment")


def _subgraph_node(subgraph: CompiledStateGraph) -> Callable[..., Awaitable[dict[str, Any]]]:
    """
    Run a class's subgraph as one node of the main graph.

    Its steps still stream (``astream(..., subgraphs=True)``), and the update it returns holds only what the
    subgraph added, so the main graph's list reducers don't repeat the classification.

    :param subgraph: The compiled subgraph.
    :return: The node.
    """

    async def node(state: AnalysisState, config: RunnableConfig) -> dict[str, Any]:
        out = await subgraph.ainvoke(state, config)
        before: dict[str, Any] = dict(state)
        update: dict[str, Any] = {k: out[k][len(before.get(k) or []) :] for k in _APPENDED if k in out}
        update |= {k: out[k] for k in _REPLACED if k in out}
        return update

    return node


def build_graph(
    skills: list[Skill] = SKILLS, model_path: Path = scoring.MODEL_PATH, *, llm: bool = True, record_page: bool = False
) -> CompiledStateGraph:
    """
    Compile the analysis graph.

    The graph flows as follows::

        preprocess → classify ─┬→ product: [cheap skills] (parallel) → triage ─┬→ [expensive skill] → triage
                               │                                                └→ judge → END
                               ├→ p2p:     [cheap skills] → triage (photo check) ─┬→ [reverse_image] → triage
                               │                                                    └→ p2p judge → END
                               ├→ service: [cheap skills] → triage (rules) ─┬→ [reverse_image] → triage
                               │                                             └→ service judge → END
                               └→ early_exit → END

    ``preprocess`` and ``classify`` are shared; each page class then has its own subgraph. Marketplace listings
    and storefronts go to the product subgraph: the cheap skills that handle that page type, then the triage, which
    decides, rules first, whether an expensive skill (a reverse image search) is worth it, within a per-analysis
    budget, with one re-triage after it, then the scorer's judge. Peer-to-peer ads and service providers (only
    while ``settings.expansion_classes`` is on) go to their own subgraphs: ads get the photo check whenever they
    have a photo and hand-written verdict rules (:mod:`unfret.graph.p2p`, :mod:`unfret.shopper_p2p`); the service
    providers' triage runs the photo search when their cheap skills found something it would confirm, and their
    judge's hand-written rules give the band (:mod:`unfret.graph.services`), after which a service company's site is
    recorded in the fingerprint store like a shop. Articles and non-product pages exit early with a score of ``-1``.

    Nodes that call the LLM or fetch outside data have LangGraph's RetryPolicy (:func:`retry_policy`). The graph
    is stream-capable: ``graph.astream(..., stream_mode="updates", subgraphs=True)`` yields each skill's findings
    as it finishes.

    To extend: add a :class:`~unfret.graph.skills.Skill` subclass and register an instance in :data:`SKILLS`; its
    ``page_types`` decide which subgraphs it joins.

    :param skills: List of skill instances to wire into the graph.
    :param model_path: The scorer artifact the judge uses (the quick tier has its own).
    :param llm: Whether the graph's own nodes may call the LLM (classification, headline); the quick tier's may not.
        Which skills run is the caller's choice.
    :param record_page: Whether a product page's photo, price and reviews are recorded in the background once the
        judge has answered (:mod:`unfret.recording`): the quick tier, whose skills look nothing up and record nothing.
        The answer never waits for it.
    :return: A compiled LangGraph ``StateGraph``.
    """
    products = [s for s in skills if s.page_types & PRODUCT_TYPES]
    product = _class_graph(
        products,
        _triage_node(skills),
        _recording(_carded(make_judge_node(model_path, llm=llm), products), page=record_page),
    )
    judges = {"product": product}
    ads = [s for s in skills if s.page_types & PAGE_CLASSES["p2p"]]
    judges["p2p"] = _class_graph(ads, p2p.make_triage(ads), _carded(p2p.make_judge(), ads))
    handled = [s for s in skills if s.page_types & PAGE_CLASSES["service"]]
    judges["service"] = _class_graph(
        handled, _service_triage_node(handled), _recording(_carded(services.make_service_judge(), handled))
    )

    builder = StateGraph(AnalysisState)
    builder.add_node("preprocess", preprocess_node)
    builder.add_node("classify", classify_node if llm else classify_by_rules_node, retry_policy=retry_policy())
    builder.add_node("early_exit", early_exit_node)
    for name, subgraph in judges.items():
        builder.add_node(name, _subgraph_node(subgraph))

    builder.add_edge(START, "preprocess")
    builder.add_edge("preprocess", "classify")

    def _route_after_classify(state: AnalysisState) -> str:
        page_type = state["page_type"]
        return next((name for name, types in PAGE_CLASSES.items() if page_type in types), "early_exit")

    builder.add_conditional_edges("classify", _route_after_classify, [*PAGE_CLASSES, "early_exit"])
    builder.add_edge("early_exit", END)
    for name in PAGE_CLASSES:
        builder.add_edge(name, END)
    graph = builder.compile()
    _CLASS_GRAPHS[graph] = judges
    return graph
