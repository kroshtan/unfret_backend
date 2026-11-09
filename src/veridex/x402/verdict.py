"""Tiers, policies and the mapping from an analysis result to a machine-actionable verdict for agents."""

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from veridex.graph.skills import Skill
from veridex.graph.skills.eu_registry import EuRegistrySkill

Tier = Literal["quick", "deep"]
Policy = Literal["strict", "balanced", "lenient"]
Action = Literal["allow", "warn", "block"]

# The quick tier runs only skills that need no outbound fetches about the shop (no RDAP/CT, VIES or reverse image
# search); the deep tier runs every skill. eu_registry runs in both, without its VIES lookup in quick, so the quick
# tier computes the same features the shipped scorer was trained on (offline, without network lookups). Change
# tiers here and nowhere else.
QUICK_SKILLS: frozenset[str] = frozenset(
    {
        "content",
        "listing",
        "pricing",
        "reviews",
        "html_source_signals",
        "dark_patterns",
        "injection",
        "eu_registry",
        "us_identity",
        "shop_policies",
        "shop_links",
    }
)

# risk_score thresholds per caller policy: at or above `block` → block; at or above `warn` → warn ("ask your
# human"); below → allow. The caller chooses the policy; the flags stay factual either way.
POLICY_THRESHOLDS: dict[Policy, tuple[float, float]] = {
    "strict": (0.25, 0.5),
    "balanced": (0.4, 0.7),
    "lenient": (0.6, 0.85),
}

_SEVERITY_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}
_SKILL_ENTRY_RE = re.compile(r"\A\[([^\]]+)\]\n(.*)\Z", re.DOTALL)
_EVIDENCE_CHARS = 400


def skills_for(tier: Tier, skills: list[Skill]) -> list[Skill]:
    """
    Select the skills a tier runs.

    :param tier: ``quick`` or ``deep``.
    :param skills: The full skill registry.
    :return: The skills for that tier.
    """
    if tier == "deep":
        return skills
    return [EuRegistrySkill(vies=False) if s.name == "eu_registry" else s for s in skills if s.name in QUICK_SKILLS]


class Flag(BaseModel):
    """One factual signal and how much it contributed."""

    type: str
    evidence: str
    weight: float = Field(
        ge=0, description="How much this signal matters (larger = more; high 1.0, medium 0.6, low 0.3)."
    )
    label: str | None = Field(default=None, description="A short human label for the signal.")
    severity: str | None = Field(default=None, description="high, medium or low.")


class Verdict(BaseModel):
    """The response of ``POST /v1/check``."""

    action: Action = Field(description='"warn" means: ask your human before buying.')
    assessed: bool = Field(
        default=True, description="False when the page couldn't be assessed (not a product page): risk_score is null."
    )
    risk_score: float | None = Field(ge=0, le=1, description="0 = no concerns, 1 = clear risk; null if not assessed.")
    confidence: float = Field(ge=0, le=1)
    flags: list[Flag]
    policy_applied: Policy
    tier: Tier
    cached: bool = False
    checked_at: datetime
    model_version: str | None = Field(default=None, description="The scorer model that set risk_score, if any.")
    band: str | None = Field(
        default=None, description="reliable, concerns, careful or junk_or_scam: the same verdict a shopper sees."
    )
    summary: str | None = Field(default=None, description="One plain sentence for the human you're buying for.")
    page_type: str | None = Field(
        default=None, description="aggregate_listing (marketplace), storefront, article or non_product."
    )


def action_for(risk_score: float, policy: Policy) -> Action:
    """
    Apply a caller policy to a risk score.

    :param risk_score: Risk from 0 (no concerns) to 1.
    :param policy: The caller's policy.
    :return: The action for the agent to take.
    """
    warn, block = POLICY_THRESHOLDS[policy]
    if risk_score >= block:
        return "block"
    if risk_score >= warn:
        return "warn"
    return "allow"


def to_verdict(result: dict[str, Any], *, policy: Policy, tier: Tier) -> Verdict:
    """
    Turn an analysis result into a verdict.

    The trained scorer's ``assessment`` (risk_score, confidence, weighted flags, model_version) is used whenever the
    graph produced one. Only without a shipped model does it fall back to the LLM judge: risk_score from its 0–100
    score, confidence capped at 0.5 (half the share of skills that produced evidence), one equally weighted flag
    per skill finding.

    :param result: The final graph state (``score``, ``explanation``, ``skill_results``, optional ``assessment``).
    :param policy: The caller's policy.
    :param tier: The tier that ran.
    :return: The verdict.
    """
    now = datetime.now(UTC)
    if result["score"] == -1:
        # Not a product page (or empty): nothing to assess, so leave the decision to the human.
        explanation = str(result.get("explanation", ""))
        flag = Flag(type="not_assessable", evidence=explanation, weight=1.0, label="Not assessable", severity="low")
        return Verdict(
            action="warn",
            assessed=False,
            risk_score=None,
            confidence=0.0,
            flags=[flag],
            policy_applied=policy,
            tier=tier,
            checked_at=now,
            summary=explanation or None,
            page_type=result.get("page_type"),
        )

    assessment = result.get("assessment")
    if assessment:
        risk_score = float(assessment["risk_score"])
        shopper_verdict = result.get("verdict") or {}
        action = action_for(risk_score, policy)
        if assessment.get("degraded") and action == "allow":
            action = "warn"  # a key skill failed: never wave a purchase through on partial evidence
        return Verdict(
            action=action,
            risk_score=risk_score,
            confidence=float(assessment["confidence"]),
            flags=[
                Flag(
                    type=r["type"],
                    evidence=str(r["detail"])[:_EVIDENCE_CHARS],
                    weight=_SEVERITY_WEIGHT.get(r.get("severity", "low"), 0.3),
                    label=r.get("title"),
                    severity=r.get("severity"),
                )
                for r in shopper_verdict.get("reasons", [])
                if r.get("type") != "overall"
            ],
            policy_applied=policy,
            tier=tier,
            checked_at=now,
            model_version=str(assessment.get("model_version") or "") or None,
            band=shopper_verdict.get("band"),
            summary=shopper_verdict.get("headline") or result.get("explanation"),
            page_type=result.get("page_type"),
        )

    findings: list[tuple[str, str]] = []
    for entry in result.get("skill_results", []):
        match = _SKILL_ENTRY_RE.match(entry)
        if match:
            findings.append((match.group(1), match.group(2).strip()))
    evidence = [(name, text) for name, text in findings if name != "classify" and text != "Skill unavailable."]
    skills_run = [name for name, _ in findings if name != "classify"]
    weight = 1 / len(evidence) if evidence else 0.0
    flags = [Flag(type=name, evidence=text[:_EVIDENCE_CHARS], weight=round(weight, 4)) for name, text in evidence]

    risk_score = round(1 - result["score"] / 100, 4)
    confidence = round(0.5 * len(evidence) / len(skills_run), 4) if skills_run else 0.0
    return Verdict(
        action=action_for(risk_score, policy),
        risk_score=risk_score,
        confidence=confidence,
        flags=flags,
        policy_applied=policy,
        tier=tier,
        checked_at=now,
        summary=str(result.get("explanation") or "") or None,
        page_type=result.get("page_type"),
    )
