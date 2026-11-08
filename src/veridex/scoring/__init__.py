"""
The feature scorer: skill features → a calibrated probability that a page is unreliable (junk or scam).

A small logistic regression trained on the labelled corpus (``evals/train.py``) and shipped as a JSON artifact
(``model.json``): feature names, scaling, coefficients and an optional isotonic calibration table. Inference is
plain Python, so production needs no ML library. Because the model is linear, each feature's contribution to the
score is exact, and that is the ``weight`` of the flags: why a page scored as it did.

The score is deterministic (same features, same score), so page text can only move it through features, not by
talking to an LLM judge.
"""

import json
import math
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the graph imports this module, so no runtime import back into it
    from veridex.graph.state import Finding

MODEL_PATH = Path(__file__).with_name("model.json")


@dataclass(frozen=True)
class Flag:
    """A signal that moved the score, with evidence a person can check."""

    type: str
    evidence: str
    weight: float


@dataclass(frozen=True)
class Assessment:
    """The scorer's output."""

    risk_score: float
    confidence: float
    flags: list[Flag] = field(default_factory=list)
    model_version: str = ""


@dataclass(frozen=True)
class Model:
    """A trained linear model, loaded from its JSON artifact."""

    version: str
    features: list[str]
    means: list[float]
    scales: list[float]
    coefficients: list[float]
    intercept: float
    calibration: list[tuple[float, float]]  # isotonic (raw probability, calibrated) points, ascending

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Model:
        """
        Build a model from its artifact.

        :param data: The parsed ``model.json``.
        :return: The model.
        :raises ValueError: If the artifact is inconsistent.
        """
        n = len(data["features"])
        if not (len(data["means"]) == len(data["scales"]) == len(data["coefficients"]) == n):
            raise ValueError("model artifact: feature, scaling and coefficient lengths differ")
        return cls(
            version=str(data["version"]),
            features=list(data["features"]),
            means=[float(v) for v in data["means"]],
            scales=[float(v) or 1.0 for v in data["scales"]],
            coefficients=[float(v) for v in data["coefficients"]],
            intercept=float(data["intercept"]),
            calibration=[(float(x), float(y)) for x, y in data.get("calibration", [])],
        )

    def contributions(self, features: dict[str, float]) -> list[float]:
        """
        Each feature's contribution to the log-odds (standardised value times coefficient).

        :param features: Skill features; missing ones count as 0.
        :return: One contribution per model feature.
        """
        return [
            c * (features.get(name, 0.0) - m) / s
            for name, m, s, c in zip(self.features, self.means, self.scales, self.coefficients, strict=True)
        ]

    def probability(self, features: dict[str, float]) -> float:
        """
        The calibrated probability that the page is unreliable.

        :param features: Skill features.
        :return: A probability between 0 and 1.
        """
        logit = self.intercept + sum(self.contributions(features))
        raw = 1 / (1 + math.exp(-max(min(logit, 30.0), -30.0)))
        return _interpolate(self.calibration, raw) if self.calibration else raw


def _interpolate(points: list[tuple[float, float]], x: float) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


@cache
def load_model(path: Path = MODEL_PATH) -> Model | None:
    """
    Load the shipped model, once per process.

    :param path: The artifact path.
    :return: The model, or ``None`` when no artifact is shipped (the LLM judge scores instead).
    """
    if not path.exists():
        return None
    return Model.from_dict(json.loads(path.read_text()))


def _describe(name: str, value: float) -> str:
    return f"{name} = {value:g}"


def _evidence(name: str, value: float, skill_findings: list[Finding]) -> str:
    # The finding whose type shares a word with the feature (listing.discount_pct ↔ "discount"), else the value.
    words = set(name.split(".", 1)[-1].split("_"))
    for f in skill_findings:
        if words & set(f.type.split("_")):
            return f.evidence
    return _describe(name, value)


def score(
    features: dict[str, float], findings: list[Finding] | None = None, model: Model | None = None
) -> Assessment | None:
    """
    Score a page from its skill features.

    ``confidence`` combines how decisive the probability is (``|2p - 1|``) with how many of the model's features
    were measured on this page (a page where most skills found nothing to measure is scored, but less surely).
    ``flags`` are the features that pushed the score towards unreliable, strongest first, each with the matching
    skill's finding as evidence where there is one.

    :param features: The skill features from the graph state.
    :param findings: The skill findings, used as evidence for the flags.
    :param model: The model to use; the shipped one by default.
    :return: The assessment, or ``None`` when no model is available.
    """
    model = model or load_model()
    if model is None:
        return None
    risk = model.probability(features)
    measured = sum(1 for name in model.features if name in features) / len(model.features)
    confidence = abs(2 * risk - 1) * (0.5 + 0.5 * measured)

    by_skill: dict[str, list[Finding]] = {}
    for f in findings or []:
        by_skill.setdefault(f.skill, []).append(f)
    contributions = sorted(
        zip(model.features, model.contributions(features), strict=True), key=lambda t: t[1], reverse=True
    )
    flags: list[Flag] = []
    for name, contribution in contributions:
        if contribution <= 0.05 or len(flags) >= 6:
            break
        evidence = _evidence(name, features.get(name, 0.0), by_skill.get(name.split(".", 1)[0], []))
        flags.append(Flag(type=name, evidence=evidence, weight=round(contribution, 3)))
    return Assessment(
        risk_score=round(risk, 4), confidence=round(confidence, 4), flags=flags, model_version=model.version
    )
