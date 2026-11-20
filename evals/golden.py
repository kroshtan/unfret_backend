"""
False alarms on well-known sellers: both tiers' verdicts on the golden set (:mod:`evals.sources.golden`).

Usage::

    uv run python -m evals.golden [--split test|train|all] [--record | --replay] [--tier deep quick]

The pass rule, per tier: **no** "Serious concerns", and fewer than :data:`MAX_FLAGGED_SHARE` of the pages "Worth a
look" or worse ("Think twice" counts too). The deep tier is the full analysis as :mod:`evals.accuracy` replays it
(the URL given, recorded LLM answers, no network lookups); the quick tier is the x402 quick check (its own skills
and scorer, no LLM). ``--lookups`` turns on the network lookups a deep check makes in production (domain age,
brand presence, the VAT register, the shop's legal pages): the golden pages are recent captures of current shops, so
today's answers fit them, unlike the archived corpus (those answers aren't recorded, so such a run isn't
reproducible). Each runs the scorers the server would run (``UNFRET_MODEL_PATH``,
``UNFRET_MODEL_QUICK_PATH``), so a retrained scorer is measured by setting them. ``--record`` (the default for the
deep tier, like :mod:`evals.run`) calls the LLM for pages whose answers aren't recorded yet; ``--replay`` never does.

Every page that fails the rule is listed with what drove it: the verdict's reasons and the scorer's strongest flags,
and the reasons and flags are counted over the failures. ``--split test`` measures the held-out part only (the
golden pages a retrained scorer never saw). The report goes to ``<corpus>/reports/golden-<stamp>.json`` and ``.md``;
it names the sellers, so it stays in the corpus.
"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import structlog

from evals.corpus import Corpus, LabelRecord, dump_json
from evals.metrics import BANDS, Outcome
from evals.run import DEFAULT_EXCLUDED, configure, evaluate
from evals.sources.golden import DATASET
from unfret import scoring
from unfret.config import settings
from unfret.graph import SKILLS, build_graph
from unfret.scoring import MODEL_QUICK_PATH
from unfret.x402.verdict import skills_for

logger = structlog.get_logger("unfret.evals")

# Fewer than this share of the pages may be "Worth a look" or worse, and none "Serious concerns".
MAX_FLAGGED_SHARE = 0.05
FLAGGED = frozenset({"concerns", "careful", "junk_or_scam"})
TIERS = ("deep", "quick")


def _notes(record: LabelRecord) -> dict[str, Any]:
    try:
        notes = json.loads(record.notes or "{}")
    except json.JSONDecodeError:
        return {}
    return notes if isinstance(notes, dict) else {}


def tier_summary(
    outcomes: list[Outcome], records: dict[str, LabelRecord], results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """
    One tier's verdict counts, the pass rule and each failure's causes.

    :param outcomes: The tier's results.
    :param records: The golden records, by id.
    :param results: Each page's ``verdict`` and ``assessment``, by id.
    :return: ``pages``, ``bands``, ``serious``, ``flagged`` (Worth a look or worse) and its share, ``passed``,
        ``failures`` (one entry per flagged page), the reasons and flags counted over the failures, and the reasons
        shown on any page (``reasons_shown``).
    """
    scored = [o for o in outcomes if o.band in BANDS]
    bands = Counter(o.band for o in scored)
    flagged = [o for o in scored if o.band in FLAGGED]
    failures = []
    for o in sorted(flagged, key=lambda o: (BANDS.index(o.band), o.score or 0), reverse=True):
        result = results.get(o.id) or {}
        verdict = result.get("verdict") or {}
        assessment = result.get("assessment") or {}
        notes = _notes(records[o.id])
        failures.append(
            {
                "id": o.id,
                "seller": notes.get("seller", ""),
                "market": notes.get("market", ""),
                "fetched": records[o.id].source,
                "split": records[o.id].split,
                "url": records[o.id].origin_url,
                "band": o.band,
                "score": o.score,
                "confidence": o.confidence,
                "reasons": [r["type"] for r in verdict.get("reasons", [])],
                "flags": [(f["type"], f["weight"]) for f in assessment.get("flags", [])][:4],
                "note": o.note,
            }
        )
    share = len(flagged) / len(scored) if scored else None
    return {
        "pages": len(outcomes),
        "scored": len(scored),
        "errors": [{"id": o.id, "error": o.error} for o in outcomes if o.error],
        "not_assessed": sum(1 for o in outcomes if not o.error and o.score == -1),
        "bands": {b: bands[b] for b in BANDS},
        "serious": bands["junk_or_scam"],
        "flagged": len(flagged),
        "flagged_share": round(share, 3) if share is not None else None,
        "passed": bool(scored) and bands["junk_or_scam"] == 0 and share is not None and share < MAX_FLAGGED_SHARE,
        "failure_reasons": dict(Counter(r for f in failures for r in f["reasons"]).most_common()),
        # Every reason shown on any page, "Looks fine" included (its one note): a note is a false alarm too.
        "reasons_shown": dict(
            Counter(
                r["type"] for result in results.values() for r in (result.get("verdict") or {}).get("reasons", [])
            ).most_common()
        ),
        "failure_flags": dict(Counter(name for f in failures for name, _ in f["flags"]).most_common()),
        "failures": failures,
    }


def to_markdown(report: dict[str, Any]) -> str:
    """
    Render the report.

    :param report: The report dict.
    :return: Markdown.
    """
    lines = [
        f"# Golden set {report['stamp']} ({report['split']}, {report['pages']} pages"
        + (", with network lookups)" if report.get("lookups") else ")"),
        "",
        (
            f"Scorers: deep `{report['scorers']['deep']}`, quick `{report['scorers']['quick']}`. Pass: no Serious "
            f"concerns and under {MAX_FLAGGED_SHARE:.0%} Worth a look or worse."
        ),
        "",
        "| tier | scored | Looks fine | Worth a look | Think twice | Serious | flagged | pass |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tier, t in report["tiers"].items():
        b = t["bands"]
        lines.append(
            f"| {tier} | {t['scored']} | {b['reliable']} | {b['concerns']} | {b['careful']} | {b['junk_or_scam']} "
            f"| {t['flagged']} ({t['flagged_share']}) | {'yes' if t['passed'] else 'no'} |"
        )
    for tier, t in report["tiers"].items():
        lines += ["", f"## {tier}: what drives the failures", ""]
        lines.append(f"Reasons: {t['failure_reasons']}")
        lines += ["", f"Reasons shown on any page (notes on Looks fine included): {t.get('reasons_shown', {})}"]
        lines.append("")
        lines.append(f"Flags: {t['failure_flags']}")
        lines += ["", "| seller | market | split | band | score | reasons | flags |", "|---|---|---|---|---|---|---|"]
        for f in t["failures"]:
            flags = ", ".join(f"{n} {w}" for n, w in f["flags"])
            lines.append(
                f"| {f['seller']} | {f['market']} | {f['split']} | {f['band']} | {f['score']} "
                f"| {', '.join(f['reasons'])} | {flags} |"
            )
        if t["errors"]:
            lines += ["", f"Errors: {t['errors']}"]
    return "\n".join(lines) + "\n"


async def run_tier(
    tier: str, corpus: Corpus, records: list[LabelRecord], concurrency: int
) -> tuple[list[Outcome], dict[str, dict[str, Any]]]:
    """
    Score the pages with one tier.

    :param tier: ``deep`` or ``quick``.
    :param corpus: The corpus.
    :param records: The golden records.
    :param concurrency: Pages scored at the same time.
    :return: The outcomes, and each page's verdict and assessment.
    """
    skills = [s.name for s in SKILLS if s.name not in DEFAULT_EXCLUDED]
    graph = None
    if tier == "quick":
        quick = [s for s in skills_for("quick", SKILLS) if s.name not in DEFAULT_EXCLUDED]
        graph = build_graph(quick, model_path=MODEL_QUICK_PATH, llm=False)
    results: dict[str, dict[str, Any]] = {}
    outcomes = await evaluate(corpus, records, skills, True, concurrency, results=results, graph=graph)
    return outcomes, results


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--split", choices=["test", "train", "all"], default="all")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="call the LLM on cache misses (default)")
    mode.add_argument("--replay", action="store_true", help="only use cached LLM responses")
    parser.add_argument("--tier", nargs="+", choices=TIERS, default=list(TIERS))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--lookups", action="store_true", help="the deep tier's network lookups, as in production")
    args = parser.parse_args()

    corpus = Corpus()
    configure(corpus, replay=args.replay)
    settings.network_lookups = args.lookups
    settings.expansion_classes = True  # as dev and production run
    golden = {
        r.id: r
        for r in corpus.labels().values()
        if r.origin_dataset == DATASET and r.label and args.split in {"all", r.split}
    }
    records = sorted(golden.values(), key=lambda r: r.id)
    full, quick = scoring.load_model(), scoring.load_model(MODEL_QUICK_PATH) or scoring.load_model()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "stamp": stamp,
        "split": args.split,
        "lookups": args.lookups,
        "pages": len(records),
        "markets": dict(Counter(_notes(r).get("market", "") for r in records)),
        "scorers": {"deep": full.version if full else None, "quick": quick.version if quick else None},
        "tiers": {},
    }
    for tier in args.tier:
        logger.info("golden_tier", tier=tier, pages=len(records))
        outcomes, results = asyncio.run(run_tier(tier, corpus, records, args.concurrency))
        report["tiers"][tier] = tier_summary(outcomes, golden, results)
    reports = corpus.root / "reports"
    reports.mkdir(exist_ok=True)
    dump_json(reports / f"golden-{stamp}.json", report)
    markdown = to_markdown(report)
    (reports / f"golden-{stamp}.md").write_text(markdown)
    print(markdown)  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
