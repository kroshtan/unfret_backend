import pytest

from veridex import scoring
from veridex.graph import SKILLS
from veridex.graph.skills.eu_registry import EuRegistrySkill
from veridex.x402.verdict import QUICK_SKILLS, action_for, skills_for, to_verdict

# Captured at import, before the suite's autouse fixture hides the shipped model.
_LOAD_MODEL = scoring.load_model


@pytest.mark.parametrize(
    ("risk", "policy", "action"),
    [
        (0.1, "strict", "allow"),
        (0.25, "strict", "warn"),
        (0.5, "strict", "block"),
        (0.39, "balanced", "allow"),
        (0.4, "balanced", "warn"),
        (0.7, "balanced", "block"),
        (0.59, "lenient", "allow"),
        (0.6, "lenient", "warn"),
        (0.84, "lenient", "warn"),
        (0.85, "lenient", "block"),
    ],
)
def test_policy_thresholds(risk: float, policy: str, action: str) -> None:
    assert action_for(risk, policy) == action  # type: ignore[arg-type]


def test_verdict_uses_the_scorer_assessment() -> None:
    result = {
        "score": 17,
        "explanation": "Generic goods.",
        "skill_results": ["[content]\nUnknown brand."],
        "assessment": {
            "risk_score": 0.83,
            "confidence": 0.71,
            "flags": [
                {"type": "reviews.per_day_since_listing", "evidence": "4,800 reviews in 5 weeks", "weight": 1.42},
                {"type": "listing.discount_pct", "evidence": "85% off a list price", "weight": 0.31},
            ],
            "model_version": "v1-20261108-B",
        },
        "verdict": {
            "band": "junk_or_scam",
            "label": "Likely junk or a scam",
            "headline": "Thousands of reviews on a brand-new listing and a too-good discount.",
            "reasons": [
                {
                    "type": "review_velocity",
                    "title": "Suspicious number of reviews",
                    "detail": "4,800 ratings for a product that's only been listed for 5 weeks.",
                    "severity": "high",
                },
                {
                    "type": "big_discount",
                    "title": "Too-good discount",
                    "detail": "An 85% discount on a 'was' price that may never have been charged.",
                    "severity": "medium",
                },
            ],
            "positives": [],
            "language": "en",
        },
    }

    verdict = to_verdict(result, policy="balanced", tier="quick")

    assert (verdict.action, verdict.risk_score, verdict.confidence) == ("block", 0.83, 0.71)
    # Agents get the same plain evidence a shopper sees, plus the machine fields.
    assert [f.type for f in verdict.flags] == ["review_velocity", "big_discount"]
    assert verdict.flags[0].evidence.startswith("4,800 ratings")
    assert [f.weight for f in verdict.flags] == [1.0, 0.6]
    assert (verdict.band, verdict.summary) == (
        "junk_or_scam",
        "Thousands of reviews on a brand-new listing and a too-good discount.",
    )
    assert [(f.label, f.severity) for f in verdict.flags] == [
        ("Suspicious number of reviews", "high"),
        ("Too-good discount", "medium"),
    ]
    assert verdict.assessed is True
    assert verdict.model_version == "v1-20261108-B"


def test_falls_back_to_the_judge_without_an_assessment() -> None:
    verdict = to_verdict({"score": 80, "explanation": "x", "skill_results": []}, policy="strict", tier="quick")
    assert (verdict.risk_score, verdict.model_version) == (0.2, None)


def test_verdict_from_judge_and_skills() -> None:
    result = {
        "score": 20,
        "explanation": "Generic goods.",
        "skill_results": [
            "[classify]\nPage type: aggregate_listing (Amazon)",
            "[page_content]\nFake urgency, unknown brand.",
            "[html_source_signals]\nSkill unavailable.",
        ],
    }

    verdict = to_verdict(result, policy="balanced", tier="quick")

    assert (verdict.action, verdict.risk_score) == ("block", 0.8)
    assert [f.type for f in verdict.flags] == ["page_content"]
    assert verdict.flags[0].weight == 1.0
    assert verdict.confidence == 0.25  # half of the share of skills that produced evidence
    assert all("scam" not in f.type for f in verdict.flags)


def test_unassessable_page_asks_the_human() -> None:
    verdict = to_verdict(
        {"score": -1, "explanation": "No product referenced on this page"}, policy="lenient", tier="deep"
    )
    # Not "risk 0.0" (which reads as safe): explicitly not assessed.
    assert (verdict.action, verdict.assessed, verdict.risk_score, verdict.confidence) == ("warn", False, None, 0.0)
    assert verdict.summary == "No product referenced on this page"
    assert verdict.flags[0].type == "not_assessable"


def test_quick_tier_runs_no_fetching_skills() -> None:
    names = {s.name for s in skills_for("quick", SKILLS)}
    assert names == QUICK_SKILLS
    assert not names & {"reverse_image", "domain_age"}  # eu_registry runs, without VIES (next test)
    assert skills_for("deep", SKILLS) == SKILLS


def test_quick_tier_keeps_eu_registry_local() -> None:
    quick = {s.name: s for s in skills_for("quick", SKILLS)}
    deep = {s.name: s for s in skills_for("deep", SKILLS)}

    assert isinstance(quick["eu_registry"], EuRegistrySkill) and quick["eu_registry"].vies is False
    assert isinstance(deep["eu_registry"], EuRegistrySkill) and deep["eu_registry"].vies is True
    assert {"domain_age", "reverse_image"}.isdisjoint(quick)


def test_quick_tier_computes_every_feature_of_the_full_model_or_ships_its_own() -> None:
    # Scoring a feature the tier never computed as 0 reads "never checked" as "absent". Either the quick tier covers
    # every feature of the shipped model, or a quick-tier model (evals.train --quick) must be shipped.
    model = _LOAD_MODEL.__wrapped__(scoring.MODEL_PATH)
    if model is None:
        pytest.skip("no model shipped")
    computed = {f"{s.name}.{f}" for s in skills_for("quick", SKILLS) for f in s.features}
    missing = set(model.features) - computed
    assert not missing or scoring.MODEL_QUICK_PATH.exists(), f"quick tier lacks {sorted(missing)}"
