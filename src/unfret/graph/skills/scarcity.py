"""
Low-stock claims across visits: an "only 3 left" that never changes, or keeps going back up.

The page's claim ("Only 3 left in stock", ``PageFacts.stock_left``) is compared with the claims earlier checks of the
same product page recorded (:mod:`unfret.scarcity_store`), then recorded itself. Only on a shop's own pages: on a
marketplace the stock line is the platform's, from real inventory.
"""

from unfret import scarcity_store
from unfret.config import settings
from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState


class ScarcitySkill(Skill):
    """Whether a shop's "only N left" claim behaves like real stock over time."""

    name = "scarcity"
    description = "Compares a shop's \"only N left\" claim with the same page's claims on earlier days."
    page_types = STOREFRONTS
    features = ("claim", "static", "resets")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Compare and record the page's stock claim.

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        features: dict[str, float | bool] = {"claim": facts.stock_left is not None}
        observations: list[Observation] = []
        found = await scarcity_store.observe(state.get("url", ""), facts) if settings.scarcity_observations else None
        if found is not None and found.pattern == "static":
            features["static"] = True
            observations.append(
                Observation(
                    type="scarcity_static",
                    evidence=f'"Only {found.count} left" on {found.days} days over {found.span_days} days: the '
                    "count never changed.",
                )
            )
        elif found is not None:
            features["resets"] = True
            observations.append(
                Observation(
                    type="scarcity_resets",
                    evidence=f'"Only {found.count} left" today; over {found.span_days} days the count went back up '
                    f"{found.resets} times.",
                )
            )
        return self.output(features, observations)
