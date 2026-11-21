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
  never from today's shop: a long-gone shop's dead photos would otherwise teach "photo found" as "still in business";
- **the variant check** (:mod:`unfret.graph.skills.variant_check`) on the page's own options.

``--only`` runs some of them and keeps a page's other results (a check that lands later is added for its own cost
only); ``--candidates`` adds them to the deep-check candidates' results instead (:mod:`evals.candidates`).

Left out on purpose: the history check (:mod:`unfret.graph.skills.domain_history`) compares a shop with its archive
copies before *today*, which for a page captured years ago measures what became of the domain since, not the page;
it runs on the live candidates (:mod:`evals.candidates`) only. The skills that look up today's registries stay off
for the same reason. The page's class must be a product page (``page_type`` as labelled; no classification call).

Spend is what the calls the recording cache stores cost, at the settings' prices (:func:`evals.candidates.miss_cost`:
an answer replayed costs nothing), counted together with the candidates': the run stops before the total would pass
``--budget-eur``. Output:
``reports/new-findings/results.jsonl`` (features, findings, cost per page) and ``reports/new-findings/features.jsonl``.

Usage: ``uv run python -m evals.new_findings [--limit N] [--budget-eur 55] [--concurrency 6] [--rerun]
[--only variant_check] [--candidates]``
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

from evals import candidates as cand
from evals.corpus import Corpus, LabelRecord, dump_json, is_product, now_iso
from evals.run import configure
from unfret.config import settings
from unfret.graph.nodes import preprocess_node
from unfret.graph.skills import SkillOutput, photo_claims
from unfret.graph.skills.claim_audit import ClaimAuditSkill
from unfret.graph.skills.photo_claims import PhotoClaimsSkill
from unfret.graph.skills.variant_check import VariantCheckSkill
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


SKILL_NAMES: tuple[str, ...] = ("claim_audit", "variant_check", "photo_claims")


async def one(
    corpus: Corpus, record: LabelRecord, *, skills: tuple[str, ...], previous: dict[str, Any] | None, with_url: bool
) -> dict[str, Any]:
    """
    The new checks on one page, merged into its earlier result when only some are run.

    :param corpus: The corpus.
    :param record: The page.
    :param skills: The checks to run (of :data:`SKILL_NAMES`; the photo check runs the claim audit too, replayed).
    :param previous: The page's earlier result, if any (its other checks' features and findings are kept).
    :param with_url: Read the facts with the page's URL (live candidates, as their deep check did).
    :return: Its result row (features, findings, cost), or one with ``error``.
    """
    start = time.perf_counter()
    base = previous if previous and "error" not in previous else {}
    row: dict[str, Any] = {**base, "id": record.id, "checked_at": now_iso(), "label": record.label}
    row.pop("error", None)
    if record.source == "wayback":
        _CAPTURE.set(record.collected_at or "2023")
    paid = cand.metered()
    url = record.origin_url if with_url else ""
    state: dict[str, Any] = {"page_content": corpus.read_page(record.id), "url": url, "flags": []}
    state |= await preprocess_node(cast("AnalysisState", state))
    state["page_type"] = record.page_type or "storefront"
    state["url"] = record.origin_url  # the photos' addresses
    try:
        outputs = await _checks(state, skills)
    except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
        row = {"id": record.id, "checked_at": row["checked_at"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    else:
        ran = set(outputs)
        features = {k: v for k, v in base.get("features", {}).items() if k.split(".")[0] not in ran}
        findings = [f for f in base.get("findings", []) if f["skill"] not in ran]
        for output in outputs.values():
            features |= output["features"]
            findings += [f.model_dump() for f in output["findings"]]
        row |= {"features": features, "findings": findings, "fired": cand.fired(features, findings)}
        if "claim_audit" in outputs:
            row["piece_count"] = outputs["claim_audit"].get("listing_piece_count")
    row["cost_eur"] = float(sum(paid, Decimal(0))) + (previous or {}).get("cost_eur", 0)
    row["seconds"] = round(time.perf_counter() - start, 1)
    return row


async def _checks(state: dict[str, Any], skills: tuple[str, ...]) -> dict[str, SkillOutput]:
    """
    The chosen checks on one page: the claim audit first when the photo check needs its claims.

    :param state: The page's state after preprocessing.
    :param skills: The checks to run.
    :return: Each check's output, by skill name.
    """
    outputs: dict[str, SkillOutput] = {}
    if "claim_audit" in skills or "photo_claims" in skills:
        audit = await ClaimAuditSkill().run(cast("AnalysisState", state))
        state |= {k: v for k, v in audit.items() if k in {"listing_claims", "listing_piece_count"}}
        outputs["claim_audit"] = audit
    if "variant_check" in skills:
        outputs["variant_check"] = await VariantCheckSkill().run(cast("AnalysisState", state))
    if "photo_claims" in skills:
        outputs["photo_claims"] = await PhotoClaimsSkill().run(cast("AnalysisState", state))
    return outputs


async def run(
    corpus: Corpus,
    *,
    limit: int | None,
    budget: Decimal,
    concurrency: int,
    rerun: bool,
    skills: tuple[str, ...] = SKILL_NAMES,
    on_candidates: bool = False,
) -> dict[str, Any]:
    """
    Run the new checks on the corpus's product pages (or the candidates) not done yet, within the budget.

    :param corpus: The corpus.
    :param limit: Pages at most.
    :param budget: Euros at most, everything recorded so far included (candidates and this replay).
    :param concurrency: Pages at the same time.
    :param rerun: Do pages with a result again (recorded answers cost nothing).
    :param skills: The checks to run; with fewer than all, a page's other checks' results are kept.
    :param on_candidates: Run on the deep-check candidates and add to their results instead.
    :return: The summary.
    """
    configure(corpus, replay=False)
    cand.meter_misses()
    settings.network_lookups = True  # the photo check fetches photos (from the archive for archived pages)
    photo_claims._fetch = _fetch
    load, save = (cand.read_results, cand.write_results) if on_candidates else (read_results, write_results)
    results = load(corpus)
    # Candidates get the new checks added to their deep check: one without its deep check yet waits for it.
    records = (
        [r for r in cand.candidates(corpus) if cand.checked(results.get(r.id))] if on_candidates else pages(corpus)
    )
    todo = [r for r in records if rerun or r.id not in results or "error" in results[r.id]]
    todo = todo[:limit] if limit else todo
    spent = Decimal(str(cand.total_spent(corpus))) + cand.other_spend(corpus)
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
            before = results.get(record.id, {}).get("cost_eur", 0)
            task = one(corpus, record, skills=skills, previous=results.get(record.id), with_url=on_candidates)
            row = await asyncio.create_task(task, context=contextvars.copy_context())
            paid = Decimal(str(row.get("cost_eur", 0) - before))
            async with lock:
                spent += paid
                worst = max(worst, paid * Decimal("1.5"))
                results[record.id] = row
                done += 1
                if done % 25 == 0:
                    save(corpus, results)
                    logger.info("new_findings_progress", done=done, total=len(todo), spent_eur=round(float(spent), 3))

    await asyncio.gather(*(page(r) for r in todo))
    save(corpus, results)
    summary = (cand.summarise(corpus, cand.read_results(corpus)) if on_candidates else summarise(corpus, results)) | {
        "run_pages": done,
        "run_skills": list(skills),
        "stopped_for_budget": stopped,
        "budget_eur": float(budget),
        "spent_eur_everything": round(float(spent), 3),
    }
    dump_json(corpus.root / (cand.REPORTS if on_candidates else REPORTS) / "summary.json", summary)
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
    parser.add_argument("--only", nargs="+", choices=SKILL_NAMES, default=list(SKILL_NAMES), help="checks to run")
    parser.add_argument("--candidates", action="store_true", help="the deep-check candidates instead of the corpus")
    args = parser.parse_args()
    corpus = Corpus()
    if args.summary:
        summary = summarise(corpus, read_results(corpus))
    else:
        summary = asyncio.run(
            run(
                corpus,
                limit=args.limit,
                budget=args.budget_eur,
                concurrency=args.concurrency,
                rerun=args.rerun,
                skills=tuple(args.only),
                on_candidates=args.candidates,
            )
        )
    print(json.dumps(summary, indent=1))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
