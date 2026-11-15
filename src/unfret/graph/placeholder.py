"""
The interim judge of peer-to-peer ads (service providers have their own: :mod:`unfret.graph.services`).

No scorer is trained for these classes yet, so this judge never gives an overall "looks fine": the band is
"concerns" ("a few things to check"), or "careful" when a skill found something serious (a high-severity reason
such as text addressed to an AI). The reasons are the shopper verdict's own (:func:`unfret.shopper.build`), from
the skills that ran; the headline says plainly that checks for this kind of page are new. The class-specific
judges replace it.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from unfret import shopper
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph.state import AnalysisState

# Scores inside the two bands the interim judge uses (the band thresholds are settings).
_CONCERNS_SCORE = 60
_CAREFUL_SCORE = 40


def _scores() -> tuple[int, int]:
    concerns = min(max(_CONCERNS_SCORE, settings.verdict_concerns_min), settings.verdict_reliable_min - 1)
    careful = min(max(_CAREFUL_SCORE, settings.verdict_careful_min), settings.verdict_concerns_min - 1)
    return concerns, careful


def interim_verdict(state: AnalysisState) -> dict[str, Any]:
    """
    A cautious verdict from the findings so far.

    :param state: The graph state after the class's skills; uses ``page_type``, ``features``, ``findings`` and
        ``facts``.
    :return: ``score``, ``explanation`` and ``verdict`` (no ``assessment``: there is no scorer).
    """
    page_type = state.get("page_type", "p2p_listing")
    concerns, careful = _scores()

    def build(score: int) -> shopper.Verdict:
        return shopper.build(
            score,
            features=state.get("features", {}),
            findings=state.get("findings", []),
            facts=state.get("facts") or PageFacts(),
            page_type=page_type,
            headline="-",
        )

    verdict = build(concerns)
    reasons = [r for r in verdict.reasons if r.type != "overall"]  # no "several small signals" without a scorer
    score = careful if any(r.severity == "high" for r in reasons) else concerns
    if score != concerns:
        verdict = build(score)
        reasons = [r for r in verdict.reasons if r.type != "overall"]
    headlines: dict[str, dict[str, str]] = shopper.catalogue()["class_headlines"]
    headline = headlines.get(page_type, headlines["p2p_listing"])[verdict.band]
    verdict = verdict.model_copy(update={"reasons": reasons, "headline": headline})
    return {"score": score, "explanation": headline, "verdict": verdict.model_dump()}


def make_interim_judge() -> Callable[[AnalysisState], Awaitable[dict[str, Any]]]:
    """
    The interim judge as a graph node.

    :return: The node.
    """

    async def judge(state: AnalysisState) -> dict[str, Any]:
        """
        Judge a page of a class without a trained scorer (see :func:`interim_verdict`).

        :param state: Current graph state.
        :return: ``score``, ``explanation`` and ``verdict``.
        """
        return interim_verdict(state)

    return judge
