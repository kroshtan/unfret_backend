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
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # the graph imports this module, so no runtime import back into it
    from unfret.graph.state import Finding

MODEL_PATH = Path(__file__).with_name("model.json")
# The quick tier skips the skills that look the shop up elsewhere, so it has its own model trained on exactly the
# features it computes (scoring a missing feature as 0 would read "never checked" as "absent").
MODEL_QUICK_PATH = Path(__file__).with_name("model-quick.json")


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
    # A key skill (the content read) failed: the score rests on partial evidence, confidence is capped, and an
    # automated caller should not "allow" on it alone.
    degraded: bool = False


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

        A skill that ran but had nothing to report leaves its features out, and training read those as 0, so
        missing ones count as 0. A skill that *failed* (it reports ``<skill>.unavailable``) never measured
        anything: its features are scored at the training mean, a contribution of 0, so a failure (an LLM outage,
        a timeout) can't make a page look safer than its evidence says. A skill whose rules don't apply to the page
        (``<skill>.not_applicable``, e.g. EU requirements on a US shop) is scored the same way, and so is a single
        feature that doesn't apply (``<skill>.<feature>.not_applicable``, e.g. an EU imprint on a US shop).

        :param features: Skill features.
        :return: One contribution per model feature.
        """
        neutral = neutral_features(features, self.features)
        return [
            0.0 if name in neutral else c * (features.get(name, 0.0) - m) / s
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


_UNAVAILABLE_SUFFIX = ".unavailable"  # = f".{unfret.graph.skills.UNAVAILABLE}" (no import: the graph imports us)
# Skills whose failure makes an assessment unreliable: the content read carries most of the model's weight.
KEY_SKILLS = frozenset({"content"})


def unavailable_skills(features: dict[str, float]) -> set[str]:
    """
    The skills that failed or timed out on this page.

    :param features: Skill features (a failed skill reports only ``<skill>.unavailable``).
    :return: Their names.
    """
    return {
        name.removesuffix(_UNAVAILABLE_SUFFIX) for name, v in features.items() if v and name.endswith(".unavailable")
    }


_NOT_APPLICABLE_SUFFIX = ".not_applicable"  # = f".{unfret.graph.skills.NOT_APPLICABLE}"


def not_applicable_skills(features: dict[str, float]) -> set[str]:
    """
    The skills whose rules don't apply to this page (they report ``<skill>.not_applicable``).

    :param features: Skill features.
    :return: Their names.
    """
    return {
        skill
        for name, v in features.items()
        if v
        and name.endswith(_NOT_APPLICABLE_SUFFIX)
        and "." not in (skill := name.removesuffix(_NOT_APPLICABLE_SUFFIX))
    }


def not_applicable_features(features: dict[str, float], names: Iterable[str]) -> set[str]:
    """
    The features whose rules don't apply to this page.

    That is every feature of a skill that doesn't apply, and single features reported as
    ``<skill>.<feature>.not_applicable``.

    :param features: Skill features.
    :param names: The feature names to check.
    :return: Those of ``names`` that don't apply.
    """
    skills = not_applicable_skills(features)
    return {n for n in names if n.split(".", 1)[0] in skills or features.get(n + _NOT_APPLICABLE_SUFFIX)}


def neutral_features(features: dict[str, float], names: Iterable[str]) -> set[str]:
    """
    The features scored as neutral (at the training mean): never measured, or not applicable to this page.

    :param features: Skill features.
    :param names: The feature names to check.
    :return: Those of ``names`` that are neutral.
    """
    names = list(names)
    failed = unavailable_skills(features)
    return {n for n in names if n.split(".", 1)[0] in failed} | not_applicable_features(features, names)


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


def _evidence(name: str, skill_findings: list[Finding]) -> str:
    # The finding whose type shares a word with the feature (listing.discount_pct ↔ "discount"); otherwise none.
    # Never the raw value: clients show the flag's human label (unfret.shopper.label_flags) instead.
    words = set(name.split(".", 1)[-1].split("_"))
    for f in skill_findings:
        if words & set(f.type.split("_")):
            return f.evidence
    return ""


def score(
    features: dict[str, float], findings: list[Finding] | None = None, model: Model | None = None
) -> Assessment | None:
    """
    Score a page from its skill features.

    ``confidence`` combines how decisive the probability is (``|2p - 1|``) with how many of the model's features
    were measured on this page (a page where most skills found nothing to measure is scored, but less surely).
    ``flags`` are the measured features that pushed the score towards unreliable, strongest first, each with the
    matching skill's finding as evidence where there is one. Features absent from ``features`` are never flagged.

    :param features: The skill features from the graph state.
    :param findings: The skill findings, used as evidence for the flags.
    :param model: The model to use; the shipped one by default.
    :return: The assessment, or ``None`` when no model is available.
    """
    model = model or load_model()
    if model is None:
        return None
    risk = model.probability(features)
    failed = unavailable_skills(features)
    # A feature counts as measured when its skill measured it, or when it doesn't apply to the page.
    settled = not_applicable_features(features, model.features)
    measured = sum(
        1 for name in model.features if (name in features and name.split(".", 1)[0] not in failed) or name in settled
    ) / len(model.features)
    confidence = abs(2 * risk - 1) * (0.5 + 0.5 * measured)
    degraded = bool(failed & KEY_SKILLS)
    if degraded:
        confidence = min(confidence, 0.3)

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
        if not features.get(name):
            # Not measured, or absent (0): the model may lean on an absence, but "Countdown timer" on a page without
            # one reads as an accusation. Absences that matter surface as plain reasons (unfret.shopper) instead.
            continue
        evidence = _evidence(name, by_skill.get(name.split(".", 1)[0], []))
        flags.append(Flag(type=name, evidence=evidence, weight=round(contribution, 3)))
    return Assessment(
        risk_score=round(risk, 4),
        confidence=round(confidence, 4),
        flags=flags,
        model_version=model.version,
        degraded=degraded,
    )
