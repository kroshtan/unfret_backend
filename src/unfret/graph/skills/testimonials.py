"""
Testimonials that look written rather than collected: copies, only 5 stars, placeholder names, bunched dates.

Copies include templates with a town or a name swapped; names can also repeat. On a company's own site its
testimonials (``facts.testimonials``); on a platform profile the platform's reviews (``facts.reviews``, without
names). Only what the page shows is compared; nothing is looked up and no names leave the check. A handful of
glowing testimonials is normal on a company's own site, so each signal needs a pattern across several of them.
"""

import re
from dataclasses import dataclass
from datetime import date

from unfret.facts.schema import PageFacts
from unfret.graph.skills import SERVICE_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

MIN_TESTIMONIALS = 3
_NEAR_COPY = 0.7  # word overlap (Jaccard) above which two testimonials are one text with a word or two swapped
_MIN_WORDS = 5
_CLUSTER_DAYS = 7
# Names that stand in for a real one in templates and examples.
_PLACEHOLDER_NAMES = frozenset(
    {"john doe", "jane doe", "jan jansen", "piet jansen", "jan modaal", "piet puk", "lorem ipsum", "max mustermann"}
    | {"test", "klant", "customer", "anonymous", "anoniem", "naam", "name"}
)
_WORD_RE = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class _Item:
    text: str
    name: str
    rating: float | None
    posted: date | None


def _items(facts: PageFacts) -> list[_Item]:
    if facts.testimonials:
        return [_Item(t.text, t.name, t.rating, t.posted) for t in facts.testimonials]
    return [_Item(r.body, "", r.rating, r.posted) for r in facts.reviews if r.body]


def _words(text: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall(text.lower()))


def copied_share(texts: list[str]) -> float:
    """
    The share of texts that repeat another one, word for word or with a word or two swapped (a town, a name).

    :param texts: The testimonial texts.
    :return: 0–1.
    """
    words = [_words(t) for t in texts]
    copied = 0
    for i, mine in enumerate(words):
        if len(mine) < _MIN_WORDS:
            continue
        for j, other in enumerate(words):
            if i != j and len(other) >= _MIN_WORDS and len(mine & other) / len(mine | other) >= _NEAR_COPY:
                copied += 1
                break
    return copied / len(texts) if texts else 0.0


class TestimonialsSkill(Skill):
    """Copied or templated testimonials, only top ratings, repeated or placeholder names, bunched dates."""

    __test__ = False  # not a pytest test class, whatever its name

    name = "testimonials"
    description = "Compares a service provider's testimonials with each other for copies and templates."
    page_types = SERVICE_PAGES
    features = ("count", "copied_share", "all_five", "repeated_names", "placeholder_names", "dates_clustered")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Compare the page's testimonials with each other.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        items = _items(state["facts"])
        if len(items) < MIN_TESTIMONIALS:
            return self.output({"count": len(items)}, note="Too few testimonials to compare.")
        n = len(items)
        copied = copied_share([i.text for i in items])
        ratings = [i.rating for i in items if i.rating is not None]
        all_five = len(ratings) >= max(4, round(n * 0.8)) and all(r >= 5 for r in ratings)
        names = [i.name.lower() for i in items if i.name]
        repeated = len(names) >= MIN_TESTIMONIALS and len(set(names)) < len(names)
        placeholders = sorted({i.name for i in items if i.name.lower() in _PLACEHOLDER_NAMES})
        dated = sorted(i.posted for i in items if i.posted is not None)
        clustered = len(dated) >= 4 and (dated[-1] - dated[0]).days <= _CLUSTER_DAYS

        observations: list[Observation] = []
        if copied >= 0.5:
            observations.append(
                Observation(
                    type="copied_testimonials",
                    evidence=f"{round(copied * n)} of {n} testimonials repeat another almost word for word.",
                )
            )
        if all_five:
            observations.append(Observation(type="all_five", evidence=f"All {len(ratings)} ratings are 5 stars."))
        if repeated:
            observations.append(
                Observation(type="repeated_names", evidence="The same name signs more than one testimonial.")
            )
        if placeholders:
            observations.append(
                Observation(type="placeholder_names", evidence=f'Signed with a placeholder name: "{placeholders[0]}".')
            )
        if clustered:
            span = (dated[-1] - dated[0]).days
            observations.append(
                Observation(
                    type="dates_clustered",
                    evidence=f"{len(dated)} testimonials, all dated within {span + 1} day{'s' if span else ''}.",
                )
            )
        return self.output(
            {
                "count": n,
                "copied_share": copied,
                "all_five": all_five,
                "repeated_names": repeated,
                "placeholder_names": len(placeholders),
                "dates_clustered": clustered,
            },
            observations,
        )
