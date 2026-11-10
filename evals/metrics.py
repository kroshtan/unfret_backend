"""
Scoring the pipeline against corpus labels.

The task is binary: ``junk`` and ``scam`` are *unreliable* (the positive class), ``legit`` is negative. The
pipeline's reliability score runs the other way (0 = scam, 100 = legitimate), so a page is predicted unreliable
when its score is below a threshold. Pages the pipeline declined to analyse (score ``-1``) or failed on are
reported separately rather than counted as either class.
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import mean, median

from evals.corpus import UNRELIABLE

THRESHOLDS = (30, 40, 50)


@dataclass(frozen=True)
class Outcome:
    """The pipeline's result for one labelled page."""

    id: str
    label: str
    score: int | None
    expected_page_type: str = ""
    predicted_page_type: str = ""
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""
    note: str = ""

    @property
    def scored(self) -> bool:
        """Whether the pipeline produced a 0-100 score."""
        return self.score is not None and self.score >= 0 and not self.error


@dataclass(frozen=True)
class BinaryMetrics:
    """Precision, recall and F1 for the unreliable class at one threshold."""

    threshold: int
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        """Share of pages flagged unreliable that are unreliable."""
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        """Share of unreliable pages that were flagged."""
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall."""
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def binary_metrics(outcomes: Sequence[Outcome], threshold: int) -> BinaryMetrics:
    """
    Confusion counts for "score below ``threshold`` means unreliable", over scored pages only.

    :param outcomes: Pipeline results.
    :param threshold: Scores strictly below this are predicted unreliable.
    :return: The metrics.
    """
    tp = fp = fn = tn = 0
    for o in outcomes:
        if not o.scored:
            continue
        assert o.score is not None
        predicted = o.score < threshold
        actual = o.label in UNRELIABLE
        tp += predicted and actual
        fp += predicted and not actual
        fn += actual and not predicted
        tn += not predicted and not actual
    return BinaryMetrics(threshold, tp, fp, fn, tn)


def roc_auc(outcomes: Sequence[Outcome]) -> float | None:
    """
    Area under the ROC curve for separating unreliable from legit pages by score.

    Computed as the probability that a random unreliable page scores lower than a random legit page (ties count
    half), i.e. the Mann-Whitney U statistic.

    :param outcomes: Pipeline results.
    :return: The AUC, or ``None`` when either class has no scored pages.
    """
    positives = [o.score for o in outcomes if o.scored and o.label in UNRELIABLE]
    negatives = [o.score for o in outcomes if o.scored and o.label not in UNRELIABLE]
    if not positives or not negatives:
        return None
    wins = 0.0
    for p in positives:
        for n in negatives:
            assert p is not None and n is not None
            wins += 1.0 if p < n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def score_distributions(outcomes: Sequence[Outcome]) -> dict[str, dict[str, float]]:
    """
    Summary statistics of the score per true label.

    :param outcomes: Pipeline results.
    :return: Per label: count, mean, median, 10th and 90th percentile.
    """
    by_label: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        if o.scored:
            assert o.score is not None
            by_label[o.label].append(float(o.score))
    return {
        label: {
            "n": len(scores),
            "mean": round(mean(scores), 1),
            "median": median(scores),
            "p10": _percentile(scores, 0.1),
            "p90": _percentile(scores, 0.9),
        }
        for label, scores in sorted(by_label.items())
    }


def page_type_confusion(outcomes: Sequence[Outcome]) -> dict[str, dict[str, int]]:
    """
    How often each expected page type was classified as each predicted type.

    :param outcomes: Pipeline results.
    :return: ``{expected: {predicted: count}}``.
    """
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for o in outcomes:
        if o.expected_page_type and not o.error:
            confusion[o.expected_page_type][o.predicted_page_type or "?"] += 1
    return {expected: dict(counts) for expected, counts in sorted(confusion.items())}


def summarise(outcomes: Sequence[Outcome]) -> dict[str, object]:
    """
    Every metric in one JSON-serialisable report.

    :param outcomes: Pipeline results.
    :return: The report.
    """
    scored = [o for o in outcomes if o.scored]
    latencies = [o.latency_s for o in outcomes if not o.error]
    return {
        "pages": len(outcomes),
        "scored": len(scored),
        "not_analysed": sum(1 for o in outcomes if not o.error and o.score == -1),
        "failures": [{"id": o.id, "error": o.error} for o in outcomes if o.error],
        "labels": dict(Counter(o.label for o in outcomes)),
        "roc_auc": roc_auc(outcomes),
        "thresholds": [
            {
                "threshold": m.threshold,
                "precision": round(m.precision, 3),
                "recall": round(m.recall, 3),
                "f1": round(m.f1, 3),
                "tp": m.tp,
                "fp": m.fp,
                "fn": m.fn,
                "tn": m.tn,
            }
            for m in (binary_metrics(outcomes, t) for t in THRESHOLDS)
        ],
        "score_distributions": score_distributions(outcomes),
        "page_type_confusion": page_type_confusion(outcomes),
        "latency_s": {
            "mean": round(mean(latencies), 2) if latencies else None,
            "p90": round(_percentile(latencies, 0.9), 2) if latencies else None,
        },
        "tokens": {
            "input": sum(o.input_tokens for o in outcomes),
            "output": sum(o.output_tokens for o in outcomes),
            "input_per_page": round(mean(o.input_tokens for o in outcomes), 0) if outcomes else 0,
            "output_per_page": round(mean(o.output_tokens for o in outcomes), 0) if outcomes else 0,
        },
    }


def to_markdown(report: dict[str, object], title: str = "Unfret evaluation") -> str:
    """
    Render :func:`summarise` output as a Markdown report.

    :param report: The summary.
    :param title: The report heading.
    :return: Markdown text.
    """
    lines = [f"# {title}", ""]
    auc = report["roc_auc"]
    lines.append(
        f"Pages: {report['pages']} · scored: {report['scored']} · not analysed: {report['not_analysed']} · "
        f"failures: {len(report['failures'])}  "  # type: ignore[arg-type]
    )
    lines.append(f"Labels: {report['labels']} · ROC AUC: {auc if auc is None else round(auc, 3)}")  # type: ignore[call-overload]
    lines += ["", "| threshold | precision | recall | F1 | TP | FP | FN | TN |", "|---|---|---|---|---|---|---|---|"]
    for t in report["thresholds"]:  # type: ignore[attr-defined]
        lines.append(
            f"| <{t['threshold']} | {t['precision']} | {t['recall']} | {t['f1']} | "
            f"{t['tp']} | {t['fp']} | {t['fn']} | {t['tn']} |"
        )
    lines += ["", "## Score per label", "", "| label | n | mean | median | p10 | p90 |", "|---|---|---|---|---|---|"]
    for label, d in report["score_distributions"].items():  # type: ignore[attr-defined]
        lines.append(f"| {label} | {d['n']} | {d['mean']} | {d['median']} | {d['p10']} | {d['p90']} |")
    lines += ["", "## Page type (expected → predicted)", ""]
    for expected, counts in report["page_type_confusion"].items():  # type: ignore[attr-defined]
        lines.append(f"- {expected}: {counts}")
    lines += ["", "## Cost and latency", "", f"- Latency: {report['latency_s']}", f"- Tokens: {report['tokens']}"]
    failures = report["failures"]
    if failures:
        lines += ["", "## Failures", ""]
        lines += [f"- {f['id']}: {f['error']}" for f in failures[:50]]  # type: ignore[index]
    return "\n".join(lines) + "\n"
