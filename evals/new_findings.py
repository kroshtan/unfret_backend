"""
The deep check's newer, page-intrinsic checks on every labelled product page: training rows for a retrain.

The scorer's training features come from a corpus replay (:mod:`evals.run`), which has no claim audit or photo
check features for the pages labelled before those checks existed. This runs just those checks on every product page
of the corpus (the labelled ones first, the golden set included, then the unlabelled ones, which the labelling queue
ranks by them) and writes their features for
``evals.train --extra-features``, recording each model answer in the corpus's ``llm_cache.sqlite`` so a re-run (or a
retrain's replay) is free:

- **the claim audit** (:mod:`unfret.graph.skills.claim_audit`, the piece-count rule included), on the page as the
  replay reads it (facts parsed without the URL, as :mod:`evals.run` withholds it);
- **the photo check** (:mod:`unfret.graph.skills.photo_claims`) where the listing states a count or size. A page
  from the Internet Archive has its photos fetched from the archive too (``im_`` copies from the capture's date),
  never from today's shop: a long-gone shop's dead photos would otherwise teach "photo found" as "still in business".

Left out on purpose: the history check (:mod:`unfret.graph.skills.domain_history`) compares a shop with its archive
copies before *today*, which for a page captured years ago measures what became of the domain since, not the page;
it runs on the live candidates (:mod:`evals.candidates`) only. The skills that look up today's registries stay off
for the same reason. The page's class must be a product page (``page_type`` as labelled; no classification call).

Spend is added up from each answer's token usage at the settings' prices and counted together with the candidates'
(:mod:`evals.candidates`): the run stops before the total would pass ``--budget-eur``. Output:
``reports/new-findings/results.jsonl`` (features, findings, cost per page) and ``reports/new-findings/features.jsonl``.

Usage: ``uv run python -m evals.new_findings [--limit N] [--budget-eur 55] [--concurrency 6] [--rerun]``
(``make new-findings``).
"""

import argparse
import asyncio
import contextvars
import json
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import structlog
from langchain_core.callbacks import get_usage_metadata_callback

from evals import candidates as cand
from evals.corpus import Corpus, LabelRecord, dump_json, is_product, now_iso
from evals.run import configure
from unfret.config import settings
from unfret.graph.nodes import preprocess_node
from unfret.graph.skills import SkillOutput, photo_claims
from unfret.graph.skills.claim_audit import ClaimAuditSkill
from unfret.graph.skills.photo_claims import PhotoClaimsSkill
from unfret.graph.state import AnalysisState

logger = structlog.get_logger("unfret.evals")

REPORTS = Path("reports") / "new-findings"
_CAPTURE: contextvars.ContextVar[str] = contextvars.ContextVar("capture", default="")
_fetch_live = photo_claims._fetch


def archived(url: str, capture: str) -> str:
    """
    The Internet Archive's raw copy of an image as of a capture date (it redirects to the closest one it holds).

    :param url: The image's address.
    :param capture: The capture date, ``YYYYMMDD…``.
    :return: The archive's address of the image.
    """
    if url.startswith("https://web.archive.org/"):
        return url
    return f"https://web.archive.org/web/{capture[:14]}im_/{url}"


async def _fetch(url: str) -> bytes:
    capture = _CAPTURE.get()
    return bytes(await _fetch_live(archived(url, capture) if capture else url))


def pages(corpus: Corpus) -> list[LabelRecord]:
    """
    The corpus's product pages, labelled ones first, then the unlabelled ones, without the candidates.

    The deep-check candidates are left to :mod:`evals.candidates`, which checks them in full.

    :param corpus: The corpus.
    :return: The records.
    """
    records = [r for r in corpus.labels().values() if is_product(r) and r.origin_dataset != cand.DATASET]
    return sorted(records, key=lambda r: (r.label is None, r.id))


def read_results(corpus: Corpus) -> dict[str, dict[str, Any]]:
    """The results so far, by page id."""
    path = corpus.root / REPORTS / "results.jsonl"
    if not path.exists():
        return {}
    rows = (json.loads(x) for x in path.read_text().splitlines() if x.strip())
    return {row["id"]: cand.with_new_findings(row) for row in rows}


def write_results(corpus: Corpus, results: dict[str, dict[str, Any]]) -> None:
    """Rewrite the results and the features file for ``evals.train --extra-features``."""
    folder = corpus.root / REPORTS
    folder.mkdir(parents=True, exist_ok=True)
    rows = [results[k] for k in sorted(results)]
    (folder / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (folder / "features.jsonl").write_text(
        "".join(json.dumps({"id": r["id"], "features": r["features"]}) + "\n" for r in rows if "features" in r)
    )


async def one(corpus: Corpus, record: LabelRecord) -> dict[str, Any]:
    """
    The new checks on one page.

    :param corpus: The corpus.
    :param record: The page.
    :return: Its result row (features, findings, cost), or one with ``error``.
    """
    start = time.perf_counter()
    row: dict[str, Any] = {"id": record.id, "checked_at": now_iso(), "label": record.label}
    if record.source == "wayback":
        _CAPTURE.set(record.collected_at or "2023")
    state: dict[str, Any] = {"page_content": corpus.read_page(record.id), "url": "", "flags": []}
    state |= await preprocess_node(cast("AnalysisState", state))
    state["page_type"] = record.page_type or "storefront"
    state["url"] = record.origin_url  # the photos' addresses only: the facts were read without it
    try:
        with get_usage_metadata_callback() as usage:
            audit, photos = await _checks(state)
    except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
        row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        usage_metadata: dict[str, Any] = {}
    else:
        usage_metadata = dict(usage.usage_metadata)
        findings = [f.model_dump() for f in [*audit["findings"], *photos["findings"]]]
        features = {**audit["features"], **photos["features"]}
        row |= {
            "features": features,
            "findings": findings,
            "fired": cand.fired(features, findings),
            "piece_count": audit.get("listing_piece_count"),
        }
    row["usage"] = usage_metadata
    row["cost_eur"] = float(cand.cost(usage_metadata))
    row["seconds"] = round(time.perf_counter() - start, 1)
    return row


async def _checks(state: dict[str, Any]) -> tuple[SkillOutput, SkillOutput]:
    """
    The claim audit, then the photo check with the claims it read.

    :param state: The page's state after preprocessing.
    :return: Both skills' outputs.
    """
    audit = await ClaimAuditSkill().run(cast("AnalysisState", state))
    state |= {k: v for k, v in audit.items() if k in {"listing_claims", "listing_piece_count"}}
    photos = await PhotoClaimsSkill().run(cast("AnalysisState", state))
    return audit, photos


async def run(corpus: Corpus, *, limit: int | None, budget: Decimal, concurrency: int, rerun: bool) -> dict[str, Any]:
    """
    Run the new checks on the labelled product pages not done yet, within the budget.

    :param corpus: The corpus.
    :param limit: Pages at most.
    :param budget: Euros at most, together with the candidates' spend.
    :param concurrency: Pages at the same time.
    :param rerun: Do pages with a result again (recorded answers cost nothing).
    :return: The summary.
    """
    configure(corpus, replay=False)
    settings.network_lookups = True  # the photo check fetches photos (from the archive for archived pages)
    photo_claims._fetch = _fetch
    results = read_results(corpus)
    todo = [r for r in pages(corpus) if rerun or r.id not in results or "error" in results[r.id]]
    todo = todo[:limit] if limit else todo
    redo = {r.id for r in todo}
    elsewhere = Decimal(str(cand.total_spent(corpus)))
    mine = Decimal(str(sum(r.get("cost_eur", 0) for k, r in results.items() if k not in redo)))
    spent = elsewhere + mine
    worst = Decimal("0.05")
    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    stopped = False
    done = 0

    async def page(record: LabelRecord) -> None:
        nonlocal spent, worst, stopped, done
        async with semaphore:
            async with lock:
                stopped = stopped or spent + worst * concurrency > budget
                if stopped:
                    return
            row = await asyncio.create_task(one(corpus, record), context=contextvars.copy_context())
            async with lock:
                spent += Decimal(str(row["cost_eur"]))
                worst = max(worst, Decimal(str(row["cost_eur"])) * Decimal("1.5"))
                results[record.id] = row
                done += 1
                if done % 25 == 0:
                    write_results(corpus, results)
                    logger.info("new_findings_progress", done=done, total=len(todo), spent_eur=round(float(spent), 3))

    await asyncio.gather(*(page(r) for r in todo))
    write_results(corpus, results)
    summary = summarise(corpus, results) | {
        "run_pages": done,
        "stopped_for_budget": stopped,
        "budget_eur": float(budget),
        "spent_eur_all_runs_and_candidates": round(float(spent), 3),
    }
    dump_json(corpus.root / REPORTS / "summary.json", summary)
    return summary


def summarise(corpus: Corpus, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    How often each new check fired, per label and per origin dataset, and the spend.

    :param corpus: The corpus.
    :param results: The results.
    :return: The summary.
    """
    records = {r.id: r for r in pages(corpus)}
    ok = [row for k, row in results.items() if k in records and "error" not in row]
    fired: Counter[str] = Counter()
    for row in ok:
        for skill in row["fired"]:
            fired[f"{skill}|{records[row['id']].label}"] += 1
    by_dataset: dict[str, Counter[str]] = {}
    for row in ok:
        counter = by_dataset.setdefault(records[row["id"]].origin_dataset, Counter())
        counter["pages"] += 1
        counter["fired"] += bool(row["fired"])
        counter["count_off"] += bool(row["features"].get("claim_audit.count_off"))
        counter["photo_checked"] += bool(row["features"].get("photo_claims.checked"))
    labels = Counter(records[row["id"]].label for row in ok)
    return {
        "product_pages": len(records),
        "done": len(ok),
        "errors": sum(1 for k, row in results.items() if k in records and "error" in row),
        "pages_per_label": dict(labels),
        "fired_per_skill_and_label": dict(sorted(fired.items())),
        "per_dataset": {k: dict(v) for k, v in sorted(by_dataset.items())},
        "spent_eur": round(sum(row.get("cost_eur", 0) for row in results.values()), 3),
    }


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--budget-eur", type=Decimal, default=Decimal(55))
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--rerun", action="store_true")
    parser.add_argument("--summary", action="store_true", help="print the numbers and stop")
    args = parser.parse_args()
    corpus = Corpus()
    if args.summary:
        summary = summarise(corpus, read_results(corpus))
    else:
        summary = asyncio.run(
            run(corpus, limit=args.limit, budget=args.budget_eur, concurrency=args.concurrency, rerun=args.rerun)
        )
    print(json.dumps(summary, indent=1))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
