"""
Review intelligence: whether the review record is plausible for this listing.

Deterministic statistics (reviews per day since the listing appeared, rating shape, duplicate and very short
reviews, bursts) plus embedding similarity between each review and the product title, which exposes reviews
pooled from other products: the classic "5,000 glowing reviews on a listing that is three weeks old".
"""

import math
import re
from datetime import UTC, date, datetime, timedelta

import structlog

from unfret.facts import PageFacts, Review
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState
from unfret.llm import get_embeddings

logger = structlog.get_logger("unfret")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")
_MAX_EMBEDDED_REVIEWS = 20
_EMBED_CHARS = 600
_OFF_TOPIC_SIMILARITY = 0.35


def _normalise(text: str) -> str:
    return _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", text.lower())).strip()


def duplicate_share(texts: list[str], min_chars: int = 12) -> float:
    """
    Share of reviews whose normalised text also appears in another review ("Great product!" ignored).

    :param texts: Review bodies.
    :param min_chars: Minimum normalised length for a review to count.
    :return: The share, 0 when no review is long enough.
    """
    normalised = [n for n in (_normalise(t) for t in texts) if len(n) >= min_chars]
    if not normalised:
        return 0.0
    counts: dict[str, int] = {}
    for n in normalised:
        counts[n] = counts.get(n, 0) + 1
    return sum(c for c in counts.values() if c > 1) / len(normalised)


def rating_shares(facts: PageFacts) -> tuple[float, float, float] | None:
    """
    Shares of 1-star, middle (2–4) and 5-star ratings, from the histogram or else the sampled reviews.

    :param facts: The page facts.
    :return: ``(one, middle, five)``, or ``None`` when there's nothing to go on.
    """
    if facts.rating_histogram:
        total = sum(facts.rating_histogram.values()) or 1.0
        h = {star: facts.rating_histogram.get(star, 0.0) / total for star in range(1, 6)}
        return h[1], h[2] + h[3] + h[4], h[5]
    stars = [round(r.rating) for r in facts.reviews if r.rating is not None]
    if not stars:
        return None
    return stars.count(1) / len(stars), sum(1 for s in stars if 2 <= s <= 4) / len(stars), stars.count(5) / len(stars)


def reviews_per_day(facts: PageFacts, today: date) -> float | None:
    """
    Ratings per day since the listing appeared.

    The listing's start is "date first available", else the oldest sampled review; the end is ``today`` (the day
    the page was seen: evaluations of archived pages pass their capture date). The end used to be the newest
    sampled review, but Amazon shows its "top" reviews, which can be years old, and that made a slow-selling
    listing look like a review burst.

    :param facts: The page facts.
    :param today: The day the page was seen.
    :return: The rate, or ``None`` without a rating count or start date.
    """
    dated = sorted(r.posted for r in facts.reviews if r.posted)
    start = facts.date_first_available or (dated[0] if dated else None)
    if start is None or not facts.rating_count:
        return None
    return facts.rating_count / max((max(today, start) - start).days, 1)


def burst_share(reviews: list[Review], window_days: int = 30) -> float:
    """
    Share of dated sampled reviews posted within ``window_days`` of the oldest one.

    :param reviews: The sampled reviews.
    :param window_days: Window length.
    :return: The share, 0 with fewer than 5 dated reviews.
    """
    dated = sorted(r.posted for r in reviews if r.posted)
    if len(dated) < 5:
        return 0.0
    cutoff = dated[0] + timedelta(days=window_days)
    return sum(1 for d in dated if d <= cutoff) / len(dated)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


async def title_similarities(title: str, bodies: list[str]) -> list[float]:
    """
    Cosine similarity between the product title and each review body.

    :param title: The product title.
    :param bodies: Review bodies (at most ``_MAX_EMBEDDED_REVIEWS`` are embedded).
    :return: One similarity per embedded review.
    """
    texts = [title[:_EMBED_CHARS], *[b[:_EMBED_CHARS] for b in bodies[:_MAX_EMBEDDED_REVIEWS]]]
    vectors = await get_embeddings().aembed_documents(texts)
    return [_cosine(vectors[0], v) for v in vectors[1:]]


class ReviewsSkill(Skill):
    """Review velocity, rating shape, duplicates, bursts and review/title similarity."""

    name = "reviews"
    description = "Checks whether the review record is plausible for the listing's age and content."
    features = (
        "has_reviews",
        "log_reviews_per_day",
        "share_five",
        "share_one",
        "share_middle",
        "j_shaped",
        "duplicate_share",
        "short_share",
        "verified_share",
        "burst_share",
        "title_similarity_mean",
        "off_topic_share",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Compute review features from ``facts`` (one embedding call when the page shows review texts).

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        if not facts.reviews and not facts.rating_count:
            return self.output({"has_reviews": False}, note="No reviews or ratings on the page.")
        features: dict[str, float | bool] = {"has_reviews": True}
        observations: list[Observation] = []
        _velocity(facts, features, observations)
        _shape(facts, features, observations)
        _texts(facts, features, observations)
        await _similarity(facts, features, observations)
        return self.output(features, observations)


def _velocity(facts: PageFacts, features: dict[str, float | bool], observations: list[Observation]) -> None:
    rate = reviews_per_day(facts, datetime.now(tz=UTC).date())
    if rate is None:
        return
    features["log_reviews_per_day"] = math.log10(1 + rate)
    dated = [r.posted for r in facts.reviews if r.posted]
    start = facts.date_first_available or (min(dated) if dated else None)
    if rate >= 5 and start is not None:
        observations.append(
            Observation(
                type="review_velocity",
                evidence=f"{facts.rating_count:,} ratings since {start.isoformat()} ({rate:.1f} per day).",
            )
        )


def _shape(facts: PageFacts, features: dict[str, float | bool], observations: list[Observation]) -> None:
    if not (shares := rating_shares(facts)):
        return
    one, middle, five = shares
    j_shaped = five >= 0.55 and one >= 0.12 and middle <= 0.33
    features |= {"share_one": one, "share_middle": middle, "share_five": five, "j_shaped": j_shaped}
    if j_shaped:
        observations.append(
            Observation(type="rating_shape", evidence=f"{five:.0%} five-star and {one:.0%} one-star ratings.")
        )


def _texts(facts: PageFacts, features: dict[str, float | bool], observations: list[Observation]) -> None:
    texts = [r.body for r in facts.reviews if r.body]
    if texts:
        dup = duplicate_share(texts)
        features["duplicate_share"] = dup
        features["short_share"] = sum(1 for t in texts if len(t.split()) < 5) / len(texts)
        if dup >= 0.15:
            observations.append(Observation(type="duplicates", evidence=f"{dup:.0%} of reviews repeat another."))
    verified = [r.verified for r in facts.reviews if r.verified is not None]
    if verified:
        features["verified_share"] = sum(verified) / len(verified)
    features["burst_share"] = burst_share(facts.reviews)


async def _similarity(facts: PageFacts, features: dict[str, float | bool], observations: list[Observation]) -> None:
    bodies = [r.body for r in facts.reviews if len(r.body) >= 20]
    if not facts.title or len(bodies) < 3:
        return
    try:
        similarities = await title_similarities(facts.title, bodies)
    except Exception as exc:  # noqa: BLE001 - embeddings are one signal among many
        logger.warning("review_embeddings_failed", error=type(exc).__name__)
        return
    off_topic = sum(1 for s in similarities if s < _OFF_TOPIC_SIMILARITY) / len(similarities)
    features["title_similarity_mean"] = sum(similarities) / len(similarities)
    features["off_topic_share"] = off_topic
    if off_topic >= 0.3:
        observations.append(
            Observation(
                type="off_topic_reviews", evidence=f"{off_topic:.0%} of reviews don't seem to be about this product."
            )
        )
