"""The feature scorer: artifact loading, calibrated probability, contributions, flags, and its use in the graph."""

import json
import math
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda

from unfret import scoring
from unfret.graph import SKILLS, nodes
from unfret.graph.state import Finding
from unfret.scoring import Model

# Captured at import, before the suite's autouse fixture replaces it.
_LOAD_MODEL = scoring.load_model

FEATURES = ["listing.discount_pct", "content.brand_recognized", "reviews.off_topic_share"]
ARTIFACT = {
    "version": "v1-test",
    "features": FEATURES,
    "means": [0.2, 0.5, 0.1],
    "scales": [0.2, 0.5, 0.0],
    "coefficients": [2.0, -1.5, 3.0],
    "intercept": -0.5,
    "calibration": [],
}


def test_probability_is_logistic_over_standardised_features() -> None:
    model = Model.from_dict(ARTIFACT)
    features = {"listing.discount_pct": 0.6, "content.brand_recognized": 0.0}

    # (0.6-0.2)/0.2*2 = 4; (0-0.5)/0.5*-1.5 = 1.5; missing off-topic share counts as 0: (0-0.1)/1*3 = -0.3
    logit = -0.5 + 4 + 1.5 - 0.3
    assert model.probability(features) == pytest.approx(1 / (1 + math.exp(-logit)))
    assert model.contributions(features) == pytest.approx([4.0, 1.5, -0.3])


def test_calibration_interpolates_and_clamps() -> None:
    model = Model.from_dict({**ARTIFACT, "calibration": [[0.1, 0.0], [0.5, 0.4], [0.9, 1.0]]})
    assert scoring._interpolate(model.calibration, 0.3) == pytest.approx(0.2)
    assert scoring._interpolate(model.calibration, 0.05) == 0.0
    assert scoring._interpolate(model.calibration, 0.99) == 1.0


def test_inconsistent_artifact_is_rejected() -> None:
    with pytest.raises(ValueError, match="lengths differ"):
        Model.from_dict({**ARTIFACT, "coefficients": [1.0]})


def test_score_flags_the_strongest_risk_signals_with_evidence() -> None:
    model = Model.from_dict(ARTIFACT)
    findings = [
        Finding(skill="listing", type="brand_name", evidence='The brand "ZHUIBO" looks generated.'),
        Finding(skill="listing", type="discount", evidence="Price 19.99 against a 'was' price of 129.99 (85% off)."),
    ]
    assessment = scoring.score(
        {"listing.discount_pct": 0.85, "content.brand_recognized": 0.0, "reviews.off_topic_share": 0.5},
        findings,
        model,
    )

    assert assessment is not None
    assert assessment.model_version == "v1-test"
    assert assessment.risk_score > 0.99
    # "Brand recognised = 0" raises the risk, but an absence is never flagged (it would read as an accusation).
    assert [f.type for f in assessment.flags] == ["listing.discount_pct", "reviews.off_topic_share"]
    assert assessment.flags[0].evidence.startswith("Price 19.99")  # matched by type, not the first finding
    assert assessment.flags[1].evidence == ""  # no matching finding: never the raw value; clients show the label
    assert assessment.confidence == pytest.approx(abs(2 * assessment.risk_score - 1), abs=0.01)


def test_confidence_drops_when_few_features_were_measured() -> None:
    model = Model.from_dict(ARTIFACT)
    full = scoring.score(dict.fromkeys(FEATURES, 1.0), model=model)
    sparse = scoring.score({"listing.discount_pct": 1.0}, model=model)
    assert full is not None and sparse is not None
    assert sparse.confidence < full.confidence


def test_no_model_means_no_assessment() -> None:
    assert scoring.score({"x": 1.0}) is None  # the test suite hides the shipped artifact


def test_load_model_reads_an_artifact(tmp_path: Path) -> None:
    path = tmp_path / "model.json"
    path.write_text(json.dumps(ARTIFACT))
    model = _LOAD_MODEL.__wrapped__(path)
    assert model is not None and model.version == "v1-test"
    assert _LOAD_MODEL.__wrapped__(tmp_path / "missing.json") is None


def test_shipped_artifact_matches_the_skills() -> None:
    model = _LOAD_MODEL()
    assert model is not None, "the model ships in the repo"
    declared = {f"{s.name}.{f}" for s in SKILLS for f in s.features}
    assert set(model.features) <= declared  # every model feature is still produced by a skill


async def test_judge_uses_the_scorer_and_only_asks_the_llm_to_explain(monkeypatch: pytest.MonkeyPatch) -> None:
    model = Model.from_dict(ARTIFACT)
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: model)
    prompts: list[Any] = []

    def structured(schema: type) -> RunnableLambda:
        def answer(messages: Any) -> Any:
            prompts.append((schema, messages))
            return nodes._Explanation(explanation="An 85% 'discount' and an unknown brand.")

        return RunnableLambda(answer)

    monkeypatch.setattr(nodes, "get_structured_llm", structured)
    state: Any = {
        "features": {"listing.discount_pct": 0.85, "content.brand_recognized": 0.0},
        "findings": [Finding(skill="listing", type="discount", evidence="85% off.")],
        "skill_results": [],
    }

    result = await nodes.judge_node(state)

    expected = round(100 * (1 - model.probability(state["features"])))
    assert result["score"] == expected
    assert result["explanation"] == "An 85% 'discount' and an unknown brand."
    assert result["verdict"]["headline"] == result["explanation"]
    assert result["verdict"]["band"] == "junk_or_scam"
    assert result["assessment"]["model_version"] == "v1-test"
    [(schema, messages)] = prompts
    assert schema is nodes._Explanation
    # The headline is written from the verdict's own plain-language points, never raw feature names or scores.
    assert "Many warning signs" in messages[0].content and "in English" in messages[0].content
    assert "Too-good discount" in messages[1].content
    assert "listing.discount_pct" not in messages[1].content
    assert f"{expected}/100" not in messages[0].content


def test_unmeasured_features_are_never_flagged() -> None:
    # A missing feature still counts as 0 in the probability (as in training), but a flag would claim evidence
    # ("brand not recognised") for something this page was never checked for.
    model = Model.from_dict(ARTIFACT)
    assessment = scoring.score({"listing.discount_pct": 0.85}, model=model)

    assert assessment is not None
    assert model.contributions({"listing.discount_pct": 0.85})[1] > 0.05  # it would have been flagged
    assert [f.type for f in assessment.flags] == ["listing.discount_pct"]


async def test_judge_scores_with_the_model_it_was_built_for(monkeypatch: pytest.MonkeyPatch) -> None:
    quick = Model.from_dict({**ARTIFACT, "version": "v1-quick-test"})
    full = Model.from_dict(ARTIFACT)
    paths: list[Path] = []

    def load(path: Path = scoring.MODEL_PATH) -> Model:
        paths.append(path)
        return quick if path == scoring.MODEL_QUICK_PATH else full

    monkeypatch.setattr(scoring, "load_model", load)
    monkeypatch.setattr(
        nodes, "get_structured_llm", lambda schema: RunnableLambda(lambda _: nodes._Explanation(explanation="x"))
    )
    state: Any = {"features": {"listing.discount_pct": 0.5}, "findings": [], "skill_results": []}

    quick_result = await nodes.make_judge_node(scoring.MODEL_QUICK_PATH)(state)
    full_result = await nodes.judge_node(state)

    assert quick_result["assessment"]["model_version"] == "v1-quick-test"
    assert full_result["assessment"]["model_version"] == "v1-test"
    assert paths[0] == scoring.MODEL_QUICK_PATH


async def test_judge_falls_back_to_the_full_model_without_a_quick_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    full = Model.from_dict(ARTIFACT)
    monkeypatch.setattr(
        scoring, "load_model", lambda path=scoring.MODEL_PATH: None if path == scoring.MODEL_QUICK_PATH else full
    )
    monkeypatch.setattr(
        nodes, "get_structured_llm", lambda schema: RunnableLambda(lambda _: nodes._Explanation(explanation="x"))
    )
    state: Any = {"features": {"listing.discount_pct": 0.5}, "findings": [], "skill_results": []}

    result = await nodes.make_judge_node(scoring.MODEL_QUICK_PATH)(state)

    assert result["assessment"]["model_version"] == "v1-test"


async def test_agents_get_the_template_headline_without_an_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: Model.from_dict(ARTIFACT))
    monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: pytest.fail("no LLM call for agents"))
    state: Any = {
        "features": {"listing.discount_pct": 0.85, "content.brand_recognized": 0.0},
        "findings": [],
        "skill_results": [],
        "explain": False,
    }

    result = await nodes.judge_node(state)

    assert result["explanation"].startswith("Many warning signs: ")
    assert "too-good discount" in result["explanation"]


async def test_a_rambling_headline_is_replaced_by_the_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: Model.from_dict(ARTIFACT))
    rambling = nodes._Explanation(explanation=" ".join(["word"] * 40))
    monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: RunnableLambda(lambda _: rambling))
    state: Any = {"features": {"listing.discount_pct": 0.85}, "findings": [], "skill_results": []}

    result = await nodes.judge_node(state)

    assert "word word" not in result["explanation"]
    assert len(result["explanation"].split()) <= 25


def test_a_failed_skill_scores_as_neutral_not_as_safe() -> None:
    # Regression (audit M3): a failed content skill left its features out, which the model read as 0 ("brand not
    # recognised" etc.), so an LLM outage moved every page's score.
    model = Model.from_dict(ARTIFACT)
    failed = {"listing.discount_pct": 0.6, "content.unavailable": 1.0}

    assert model.contributions(failed) == pytest.approx([4.0, 0.0, -0.3])  # content at its training mean


def test_a_failed_content_read_degrades_the_assessment() -> None:
    model = Model.from_dict(ARTIFACT)
    healthy = scoring.score(dict.fromkeys(FEATURES, 0.0), model=model)
    degraded = scoring.score({"listing.discount_pct": 0.0, "content.unavailable": 1.0}, model=model)

    assert healthy is not None and degraded is not None
    assert (healthy.degraded, degraded.degraded) == (False, True)
    assert degraded.confidence <= 0.3
    assert not any(f.type.endswith(".unavailable") for f in degraded.flags)
