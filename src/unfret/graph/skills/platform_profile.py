"""
A service provider's profile on a platform (Werkspot, Thumbtack, Angi, …): their track record as the platform shows.

How long they have been on the platform, how many jobs and reviews they have, their rating, the platform's badges
(background checked, licence verified, KvK verified) and how often they answer. A new profile with few reviews is
not a warning on its own (everyone starts somewhere): it only means the platform can't vouch for much yet.
"""

from datetime import date

from unfret.graph.skills import SERVICE_LISTINGS, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

NEW_DAYS = 180
FEW_REVIEWS = 5
LOW_RESPONSE_RATE = 50.0
_VERIFIED_WORDS = ("verified", "geverifieerd", "background", "certified", "screened", "erkend")


def today() -> date:
    """
    Today (a function, so tests can fix it).

    :return: The date.
    """
    return date.today()


class PlatformProfileSkill(Skill):
    """Member since, jobs, reviews, rating, badges and response rate of a provider's platform profile."""

    name = "platform_profile"
    description = "Reads a service provider's track record from their platform profile."
    page_types = SERVICE_LISTINGS
    features = (
        "known",
        "member_years",
        "new_member",
        "jobs_completed",
        "rating",
        "rating_count",
        "few_reviews",
        "low_response_rate",
        "verified_badges",
        "years_in_business",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Read the profile the platform extractor found.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        profile = state["facts"].seller_profile
        if profile is None:
            return self.output({"known": False}, note="No profile details found on the page.")
        features: dict[str, float | bool | None] = {"known": True}
        observations: list[Observation] = []
        if profile.member_since is not None:
            days = max((today() - profile.member_since).days, 0)
            features |= {"member_years": round(days / 365, 2), "new_member": days < NEW_DAYS}
            observations.append(
                Observation(type="member_since", evidence=f"On the platform since {profile.member_since:%B %Y}.")
            )
        if profile.jobs_completed is not None:
            features["jobs_completed"] = profile.jobs_completed
            observations.append(Observation(type="jobs", evidence=f"{profile.jobs_completed:,} jobs on the platform."))
        if profile.rating_count is not None:
            features |= {"rating_count": profile.rating_count, "few_reviews": profile.rating_count < FEW_REVIEWS}
        if profile.rating is not None:
            features["rating"] = profile.rating
            count = f" from {profile.rating_count:,} reviews" if profile.rating_count is not None else ""
            observations.append(Observation(type="rating", evidence=f"Rated {profile.rating:g} of 5{count}."))
        if profile.response_rate is not None:
            features["low_response_rate"] = profile.response_rate < LOW_RESPONSE_RATE
            observations.append(
                Observation(type="response_rate", evidence=f"Answers {profile.response_rate:g}% of requests.")
            )
        verified = [b for b in profile.badges if any(w in b.lower() for w in _VERIFIED_WORDS)]
        features["verified_badges"] = len(verified)
        if profile.badges:
            observations.append(Observation(type="badges", evidence=f"Badges: {', '.join(profile.badges[:5])}."))
        if profile.years_in_business is not None:
            features["years_in_business"] = profile.years_in_business
        return self.output(features, observations)
