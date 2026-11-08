from abc import ABC, abstractmethod
from typing import Any, ClassVar, Literal, TypedDict

from pydantic import BaseModel, Field

from veridex.graph.state import AnalysisState, Finding, PageType

Tier = Literal["cheap", "expensive"]

PRODUCT_PAGES: frozenset[PageType] = frozenset({"aggregate_listing", "storefront"})
STOREFRONTS: frozenset[PageType] = frozenset({"storefront"})
MARKETPLACES: frozenset[PageType] = frozenset({"aggregate_listing"})


class SkillOutput(TypedDict):
    """What a skill contributes to the graph state."""

    features: dict[str, float]
    findings: list[Finding]
    skill_results: list[str]


class Observation(BaseModel):
    """One factual observation a skill makes, before it is attributed to the skill."""

    type: str
    evidence: str = Field(max_length=500)


class Skill(ABC):
    """
    Base class for all analysis skills.

    A skill reads the shared state (``facts``, ``cleaned_content``, ``url``) and reports two things: numeric
    **features** for the scorer, and factual **findings** (a type plus evidence a person can check, never a
    verdict) for the explanation. :meth:`output` turns them into the state update.

    To add a new skill:
    1. Create a subclass of ``Skill`` in ``veridex/graph/skills/``.
    2. Implement :meth:`run`, returning ``self.output(features, observations)``.
    3. Register an instance in ``SKILLS`` inside ``veridex/graph/__init__.py``.

    ``page_types`` limits which pages it runs on; ``tier`` tells the triage whether it costs money or time
    (``expensive``) or not; skills with ``always_run = False`` only run when their name is in ``state["flags"]``.
    """

    name: str
    description: str
    always_run: bool = True
    tier: Tier = "cheap"
    page_types: ClassVar[frozenset[PageType]] = PRODUCT_PAGES
    # Feature names this skill reports. Missing ones are filled with 0, so the scorer sees a fixed vector.
    features: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Execute the skill against the current analysis state.

        :param state: The current graph state (``facts`` and ``cleaned_content`` are available).
        :return: The skill's features, findings and a plain-text summary (see :meth:`output`).
        """
        ...

    def output(
        self, features: dict[str, Any], observations: list[Observation] | None = None, note: str = ""
    ) -> SkillOutput:
        """
        Build the state update for this skill.

        :param features: Feature values (numbers or booleans), keyed without the skill-name prefix.
        :param observations: Factual observations for the explanation.
        :param note: A line of context for the summary when there is nothing to observe (e.g. "No reviews").
        :return: Prefixed features, attributed findings and a summary line block.
        """
        values = {f"{self.name}.{k}": float(v) for k, v in features.items() if v is not None}
        findings = [Finding(skill=self.name, type=o.type, evidence=o.evidence) for o in observations or []]
        lines = [f"[{self.name}]"]
        lines += [f"- {f.type}: {f.evidence}" for f in findings]
        if note:
            lines.append(note)
        if not findings and not note:
            lines.append("Nothing notable.")
        return {"features": values, "findings": findings, "skill_results": ["\n".join(lines)]}

    def unavailable(self) -> SkillOutput:
        """
        The state update for a skill that failed or timed out: no features, and a marker line.

        :return: The update.
        """
        return {"features": {}, "findings": [], "skill_results": [f"[{self.name}]\nSkill unavailable."]}
