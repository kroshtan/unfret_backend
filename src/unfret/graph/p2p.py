"""
The peer-to-peer class's own triage and judge.

The triage runs the photo check (reverse image: the same photo in other ads or shops, very telling for rentals
and electronics) whenever the ad has a photo, within the per-analysis budget (``triage_max_expensive_runs``); a
skill the caller asked for comes first. The judge is :func:`unfret.shopper_p2p.build`'s hand-written rules.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from unfret import shopper_p2p
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph.skills import Skill
from unfret.graph.state import AnalysisState

logger = structlog.get_logger("unfret")

PHOTO_SKILL = "reverse_image"


def make_triage(skills: list[Skill]) -> Callable[[AnalysisState], Awaitable[dict[str, object]]]:
    """
    The p2p triage as a graph node.

    :param skills: The skills of the p2p subgraph.
    :return: The node.
    """
    expensive = {s.name for s in skills if s.tier == "expensive"}

    async def triage(state: AnalysisState) -> dict[str, object]:
        """
        Plan one round of expensive skills: the requested one, else the photo check when the ad has a photo.

        :param state: The graph state after the cheap skills.
        :return: The next round number, the planned skills, and their record in ``expensive_runs``.
        """
        round_ = state.get("triage_round", 0)
        done = state.get("expensive_runs", [])
        within = round_ == 0 and len(done) < settings.triage_max_expensive_runs
        flagged = [n for n in state["flags"] if n in expensive and n not in done]
        facts = state.get("facts")
        run: tuple[str, ...] = ()
        if within and flagged:
            run = (flagged[0],)
        elif within and PHOTO_SKILL in expensive and facts is not None and facts.image_urls:
            run = (PHOTO_SKILL,)
        logger.info("triage", round=round_, run=run, reason="p2p: photo check" if run else "p2p: nothing to run")
        return {"triage_round": round_ + 1, "next_skills": list(run), "expensive_runs": list(run)}

    return triage


def make_judge() -> Callable[[AnalysisState], Awaitable[dict[str, Any]]]:
    """
    The p2p judge as a graph node.

    :return: The node.
    """

    async def judge(state: AnalysisState) -> dict[str, Any]:
        """
        Judge a peer-to-peer ad with the hand-written rules (:mod:`unfret.shopper_p2p`).

        :param state: The graph state after the skills.
        :return: ``score``, ``explanation`` and ``verdict`` (no ``assessment``: there is no scorer yet).
        """
        score, verdict = shopper_p2p.build(
            features=state.get("features", {}),
            findings=state.get("findings", []),
            facts=state.get("facts") or PageFacts(),
        )
        return {"score": score, "explanation": verdict.headline, "verdict": verdict.model_dump()}

    return judge
