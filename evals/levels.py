"""
Risk levels on the corpus: the level classifier's training (ordinal), and the level judge against the labels.

**Labels.** From the level batches on a page's label is its OVERALL level (``level`` in ``labels.jsonl``, higher is
worse: 0 none, 1 pressure and tricks, 2 overpaying, 3 doesn't work, 4 may not arrive / unsafe / data theft), not its
most serious single finding. Older labels (legit / junk / scam) only bound it (:data:`evals.corpus.LEGACY_LEVELS`).

**The classifier (ordinal).** The classifier is four of the scorer's own sign-constrained, calibrated logistic
regressions (:func:`evals.train.fit`), one per threshold ``P(level >= k)``, k = 1..4, each trained on the pages whose
labels settle that threshold: a level label settles all four, an old label only some (scam all; junk "any concern"
yes and "won't arrive" no; legit "doesn't work" and "won't arrive" no). The level is the highest whose threshold
probability passes 0.5, after making the four monotone.
``train`` refuses with fewer than ``--min-level-labels`` level labels (no retrain before the labels exist);
``--dry-run`` prints what each threshold would train on. A trained classifier goes to the private model store, never
``src/``.

**The judge.** ``compare`` runs the level judge (:mod:`unfret.level_judge`) on the level-labelled pages that have a
deep-check result, with the other labelled pages as its examples (leave-one-out), and compares it and the band's
translation (:func:`unfret.levels.from_band`) with the labels: exact agreement, within one level, and "any concern".
``--pin-disagreements N`` also runs it on the next N unlabelled queue pages and pins those where it disagrees with
the scorer's band translation to the front of the label queue (Floris's plan: judge–classifier disagreements first).
The judge's calls are paid: the run stops before ``--budget-eur``.

Usage: ``uv run python -m evals.levels train --dry-run``, ``... compare --budget-eur 5``.
"""

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np

from evals import candidates as cand
from evals.corpus import Corpus, LabelRecord, dump_json, is_product, level_bounds
from evals.train import Dataset, feature_names, fit, load_features
from unfret import level_judge, levels, shopper
from unfret.config import settings
from unfret.facts import parse_page
from unfret.graph.state import Finding
from unfret.html_cleaner import clean_html
from unfret.market import shop_market
from unfret.scoring import Model

THRESHOLDS = (1, 2, 3, 4)
MIN_LEVEL_LABELS = 150


@dataclass
class Threshold:
    """One threshold's training data: which pages settle ``level >= k``, and how."""

    k: int
    records: list[LabelRecord]
    y: list[int]


def thresholds(records: Sequence[LabelRecord]) -> list[Threshold]:
    """
    Per threshold, the pages whose labels settle it.

    :param records: Labelled records.
    :return: One :class:`Threshold` per k in :data:`THRESHOLDS`.
    """
    out = []
    for k in THRESHOLDS:
        kept: list[LabelRecord] = []
        y: list[int] = []
        for r in records:
            bounds = level_bounds(r)
            if bounds is None:
                continue
            low, high = bounds
            if low >= k:
                kept.append(r)
                y.append(1)
            elif high < k:
                kept.append(r)
                y.append(0)
        out.append(Threshold(k, kept, y))
    return out


def predict_from_thresholds(probabilities: Sequence[float]) -> int:
    """
    The level from the four threshold probabilities, made monotone (``P(level >= k)`` can't grow with k).

    :param probabilities: ``P(level >= k)`` for k = 1..4.
    :return: 0–4.
    """
    monotone = np.minimum.accumulate(np.asarray(probabilities, dtype=float))
    return int((monotone >= 0.5).sum())


def predict_level(models: Sequence[Model], features: dict[str, float]) -> int:
    """
    The level a trained ordinal classifier gives a page.

    :param models: The four threshold models, k = 1..4.
    :param features: The page's features.
    :return: 0–4.
    """
    from unfret import scoring  # noqa: PLC0415

    probabilities = []
    for model in models:
        assessment = scoring.score(features, [], model)
        probabilities.append(assessment.risk_score if assessment is not None else 0.0)
    return predict_from_thresholds(probabilities)


def _matrix(records: list[LabelRecord], features: dict[str, dict[str, float]], names: list[str]) -> np.ndarray:
    from evals.train import _value  # noqa: PLC0415 - the scorer's own missing-value rule

    return np.array([[_value(features[r.id], n) for n in names] for r in records], dtype=float).reshape(
        len(records), len(names)
    )


def train(corpus: Corpus, *, features_path: Any, min_level_labels: int, dry_run: bool) -> dict[str, Any]:
    """
    Train (or with ``dry_run`` only count) the ordinal level classifier.

    :param corpus: The corpus.
    :param features_path: A ``*-features.jsonl`` (None: the newest in ``reports/``).
    :param min_level_labels: The fewest level labels to train on.
    :param dry_run: Only report what each threshold would train on.
    :return: The report.
    :raises SystemExit: With too few level labels and not a dry run.
    """
    labelled = [r for r in corpus.labels().values() if is_product(r) and level_bounds(r) is not None]
    with_level = [r for r in labelled if r.level is not None]
    report: dict[str, Any] = {
        "level_labels": len(with_level),
        "levels": dict(Counter(str(r.level) for r in with_level)),
        "thresholds": {},
    }
    split = [r for r in labelled if r.split == "train"]
    for t in thresholds(split):
        report["thresholds"][f"level>={t.k}"] = {"pages": len(t.records), "positive": sum(t.y)}
    if dry_run:
        return report
    if len(with_level) < min_level_labels:
        raise SystemExit(f"{len(with_level)} level labels, fewer than {min_level_labels}: label first, then train.")
    reports = corpus.root / "reports"
    path = features_path or max(reports.glob("*-features.jsonl"), key=lambda p: p.stat().st_mtime)
    features = load_features(path)
    names = feature_names()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    models: dict[str, Any] = {}
    for t in thresholds([r for r in split if r.id in features]):
        records = [r for r in t.records if r.id in features]
        y = np.array([y for r, y in zip(t.records, t.y, strict=True) if r.id in features], dtype=int)
        model = fit(Dataset(records, _matrix(records, features, names), y), names, f"levels-{stamp}-k{t.k}")
        models[str(t.k)] = model.__dict__
    out = corpus.root / "models" / f"levels-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "levels.json").write_text(json.dumps({"version": f"levels-{stamp}", "thresholds": models}, indent=1))
    report["artifact"] = str(out / "levels.json")
    return report


# ── The judge against the labels ──────────────────────────────────────────────────────────────────────────────


def page_inputs(corpus: Corpus, record: LabelRecord, row: dict[str, Any]) -> tuple[levels.Risk, dict[str, Any]]:
    """
    A labelled page's band level and the judge's inputs, rebuilt from its deep-check result.

    :param corpus: The corpus.
    :param record: The page.
    :param row: Its deep-check result (``reports/deep-findings/results.jsonl``).
    :return: The band's risk and the judge's keyword arguments.
    """
    html = corpus.read_page(record.id)
    facts, _ = parse_page(html, record.origin_url)
    text = clean_html(html)
    findings = [Finding.model_validate(f) for f in row.get("findings", [])]
    page_type = str(row.get("page_type") or "storefront")
    fired = shopper.fired_reasons(
        features=row.get("features", {}),
        findings=findings,
        facts=facts,
        page_type=page_type,
        market=shop_market(facts, record.origin_url, text),
    )
    band_risk = levels.from_band(str(row.get("band") or "reliable"), sorted(fired, key=levels.order_key))
    if band_risk.level == levels.NONE:
        because = [levels.LevelReason(type=t, level=levels.level_of(t), title=levels.title_of(t)) for t in fired]
    else:
        because = band_risk.because
    inputs = {
        "page_text": text,
        "reasons": [{"type": b.type, "title": b.title, "level": b.level, "detail": ""} for b in because],
        "positives": [],
        "findings": [f.model_dump() for f in findings],
        "classifier": (
            {"risk": float(row["risk"]), "confidence": float(row.get("confidence") or 0.0)}
            if row.get("risk") is not None
            else None
        ),
    }
    return band_risk, inputs


def examples_for(
    corpus: Corpus, labelled: Sequence[LabelRecord], inputs: dict[str, dict[str, Any]], leave_out: str
) -> list[level_judge.Example]:
    """
    The judge's examples: labelled pages other than the one being judged, one per level first.

    :param corpus: The corpus.
    :param labelled: Level-labelled pages.
    :param inputs: Their judge inputs, by id.
    :param leave_out: The page being judged.
    :return: Up to :data:`level_judge.MAX_EXAMPLES` examples.
    """
    by_level: dict[int, list[LabelRecord]] = {}
    for r in labelled:
        if r.id != leave_out and r.id in inputs and r.level is not None:
            by_level.setdefault(r.level, []).append(r)
    picked: list[LabelRecord] = []
    while len(picked) < level_judge.MAX_EXAMPLES and any(by_level.values()):
        for lv in sorted(by_level):
            if by_level[lv] and len(picked) < level_judge.MAX_EXAMPLES:
                picked.append(by_level[lv].pop(0))
    out = []
    for r in picked:
        i = inputs[r.id]
        risk = i["classifier"]["risk"] if i["classifier"] else None
        summary = parse_page(corpus.read_page(r.id), r.origin_url)[0].title[:120] or r.id
        out.append(
            level_judge.Example(
                summary=summary, reasons=[x["type"] for x in i["reasons"]], classifier_risk=risk, level=r.level or 0
            )
        )
    return out


def agreement(pairs: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """
    How a set of answers agrees with the labels.

    :param pairs: ``(label, answer)`` levels.
    :return: Pages, exact, within one level, any-concern agreement, and the confusion (label → answer → count).
    """
    n = len(pairs)
    if not n:
        return {"pages": 0}
    exact = sum(a == b for a, b in pairs)
    near = sum(abs(a - b) <= 1 for a, b in pairs)
    concern = sum((a == 0) == (b == 0) for a, b in pairs)
    confusion: dict[str, Counter[str]] = {}
    for a, b in pairs:
        confusion.setdefault(str(a), Counter())[str(b)] += 1
    return {
        "pages": n,
        "exact": round(exact / n, 3),
        "within_one": round(near / n, 3),
        "any_concern": round(concern / n, 3),
        "confusion": {k: dict(v) for k, v in confusion.items()},
    }


def _cost(tokens: dict[str, int]) -> Decimal:
    million = Decimal(1_000_000)
    return (
        Decimal(tokens.get("input_tokens", 0)) * settings.claim_audit_input_eur_per_mtok
        + Decimal(tokens.get("output_tokens", 0)) * settings.claim_audit_output_eur_per_mtok
    ) / million


async def compare(corpus: Corpus, *, budget: Decimal, pin_disagreements: int) -> dict[str, Any]:
    """
    The level judge and the band's translation against the level labels (see the module docstring).

    :param corpus: The corpus.
    :param budget: Stop the judge's calls before this many euros.
    :param pin_disagreements: Also judge this many unlabelled queue pages and pin the disagreements.
    :return: The report.
    """
    rows = cand.label_rows(corpus)
    known = corpus.labels()
    labelled = [r for r in known.values() if r.level is not None and r.id in rows]
    built = {r.id: page_inputs(corpus, r, rows[r.id]) for r in labelled}
    inputs = {k: v[1] for k, v in built.items()}
    spent = Decimal(0)
    band_pairs: list[tuple[int, int]] = []
    judge_pairs: list[tuple[int, int]] = []
    answers: list[dict[str, Any]] = []
    for r in labelled:
        assert r.level is not None
        band_pairs.append((r.level, built[r.id][0].level))
        if spent >= budget:
            continue
        risk, answer, tokens = await level_judge.judge(
            **inputs[r.id], examples=examples_for(corpus, labelled, inputs, r.id)
        )
        spent += _cost(tokens)
        judge_pairs.append((r.level, risk.level))
        answers.append({"id": r.id, "label": r.level, "band": built[r.id][0].level, "judge": answer.model_dump()})
    pinned: list[str] = []
    if pin_disagreements:
        queue = [i for i in cand.read_queue(corpus) if i.id in rows and known.get(i.id) and known[i.id].level is None]
        for item in queue[:pin_disagreements]:
            if spent >= budget:
                break
            band_risk, page = page_inputs(corpus, known[item.id], rows[item.id])
            risk, _, tokens = await level_judge.judge(**page, examples=examples_for(corpus, labelled, inputs, item.id))
            spent += _cost(tokens)
            if risk.level != band_risk.level:
                pinned.append(item.id)
        if pinned:
            cand.pin(corpus, pinned, "the level judge and the scorer disagree on the level")
    report = {
        "prompt": level_judge.PROMPT_VERSION,
        "model": settings.claim_audit_model,
        "band": agreement(band_pairs),
        "judge": agreement(judge_pairs),
        "pinned": len(pinned),
        "spent_eur": str(round(spent, 4)),
    }
    out = corpus.root / "reports" / "levels"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dump_json(out / f"compare-{stamp}.json", report | {"answers": answers})
    return report


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("train", help="train the ordinal level classifier (refuses without enough level labels)")
    tr.add_argument("--features", default=None)
    tr.add_argument("--min-level-labels", type=int, default=MIN_LEVEL_LABELS)
    tr.add_argument("--dry-run", action="store_true", help="only count what each threshold would train on")
    cmp = sub.add_parser("compare", help="the level judge and the band's level against the level labels")
    cmp.add_argument("--budget-eur", type=Decimal, default=Decimal(5))
    cmp.add_argument("--pin-disagreements", type=int, default=0)
    args = parser.parse_args()
    corpus = Corpus()
    if args.command == "train":
        report = train(
            corpus, features_path=args.features, min_level_labels=args.min_level_labels, dry_run=args.dry_run
        )
    else:
        report = asyncio.run(compare(corpus, budget=args.budget_eur, pin_disagreements=args.pin_disagreements))
    print(json.dumps(report, indent=1))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
