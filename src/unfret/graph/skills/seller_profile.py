"""
The seller's account on a peer-to-peer platform, as the ad page shows it: age, ratings, verification, activity.

Read from :attr:`PageFacts.seller_profile` (the platform extractors, :mod:`unfret.facts.p2p`), never the seller's
name. An account a few weeks old with no ratings, or one that put up dozens of ads in its first weeks, is how most
non-delivery ads look; an account of several years with many good ratings is hard to fake. Nothing shown means
nothing found: a page without a seller box gets no findings at all (Craigslist has no profiles).
"""

import re
from datetime import UTC, date, datetime

from unfret.graph.skills import P2P_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

NEW_ACCOUNT_DAYS = 90
ESTABLISHED_DAYS = 730
WELL_RATED_COUNT = 10
_SLOW_RE = re.compile(r"(?<![a-z])(?:days?|dagen|dag|weeks?|weken|tagen?|jours?)(?![a-z])", re.IGNORECASE)
_VERIFIED_PREFIX = "Verified"


def today() -> date:
    """
    Today in UTC (a function, so tests can fix it).

    :return: The date.
    """
    return datetime.now(tz=UTC).date()


def _burst(listings: int | None, days: int | None) -> bool:
    # Many ads on a young account: 15 or more in its first three months, or 8 in its first month.
    if listings is None or days is None:
        return False
    return (days < NEW_ACCOUNT_DAYS and listings >= 15) or (days < 30 and listings >= 8)


class SellerProfileSkill(Skill):
    """The seller's account: how old, how rated, verified or not, how many ads, how responsive."""

    name = "seller_profile"
    description = "Reads the seller's account on the ad: member since, ratings, verification, other ads, replies."
    page_types = P2P_PAGES
    features = (
        "shown",
        "account_days",
        "new_account",
        "established",
        "ratings",
        "no_ratings",
        "low_rating",
        "well_rated",
        "verified",
        "listing_burst",
        "slow_responses",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Turn the seller profile into features and findings.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        profile = facts.seller_profile
        if profile is None:
            return self.output({"shown": False}, note="The page shows no seller profile.")
        days = (today() - profile.member_since).days if profile.member_since else None
        ratings = profile.rating_count
        share = facts.seller_rating  # eBay's positive share, in percent
        low = (ratings or 0) >= 3 and (
            (profile.rating is not None and profile.rating < 3.5)
            or (share is not None and (ratings or 0) >= 10 and share < 95)
        )
        well_rated = (
            (ratings or 0) >= WELL_RATED_COUNT
            and not low
            and ((profile.rating is not None and profile.rating >= 4.5) or (share is not None and share >= 98))
        )
        verified = any(b.startswith(_VERIFIED_PREFIX) for b in profile.badges)
        burst = _burst(profile.other_listings, days)
        slow = (profile.response_rate is not None and profile.response_rate < 50) or bool(
            _SLOW_RE.search(profile.response_time)
        )
        observations: list[Observation] = []
        if days is not None and days < NEW_ACCOUNT_DAYS and profile.member_since:
            observations.append(
                Observation(
                    type="new_account",
                    evidence=f"The seller's account is {max(days, 0)} days old (member since "
                    f"{profile.member_since.isoformat()}).",
                )
            )
        if ratings == 0:
            observations.append(Observation(type="no_ratings", evidence="The seller has no ratings yet."))
        if low:
            shown = f"{profile.rating:g} out of 5" if profile.rating is not None else f"{share:g}% positive"
            observations.append(
                Observation(type="low_rating", evidence=f"The seller is rated {shown} from {ratings} ratings.")
            )
        if burst:
            observations.append(
                Observation(
                    type="listing_burst",
                    evidence=f"{profile.other_listings} other ads on an account that is {days} days old.",
                )
            )
        if slow:
            said = profile.response_time or f"{profile.response_rate:g}% of messages answered"
            observations.append(Observation(type="slow_responses", evidence=f"The platform says: {said}."))
        if days is not None and days < NEW_ACCOUNT_DAYS and not verified and not profile.badges:
            observations.append(
                Observation(type="unverified", evidence="The new account shows no verification (ID, phone, bank).")
            )
        return self.output(
            {
                "shown": True,
                "account_days": min(days, 3650) if days is not None else None,
                "new_account": days is not None and days < NEW_ACCOUNT_DAYS,
                "established": days is not None and days >= ESTABLISHED_DAYS,
                "ratings": ratings,
                "no_ratings": ratings == 0,
                "low_rating": low,
                "well_rated": well_rated,
                "verified": verified,
                "listing_burst": burst,
                "slow_responses": slow,
            },
            observations,
            note="" if observations else "Nothing notable about the seller's account.",
        )
