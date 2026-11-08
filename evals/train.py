"""
Train the feature scorer on the corpus TRAIN split and evaluate it on TEST.

Usage::

    uv run python -m evals.train [--features <run>-features.jsonl] [--ship A|B|C|none]

Features come from an evaluation run (``python -m evals.run --split all`` writes ``<stamp>-features.jsonl``).
Three variants, because the marketplace labels were proposed by rules over listing statistics that some skills
measure too:

- **A**: every feature, every training label.
- **B**: every feature, without labels that rest on the rules alone (``label_source == "rule"``). Shipped by default.
- **C**: B without the feature columns that mirror the rule features, an estimate of how much the score leans on
  re-learning the rules.

``--quick`` trains on the quick tier's skills only and ships ``model-quick.json``. Needed only once the full model
uses features the quick tier doesn't compute (e.g. network lookups); until then the quick tier falls back to the
full model, which ``tests/unit/test_x402_verdict.py`` checks.

Each is a standardised logistic regression with its regularisation chosen by cross-validation on TRAIN, calibrated
with isotonic regression on cross-validated TRAIN predictions, and scored once on TEST. A gradient-boosted model is
reported for comparison only. The shipped artifact goes to ``src/veridex/scoring/model.json``; the report to
``<corpus>/reports/scorer-<stamp>.md``.
"""

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegressionCV
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict

from evals.corpus import Corpus, LabelRecord, dump_json
from evals.metrics import Outcome, binary_metrics, roc_auc, score_distributions
from veridex.graph import SKILLS
from veridex.scoring import MODEL_PATH, MODEL_QUICK_PATH, Model
from veridex.x402.verdict import QUICK_SKILLS

UNRELIABLE = {"junk", "scam"}
# Skill features that mirror the weak-labelling rules (evals/sources/amazon2023.py), for variant C.
RULE_MIRRORS = {
    "duplicate_share": ["reviews.duplicate_share"],
    "early_burst_180d": ["reviews.burst_share", "reviews.log_reviews_per_day"],
    "generated_brand_score": ["listing.generated_brand"],
    "j_shaped": ["reviews.j_shaped", "reviews.share_one", "reviews.share_five", "reviews.share_middle"],
    "keyword_stuffed": ["listing.keyword_stuffed", "listing.title_length"],
    "known_brand": ["content.brand_recognized"],
    "rating_number": ["listing.log_rating_count"],
}
# Not available offline (network lookups are off in evaluation), so not part of the model.
EXCLUDED_SKILLS = {"reverse_image", "domain_age"}
THRESHOLDS = (30, 40, 50)


@dataclass
class Dataset:
    """Feature matrix, labels and record metadata for one split."""

    records: list[LabelRecord]
    x: np.ndarray
    y: np.ndarray


def feature_names() -> list[str]:
    """
    The model's feature columns: every feature the skills declare, minus skills that can't run offline.

    :return: Prefixed feature names, in registry order.
    """
    return [f"{s.name}.{f}" for s in SKILLS if s.name not in EXCLUDED_SKILLS for f in s.features]


def load_features(path: Path) -> dict[str, dict[str, float]]:
    """
    Read a run's per-page features.

    :param path: A ``*-features.jsonl`` file.
    :return: Features by record id.
    """
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {r["id"]: r["features"] for r in rows}


def build(records: Sequence[LabelRecord], features: dict[str, dict[str, float]], names: list[str]) -> Dataset:
    """
    Turn records with features into a matrix (missing features are 0).

    :param records: Labelled records.
    :param features: Features by record id; records without features are skipped.
    :param names: Feature columns.
    :return: The dataset.
    """
    kept = [r for r in records if r.id in features]
    x = np.array([[features[r.id].get(n, 0.0) for n in names] for r in kept], dtype=float)
    y = np.array([1 if r.label in UNRELIABLE else 0 for r in kept], dtype=int)
    return Dataset(kept, x.reshape(len(kept), len(names)), y)


def fit(train: Dataset, names: list[str], version: str) -> Model:
    """
    Fit a standardised, cross-validated, isotonic-calibrated logistic regression.

    :param train: The training data.
    :param names: Its feature columns.
    :param version: Version string for the artifact.
    :return: The model.
    """
    means = train.x.mean(axis=0)
    scales = train.x.std(axis=0)
    scales[scales == 0] = 1.0
    z = (train.x - means) / scales
    folds = StratifiedKFold(n_splits=5, shuffle=True, random_state=7)
    lr = LogisticRegressionCV(Cs=12, cv=folds, scoring="roc_auc", max_iter=5_000, class_weight="balanced")
    lr.fit(z, train.y)
    raw = cross_val_predict(
        LogisticRegressionCV(Cs=[float(lr.C_[0])], cv=3, max_iter=5_000, class_weight="balanced"),
        z,
        train.y,
        cv=folds,
        method="predict_proba",
    )[:, 1]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, train.y)
    points = sorted(zip(iso.X_thresholds_.tolist(), iso.y_thresholds_.tolist(), strict=True))
    return Model(
        version=version,
        features=names,
        means=means.tolist(),
        scales=scales.tolist(),
        coefficients=lr.coef_[0].tolist(),
        intercept=float(lr.intercept_[0]),
        calibration=[(round(a, 6), round(b, 6)) for a, b in points],
    )


def outcomes(model: Model, data: Dataset, names: list[str]) -> list[Outcome]:
    """
    Score a split with the model, as evaluation outcomes (score = 100 × (1 − risk), as the API reports it).

    :param model: The model.
    :param data: The split.
    :param names: The model's feature columns.
    :return: One outcome per record.
    """
    result: list[Outcome] = []
    for record, row in zip(data.records, data.x, strict=True):
        risk = model.probability(dict(zip(names, row.tolist(), strict=True)))
        assert record.label is not None
        result.append(Outcome(id=record.id, label=record.label, score=round(100 * (1 - risk))))
    return result


def evaluate(model: Model, test: Dataset, names: list[str]) -> dict[str, Any]:
    """
    Metrics on TEST: AUC (all, marketplace, storefront), P/R/F1 at the score thresholds, Brier, per-label scores.

    :param model: The model.
    :param test: The test split.
    :param names: The model's feature columns.
    :return: The metrics.
    """
    scored = outcomes(model, test, names)
    risks = [1 - o.score / 100 for o in scored if o.score is not None]
    by_source = {
        "marketplace": [o for o, r in zip(scored, test.records, strict=True) if r.page_type == "aggregate_listing"],
        "storefront": [o for o, r in zip(scored, test.records, strict=True) if r.page_type != "aggregate_listing"],
    }
    return {
        "pages": len(scored),
        "roc_auc": roc_auc(scored),
        "roc_auc_by_page_type": {k: roc_auc(v) for k, v in by_source.items()},
        "brier": round(float(brier_score_loss(test.y, risks)), 4),
        "thresholds": [
            {"threshold": m.threshold, "precision": m.precision, "recall": m.recall, "f1": m.f1}
            for m in (binary_metrics(scored, t) for t in THRESHOLDS)
        ],
        "scores": score_distributions(scored),
    }


def judge_baseline(corpus: Corpus, test_ids: set[str], features_path: Path) -> dict[str, Any] | None:
    """
    The LLM judge's metrics on TEST from the same evaluation run, for comparison.

    :param corpus: The corpus.
    :param test_ids: Ids of the TEST records used.
    :param features_path: The run's features file (its outcomes file sits next to it).
    :return: The judge's metrics, or ``None`` when the outcomes file is missing.
    """
    path = features_path.with_name(features_path.name.replace("-features.jsonl", "-outcomes.jsonl"))
    if not path.exists():
        return None
    rows = [Outcome(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    test = [o for o in rows if o.id in test_ids and o.scored]
    return {
        "pages": len(test),
        "roc_auc": roc_auc(test),
        "thresholds": [
            {"threshold": m.threshold, "precision": m.precision, "recall": m.recall, "f1": m.f1}
            for m in (binary_metrics(test, t) for t in THRESHOLDS)
        ],
        "scores": score_distributions(test),
        "latency_mean_s": round(sum(o.latency_s for o in test) / len(test), 2) if test else None,
        "tokens_per_page": {
            "input": round(sum(o.input_tokens for o in test) / len(test)) if test else None,
            "output": round(sum(o.output_tokens for o in test) / len(test)) if test else None,
        },
    }


def gbm_auc(train: Dataset, test: Dataset) -> float:
    """
    TEST AUC of a gradient-boosted model on the same features, for comparison only.

    :param train: The training data.
    :param test: The test data.
    :return: The AUC.
    """
    gbm = HistGradientBoostingClassifier(max_depth=3, max_iter=200, learning_rate=0.05, random_state=7)
    gbm.fit(train.x, train.y)
    return round(float(roc_auc_score(test.y, gbm.predict_proba(test.x)[:, 1])), 4)


def top_coefficients(model: Model, n: int = 12) -> list[tuple[str, float]]:
    """
    The largest coefficients by magnitude (on standardised features).

    :param model: The model.
    :param n: How many.
    :return: ``(feature, coefficient)`` pairs.
    """
    pairs = sorted(zip(model.features, model.coefficients, strict=True), key=lambda t: -abs(t[1]))
    return [(name, round(c, 3)) for name, c in pairs[:n]]


def to_markdown(report: dict[str, Any]) -> str:
    """
    Render the training report.

    :param report: The report dict.
    :return: Markdown.
    """
    lines = [f"# Veridex scorer {report['stamp']}", "", f"Features: `{report['features_file']}`", ""]
    lines += [
        "| model | AUC | AUC marketplace | AUC storefront | F1 <30 | F1 <40 | F1 <50 | Brier |",
        "|---" * 8 + "|",
    ]

    def row(name: str, m: dict[str, Any]) -> str:
        by_type = m.get("roc_auc_by_page_type", {})
        f1 = {t["threshold"]: t["f1"] for t in m["thresholds"]}
        return (
            f"| {name} | {m['roc_auc']} | {by_type.get('marketplace', '–')} | {by_type.get('storefront', '–')} | "
            f"{f1[30]} | {f1[40]} | {f1[50]} | {m.get('brier', '–')} |"
        )

    if report.get("judge"):
        lines.append(row("LLM judge (same run)", report["judge"]))
    for name, variant in report["variants"].items():
        lines.append(row(f"scorer {name}", variant["test"]))
    lines += ["", "## Per-label scores on TEST (median)", ""]
    for name, variant in report["variants"].items():
        medians = {k: v["median"] for k, v in variant["test"]["scores"].items()}
        lines.append(f"- scorer {name}: {medians}")
    if report.get("judge"):
        lines.append(f"- LLM judge: { {k: v['median'] for k, v in report['judge']['scores'].items()} }")
    lines += ["", "## Variants", ""]
    for name, variant in report["variants"].items():
        lines.append(
            f"- **{name}**: {variant['description']}; train n={variant['train_n']}, "
            f"GBM AUC {variant['gbm_auc']}; top coefficients {variant['top']}"
        )
    lines += ["", f"Shipped: {report['shipped']}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--features", type=Path, default=None, help="a *-features.jsonl from evals.run")
    parser.add_argument("--ship", choices=["A", "B", "C", "none"], default="B")
    parser.add_argument(
        "--quick", action="store_true", help="train on the quick tier's skills only; ship model-quick.json"
    )
    args = parser.parse_args()

    corpus = Corpus()
    reports = corpus.root / "reports"
    path = args.features or max(reports.glob("*-features.jsonl"), key=lambda p: p.stat().st_mtime)
    features = load_features(path)
    labelled = [r for r in corpus.labels().values() if r.label and r.split in {"train", "test"}]
    names = feature_names()
    if args.quick:
        names = [n for n in names if n.split(".", 1)[0] in QUICK_SKILLS]
    rule_columns = {c for cols in RULE_MIRRORS.values() for c in cols}
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    tag = "quick-" if args.quick else ""

    variants = {
        "A": ("all features, all training labels", names, lambda r: True),
        "B": ("all features, no rule-only labels", names, lambda r: r.label_source != "rule"),
        "C": (
            "no rule-mirroring features, no rule-only labels",
            [n for n in names if n not in rule_columns],
            lambda r: r.label_source != "rule",
        ),
    }
    test_records = [r for r in labelled if r.split == "test"]
    report: dict[str, Any] = {"stamp": stamp, "features_file": path.name, "variants": {}}
    models: dict[str, Model] = {}
    for key, (description, cols, keep) in variants.items():
        train = build([r for r in labelled if r.split == "train" and keep(r)], features, cols)
        test = build(test_records, features, cols)
        model = fit(train, cols, f"v1-{tag}{stamp}-{key}")
        models[key] = model
        report["variants"][key] = {
            "description": description,
            "train_n": len(train.records),
            "test": evaluate(model, test, cols),
            "gbm_auc": gbm_auc(train, test),
            "top": top_coefficients(model),
        }
    test_ids = {r.id for r in build(test_records, features, names).records}
    report["judge"] = judge_baseline(corpus, test_ids, path)
    report["shipped"] = "none" if args.ship == "none" else models[args.ship].version

    if args.ship != "none":
        m = models[args.ship]
        artifact = {
            "version": m.version,
            "features": m.features,
            "means": m.means,
            "scales": m.scales,
            "coefficients": m.coefficients,
            "intercept": m.intercept,
            "calibration": m.calibration,
            "trained_on": {"features_file": path.name, "train_n": report["variants"][args.ship]["train_n"]},
            "test": {k: report["variants"][args.ship]["test"][k] for k in ("roc_auc", "brier", "thresholds")},
        }
        (MODEL_QUICK_PATH if args.quick else MODEL_PATH).write_text(json.dumps(artifact, indent=1) + "\n")
    dump_json(reports / f"scorer-{tag}{stamp}.json", report)
    markdown = to_markdown(report)
    (reports / f"scorer-{tag}{stamp}.md").write_text(markdown)
    print(markdown)  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
