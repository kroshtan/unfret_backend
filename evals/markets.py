"""
How the shipped scorers do per market: legit pages warned about and unreliable pages caught, EU, US and elsewhere.

Usage::

    uv run python -m evals.markets [--features <run>-features.jsonl] [--split test|train|all] [--train]
        [--ship quick|full|both]

The skills that read only the page (``listing``, ``pricing`` without its price history, ``html_source_signals``,
``dark_patterns``, ``injection``, ``eu_registry`` without VIES, ``us_identity``, ``shop_policies``) are recomputed
with the current code on each page's saved HTML and merged into an earlier run's features; everything else (the
LLM-read ``content``, ``reviews``) is reused from that run, so this costs no LLM calls. Pages that were never part
of a run (added to the corpus since) have only the recomputed features: they are scored by the quick model, which
reads nothing else, and left out of the full model's numbers.

A page is *warned about* when the shopper verdict (:func:`unfret.shopper.build`, with the scorer's confidence and
the quick tier's ``concrete_only``) is "careful" or worse, as a shopper would see it. The verdict's reasons come from
the recomputed skills' findings; the run's ``content`` findings aren't stored, so its reasons that quote the page
(an implausible claim) can't keep a page at "careful" here, which makes the counts a slight underestimate of
warnings.

``--train`` fits candidates on the merged features (variant C, as shipped: no rule-only labels, no rule-mirroring
columns; calibrated to a base rate of ``--prior``) and reports them next to the shipped models; ``--ship`` writes a
candidate's artifact. The merged features go to ``<corpus>/reports/<stamp>-markets-features.jsonl`` (pages from the
run only, usable with ``evals.train``) and ``<stamp>-markets-quick-features.jsonl`` (every page, complete for the
quick model only), the report to ``markets-<stamp>.json`` and ``.md``. Groups: every market, and the EU split into
``eu_archived`` (the pages the corpus had) and ``eu_live`` (fetched live by :mod:`evals.sources.eu_shops`).
"""

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sklearn.metrics import brier_score_loss

from evals.corpus import UNRELIABLE, Corpus, LabelRecord, dump_json
from evals.metrics import Outcome, roc_auc
from evals.train import RULE_MIRRORS, build, feature_names, fit, load_features, write_artifact
from unfret import scoring, shopper
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.graph.skills import Skill
from unfret.graph.skills.dark_patterns import DarkPatternsSkill
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.graph.skills.html_source_signals import HtmlSourceSignalsSkill
from unfret.graph.skills.injection import InjectionSkill
from unfret.graph.skills.listing import ListingSkill
from unfret.graph.skills.pricing import PricingSkill
from unfret.graph.skills.shop_policies import ShopPoliciesSkill
from unfret.graph.skills.us_identity import UsIdentitySkill
from unfret.graph.state import Finding
from unfret.market import shop_market
from unfret.scoring import MODEL_PATH, MODEL_QUICK_PATH, Model
from unfret.x402.verdict import QUICK_SKILLS

RECOMPUTED: list[Skill] = [
    ListingSkill(),
    PricingSkill(history=False),
    HtmlSourceSignalsSkill(),
    DarkPatternsSkill(),
    InjectionSkill(),
    EuRegistrySkill(vies=False),
    UsIdentitySkill(),
    ShopPoliciesSkill(),
]
# "eu_live" are the pages fetched live (evals.sources.eu_shops), "eu_archived" the rest of the EU pages.
MARKETS = ("all", "eu", "eu_archived", "eu_live", "non_eu", "us", "other")
WARNED = frozenset({"careful", "junk_or_scam"})


@dataclass
class Page:
    """One labelled page with its merged features, as the scorer and the shopper verdict see it."""

    record: LabelRecord
    features: dict[str, float]
    facts: PageFacts
    market: str
    page_type: str
    from_run: bool = False
    findings: list[Finding] = field(default_factory=list)


async def recompute(corpus: Corpus, record: LabelRecord, old: dict[str, float] | None) -> Page:
    """
    Recompute the page-only skills for one page and merge them into its features from an earlier run.

    :param corpus: The corpus.
    :param record: The page's label record.
    :param old: Its features from an earlier run, or ``None`` when it wasn't part of one.
    :return: The page.
    """
    html = corpus.read_page(record.id)
    facts, text = await asyncio.to_thread(parse_page, html, record.origin_url)
    page_type = record.page_type or "storefront"
    if old is not None and "listing.is_marketplace" in old:
        page_type = "aggregate_listing" if old["listing.is_marketplace"] else "storefront"
    state: Any = {
        "page_content": html,
        "url": record.origin_url,
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": page_type,
    }
    names = {s.name for s in RECOMPUTED}
    merged = {k: v for k, v in (old or {}).items() if k.split(".", 1)[0] not in names}
    findings: list[Finding] = []
    for skill in RECOMPUTED:
        if page_type in skill.page_types:
            out = await skill.run(state)
            merged |= out["features"]
            findings += out["findings"]
    market = shop_market(facts, record.origin_url, text)
    return Page(record, merged, facts, market, page_type, old is not None, findings)


def in_market(page: Page, market: str) -> bool:
    """
    Whether a page belongs to a market group.

    :param page: The page.
    :param market: One of :data:`MARKETS` (``non_eu`` is US and other together).
    :return: ``True`` when it does.
    """
    if market in {"eu_archived", "eu_live"}:
        return page.market == "eu" and (page.record.source == "live") == (market == "eu_live")
    return market in {"all", page.market} or (market == "non_eu" and page.market != "eu")


def warned(page: Page, model: Model, *, quick: bool) -> tuple[float, bool]:
    """
    The model's risk for a page and whether a shopper would be warned ("careful" or worse, after the verdict's rules).

    :param page: The page.
    :param model: The scorer.
    :param quick: Whether this is the quick tier (it warns only on something concrete).
    :return: ``(risk, warned)``.
    """
    assessment = scoring.score(page.features, page.findings, model)
    assert assessment is not None
    verdict = shopper.build(
        round(100 * (1 - assessment.risk_score)),
        features=page.features,
        findings=page.findings,
        facts=page.facts,
        page_type=page.page_type,
        market=page.market,
        confidence=assessment.confidence,
        concrete_only=quick,
    )
    return assessment.risk_score, verdict.band in WARNED


def market_metrics(pages: Sequence[Page], model: Model, *, quick: bool) -> dict[str, Any]:
    """
    A scorer's metrics on some pages: AUC, Brier, legit pages warned about and unreliable pages caught.

    :param pages: The pages.
    :param model: The scorer.
    :param quick: Whether this is the quick tier.
    :return: The metrics.
    """
    scored = [(p, *warned(p, model, quick=quick)) for p in pages]
    legit = [w for p, _, w in scored if p.record.label not in UNRELIABLE]
    bad = [w for p, _, w in scored if p.record.label in UNRELIABLE]
    # The score alone, before the verdict's rules: below "concerns", i.e. "careful" or worse by the number.
    low = [(p.record.label in UNRELIABLE, round(100 * (1 - r)) < settings.verdict_concerns_min) for p, r, _ in scored]
    outcomes = [Outcome(id=p.record.id, label=str(p.record.label), score=round(100 * (1 - r))) for p, r, _ in scored]
    both = bool(legit) and bool(bad)
    truth = [int(p.record.label in UNRELIABLE) for p, _, _ in scored]
    return {
        "pages": len(scored),
        "roc_auc": round(auc, 4) if (auc := roc_auc(outcomes)) is not None else None,
        "brier": round(float(brier_score_loss(truth, [r for _, r, _ in scored])), 4) if both else None,
        "legit": len(legit),
        "legit_warned": sum(legit),
        "legit_warned_rate": round(sum(legit) / len(legit), 3) if legit else None,
        "unreliable": len(bad),
        "unreliable_caught": sum(bad),
        "recall": round(sum(bad) / len(bad), 3) if bad else None,
        "legit_low_score": sum(lo for is_bad, lo in low if not is_bad),
        "unreliable_low_score": sum(lo for is_bad, lo in low if is_bad),
    }


def by_market(pages: Sequence[Page], model: Model, *, quick: bool) -> dict[str, Any]:
    """
    :func:`market_metrics` for every group in :data:`MARKETS`.

    :param pages: The pages.
    :param model: The scorer.
    :param quick: Whether this is the quick tier.
    :return: Metrics keyed by market group.
    """
    return {m: market_metrics([p for p in pages if in_market(p, m)], model, quick=quick) for m in MARKETS}


def candidate(pages: Sequence[Page], *, quick: bool, prior: float, version: str) -> tuple[Model, int]:
    """
    Fit a variant-C candidate on the TRAIN pages (pages without an LLM run only for the quick model).

    :param pages: All labelled pages.
    :param quick: Train the quick tier's model.
    :param prior: The base rate the calibration assumes.
    :param version: Its version string.
    :return: The model and its number of training pages.
    """
    rule_columns = {c for cols in RULE_MIRRORS.values() for c in cols}
    names = [n for n in feature_names() if n not in rule_columns]
    if quick:
        names = [n for n in names if n.split(".", 1)[0] in QUICK_SKILLS]
    train = [
        p for p in pages if p.record.split == "train" and p.record.label_source != "rule" and (quick or p.from_run)
    ]
    data = build([p.record for p in train], {p.record.id: p.features for p in train}, names)
    return fit(data, names, version, prior), len(data.records)


def to_markdown(report: dict[str, Any]) -> str:
    """
    Render the report's tables.

    :param report: The report dict.
    :return: Markdown.
    """
    lines = [f"# Unfret per-market evaluation {report['stamp']} ({report['split']})", ""]
    lines += [f"Features: `{report['features_from']}`; pages by market: {report['pages_by_market']}", ""]
    lines += [
        "| model | market | pages | AUC | Brier | legit warned | unreliable caught | legit / unreliable < 50 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, result in report["models"].items():
        for market, m in result["metrics"].items():
            warned_cell = f"{m['legit_warned']}/{m['legit']} ({m['legit_warned_rate']})" if m["legit"] else "–"
            caught_cell = f"{m['unreliable_caught']}/{m['unreliable']} ({m['recall']})" if m["unreliable"] else "–"
            lines.append(
                f"| {name} | {market} | {m['pages']} | {m['roc_auc']} | {m['brier']} | {warned_cell} | {caught_cell} "
                f"| {m['legit_low_score']} / {m['unreliable_low_score']} |"
            )
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> None:
    """
    Merge, score and report (see the module docstring).

    :param args: Parsed command-line arguments.
    """
    corpus = Corpus()
    reports = corpus.root / "reports"
    path = args.features or max(reports.glob("*Z-features.jsonl"), key=lambda p: p.stat().st_mtime)
    old = load_features(path)
    labelled = [r for r in corpus.labels().values() if r.label and r.split in {"train", "test"}]
    pages = list(await asyncio.gather(*(recompute(corpus, r, old.get(r.id)) for r in labelled)))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # Two files: the run's pages (every skill, for the full model) and all pages (the quick skills are complete).
    out_features = reports / f"{stamp}-markets-features.jsonl"
    out_quick = reports / f"{stamp}-markets-quick-features.jsonl"
    for out, kept in ((out_features, [p for p in pages if p.from_run]), (out_quick, pages)):
        out.write_text("".join(json.dumps({"id": p.record.id, "features": p.features}) + "\n" for p in kept))

    split = [p for p in pages if args.split in {"all", p.record.split}]
    models: dict[str, tuple[Model, bool, list[Page]]] = {}
    for name, model_path, quick in (("full", MODEL_PATH, False), ("quick", MODEL_QUICK_PATH, True)):
        shipped = scoring.load_model(model_path)
        assert shipped is not None
        models[f"{name} (shipped {shipped.version})"] = (shipped, quick, split)
    trained: dict[str, tuple[Model, int]] = {}
    if args.train:
        version_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        for name, quick in (("full", False), ("quick", True)):
            tag = "quick-" if quick else ""
            trained[name] = candidate(pages, quick=quick, prior=args.prior, version=f"v1-{tag}{version_stamp}-C")
            models[f"{name} (candidate {trained[name][0].version})"] = (trained[name][0], quick, split)

    report: dict[str, Any] = {
        "stamp": stamp,
        "split": args.split,
        "features_from": path.name,
        "features_file": out_features.name,
        "quick_features_file": out_quick.name,
        "pages_by_market": dict(Counter(p.market for p in split)),
        "labels_by_market": dict(Counter(f"{p.market}:{p.record.label}" for p in split)),
        "models": {},
    }
    for name, (model, quick, subset) in models.items():
        # The full model reads the LLM's content features, which only pages from a run have.
        scored = subset if quick else [p for p in subset if p.from_run]
        report["models"][name] = {"metrics": by_market(scored, model, quick=quick)}

    for name in ("full", "quick") if args.ship == "both" else (args.ship,) if args.ship else ():
        model, train_n = trained[name]
        quick = name == "quick"
        test = [p for p in pages if p.record.split == "test" and (quick or p.from_run)]
        data = build([p.record for p in test], {p.record.id: p.features for p in test}, model.features)
        write_artifact(
            model,
            MODEL_QUICK_PATH if quick else MODEL_PATH,
            features_file=(out_quick if quick else out_features).name,
            train_n=train_n,
            prior=args.prior,
            test=data,
        )
        report[f"shipped_{name}"] = model.version

    dump_json(reports / f"markets-{stamp}.json", report)
    markdown = to_markdown(report)
    (reports / f"markets-{stamp}.md").write_text(markdown)
    print(markdown)  # noqa: T201 - command-line report


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--features", type=Path, default=None, help="an earlier *-features.jsonl from evals.run")
    parser.add_argument("--split", choices=["test", "train", "all"], default="test", help="which split to report")
    parser.add_argument("--train", action="store_true", help="fit and report candidates on the merged features")
    parser.add_argument("--prior", type=float, default=0.5, help="base rate of unreliable pages to calibrate to")
    parser.add_argument("--ship", choices=["quick", "full", "both"], default=None, help="write a candidate (--train)")
    args = parser.parse_args()
    if args.ship and not args.train:
        parser.error("--ship needs --train")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
