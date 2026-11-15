"""
The same review texts on other shops: reviews copied in from elsewhere.

Looks the page's distinctive reviews up in Unfret's review fingerprint store (:mod:`unfret.review_store`: hashes of
normalised review text per shop, nothing else) and records them on the way. Brands syndicate their reviews to the
shops that sell them, so the shopper only hears about it for an unknown brand with other signs of a resold supplier
product (:mod:`unfret.shopper`).
"""

from unfret import review_store
from unfret.config import settings
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState


class ReviewCopiesSkill(Skill):
    """Reviews whose exact text other shops show too."""

    name = "review_copies"
    description = "Finds reviews whose text also appears on other shops."
    features = ("checked", "copied", "copied_share", "other_shops")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look the page's reviews up in the fingerprint store and record them.

        :param state: Current graph state; uses ``url`` and ``facts``.
        :return: The skill's features and findings.
        """
        if not settings.fingerprints:
            return self.output({}, note="Review fingerprints are off.")
        copies = await review_store.observe(state.get("url", ""), state["facts"])
        if copies is None:
            return self.output({}, note="No reviews long enough to compare.")
        observations: list[Observation] = []
        if copies.copied:
            observations.append(
                Observation(
                    type="reviews_on_other_shops",
                    evidence=f"{copies.copied} of {copies.checked} reviews appear word for word on "
                    f"{copies.other_shops} other shop{'s' if copies.other_shops != 1 else ''}.",
                )
            )
        return self.output(
            {
                "checked": copies.checked,
                "copied": copies.copied,
                "copied_share": round(copies.share, 4),
                "other_shops": copies.other_shops,
            },
            observations,
        )
