"""
Re-score the corpus with the US-market rules and report how the scorer does on US and EU pages separately.

Usage::

    uv run python -m evals.us_market [--features <run>-features.jsonl] [--ship]

The deterministic skills that the US-market rules change or add (``eu_registry`` without VIES, ``us_identity``,
``shop_policies``) are recomputed on each page's saved HTML and merged into an earlier run's features; everything
else (including the LLM-read ``content`` features) is reused, so this costs no LLM calls. Each page's market comes
from :func:`unfret.market.shop_market` on its facts and original URL.

It reports the shipped model on TEST with the old and the new features, split by market, and a retrained candidate
(variant C, as shipped: no rule-only labels, no rule-mirroring columns). ``--ship`` writes the candidate to
``model.json`` only when it improves the overall AUC and Brier without lowering EU AUC; otherwise it says why not.
The merged features go to ``<corpus>/reports/<stamp>-us-features.jsonl``, the report next to it.
"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.corpus import Corpus, LabelRecord, dump_json
from evals.train import RULE_MIRRORS, build, evaluate, feature_names, fit, load_features
from unfret.facts import parse_page
from unfret.graph.skills import Skill
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.graph.skills.shop_policies import ShopPoliciesSkill
from unfret.graph.skills.us_identity import UsIdentitySkill
from unfret.market import shop_market
from unfret.scoring import MODEL_PATH, Model, load_model

RECOMPUTED: list[Skill] = [EuRegistrySkill(vies=False), UsIdentitySkill(), ShopPoliciesSkill()]


async def recompute(corpus: Corpus, record: LabelRecord, old: dict[str, float]) -> tuple[dict[str, float], str]:
    """
    Recompute the US-market skills for one page and merge them into its earlier features.

    :param corpus: The corpus.
    :param record: The page's label record.
    :param old: Its features from an earlier run.
    :return: The merged features and the page's market.
    """
    html = (corpus.root / "pages" / f"{record.id}.html").read_text(encoding="utf-8", errors="replace")
    facts, text = await asyncio.to_thread(parse_page, html, record.origin_url)
    page_type = "aggregate_listing" if old.get("listing.is_marketplace") else "storefront"
    state: Any = {
        "page_content": html,
        "url": record.origin_url,
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": page_type,
    }
    names = {s.name for s in RECOMPUTED}
    merged = {k: v for k, v in old.items() if k.split(".", 1)[0] not in names}
    for skill in RECOMPUTED:
        if page_type in skill.page_types:
            merged |= (await skill.run(state))["features"]
    return merged, shop_market(facts, record.origin_url, text)


def by_market(
    model: Model, records: list[LabelRecord], features: dict[str, dict[str, float]], markets: dict[str, str]
) -> dict[str, Any]:
    """
    The model's TEST metrics overall and per market.

    :param model: The model.
    :param records: TEST records.
    :param features: Features by record id.
    :param markets: Market by record id.
    :return: Metrics keyed by "all", "us" and "eu".
    """
    out: dict[str, Any] = {}
    for market in ("all", "us", "eu"):
        subset = [r for r in records if market == "all" or markets.get(r.id) == market]
        data = build(subset, features, model.features)
        if len(set(data.y.tolist())) < 2:
            out[market] = {"pages": len(data.records), "note": "one class only"}
            continue
        m = evaluate(model, data, model.features)
        out[market] = {"pages": m["pages"], "roc_auc": m["roc_auc"], "brier": m["brier"]}
    return out


async def main_async(args: argparse.Namespace) -> None:
    """
    Merge, score and report (see the module docstring).

    :param args: Parsed command-line arguments.
    """
    corpus = Corpus()
    reports = corpus.root / "reports"
    path = args.features or max(reports.glob("*Z-features.jsonl"), key=lambda p: p.stat().st_mtime)
    old = load_features(path)
    labelled = [r for r in corpus.labels().values() if r.label and r.split in {"train", "test"} and r.id in old]
    results = await asyncio.gather(*(recompute(corpus, r, old[r.id]) for r in labelled))
    new = {r.id: f for r, (f, _) in zip(labelled, results, strict=True)}
    markets = {r.id: m for r, (_, m) in zip(labelled, results, strict=True)}

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_features = reports / f"{stamp}-us-features.jsonl"
    out_features.write_text("".join(json.dumps({"id": i, "features": f}) + "\n" for i, f in new.items()))

    shipped = load_model(MODEL_PATH)
    assert shipped is not None
    test = [r for r in labelled if r.split == "test"]
    rule_columns = {c for cols in RULE_MIRRORS.values() for c in cols}
    names = [n for n in feature_names() if n not in rule_columns]
    train = build([r for r in labelled if r.split == "train" and r.label_source != "rule"], new, names)
    candidate = fit(train, names, f"v1-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-C-us")

    report: dict[str, Any] = {
        "stamp": stamp,
        "features_from": path.name,
        "features_file": out_features.name,
        "pages_by_market": dict(Counter(markets.values())),
        "test_by_market_and_label": dict(Counter(f"{markets[r.id]}:{r.label}" for r in test)),
        "shipped_old_features": by_market(shipped, test, old, markets),
        "shipped_new_features": by_market(shipped, test, new, markets),
        "candidate": {"version": candidate.version, **by_market(candidate, test, new, markets)},
        "signals_fired": dict(
            Counter(
                k
                for f in new.values()
                for k, v in f.items()
                if v and k.split(".", 1)[0] in {"us_identity", "shop_policies"}
            )
        ),
    }
    base, cand = report["shipped_new_features"], report["candidate"]
    better = (
        cand["all"]["roc_auc"] > base["all"]["roc_auc"]
        and cand["all"]["brier"] <= base["all"]["brier"]
        and cand["eu"].get("roc_auc", 0) >= base["eu"].get("roc_auc", 0)
    )
    report["ship_decision"] = (
        "candidate improves overall AUC and Brier without hurting EU"
        if better
        else ("keep the shipped model: the candidate doesn't improve overall AUC and Brier without hurting EU")
    )
    if args.ship and better:
        test_metrics = evaluate(candidate, build(test, new, names), names)
        artifact = {
            "version": candidate.version,
            "features": candidate.features,
            "means": candidate.means,
            "scales": candidate.scales,
            "coefficients": candidate.coefficients,
            "intercept": candidate.intercept,
            "calibration": candidate.calibration,
            "trained_on": {"features_file": out_features.name, "train_n": len(train.records)},
            "test": {k: test_metrics[k] for k in ("roc_auc", "brier", "thresholds")},
        }
        MODEL_PATH.write_text(json.dumps(artifact, indent=1) + "\n")
        report["shipped"] = candidate.version
    dump_json(reports / f"us-market-{stamp}.json", report)
    print(json.dumps(report, indent=1))  # noqa: T201 - command-line report


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--features", type=Path, default=None, help="an earlier *-features.jsonl from evals.run")
    parser.add_argument("--ship", action="store_true", help="ship the candidate if it improves (see above)")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
