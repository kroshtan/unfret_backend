"""
Deterministic listing features used to weak-label marketplace products.

These are deliberately simple and transparent: they only decide labels at confident extremes, and an independent
LLM second opinion has to agree before a weak label counts as ground truth.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_PINYIN_RE = re.compile(r"zh|xi|qi|xu|ao|uo|ang|eng|ong|iu|ui")
_CONSONANT_RUN_RE = re.compile(r"[bcdfghjklmnpqrstvwxz]{4,}")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")

DAY_MS = 86_400_000


def generated_brand_score(
    brand: str, dictionary: frozenset[str] = frozenset(), known: frozenset[str] = frozenset()
) -> float:
    """
    Score how much a brand name looks machine-generated ("ZHUIBO", "XIEERDUO", "HUAXINGDA"), from 0 to 1.

    Real brands are usually dictionary words, people's names, or mixed-case names; generated marketplace brands
    are typically a single all-caps token of 5-12 letters with pinyin-like letter pairs or unusual letters.

    :param brand: The brand or store name.
    :param dictionary: Lower-case English words; a brand that is a dictionary word scores 0.
    :param known: Lower-case names of established brands, which always score 0.
    :return: The score.
    """
    name = brand.strip()
    lowered = name.lower()
    if not name or lowered in known or lowered in dictionary:
        return 0.0
    tokens = name.split()
    score = 0.0
    if len(tokens) == 1 and name.isalpha() and 5 <= len(name) <= 12:
        score += 0.35
        if name.isupper():
            score += 0.25
    if _PINYIN_RE.search(lowered):
        score += 0.2
    if any(c in lowered for c in "xzq"):
        score += 0.1
    if _CONSONANT_RUN_RE.search(lowered):
        score += 0.1
    if len(tokens) > 1 and all(t.lower() in dictionary for t in tokens):
        score = 0.0
    return min(score, 1.0)


def normalise_review(text: str) -> str:
    """Lower-case, strip punctuation and collapse whitespace, for duplicate detection."""
    return _WS_RE.sub(" ", _NON_ALNUM_RE.sub(" ", text.lower())).strip()


def duplicate_share(texts: Iterable[str], min_chars: int = 12) -> float:
    """
    Share of reviews whose normalised text also appears in another review of the same product.

    Very short reviews ("great", "love it") are ignored, since they repeat naturally.

    :param texts: Review bodies.
    :param min_chars: Minimum normalised length for a review to count.
    :return: The share, 0 when no review is long enough.
    """
    normalised = [n for n in (normalise_review(t) for t in texts) if len(n) >= min_chars]
    if not normalised:
        return 0.0
    counts: dict[str, int] = {}
    for n in normalised:
        counts[n] = counts.get(n, 0) + 1
    duplicated = sum(c for c in counts.values() if c > 1)
    return duplicated / len(normalised)


@dataclass(frozen=True)
class RatingShape:
    """Share of 1-star, middle (2-4 star) and 5-star ratings."""

    one: float
    middle: float
    five: float

    @property
    def j_shaped(self) -> bool:
        """Polarised ratings (many 5s, notable 1s, few in between), typical of manipulated listings."""
        return self.five >= 0.55 and self.one >= 0.12 and self.middle <= 0.33


def rating_shape(histogram: Sequence[int]) -> RatingShape:
    """
    Summarise a 1-to-5-star histogram.

    :param histogram: Counts for 1, 2, 3, 4 and 5 stars.
    :return: The rating shape; all zeros for an empty histogram.
    """
    total = sum(histogram)
    if total == 0:
        return RatingShape(0.0, 0.0, 0.0)
    return RatingShape(histogram[0] / total, sum(histogram[1:4]) / total, histogram[4] / total)


def early_review_burst(timestamps_ms: Sequence[int], window_days: int = 180) -> int:
    """
    Number of reviews in the first ``window_days`` after the first review.

    :param timestamps_ms: Review times in milliseconds since the epoch.
    :param window_days: Length of the window.
    :return: The count.
    """
    if not timestamps_ms:
        return 0
    start = min(timestamps_ms)
    return sum(1 for t in timestamps_ms if t - start <= window_days * DAY_MS)


def keyword_stuffed(title: str) -> bool:
    """Long, comma-chained titles ("Earbuds, Bluetooth 5.4, Headphones, IPX7, ...") typical of generic listings."""
    return len(title) >= 120 and title.count(",") >= 3
