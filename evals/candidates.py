"""
Deep-check the unlabelled candidate pages, order them for labelling by value, and label them in batches.

The candidates (:mod:`evals.sources.deep_candidates`, ``origin_dataset`` ``deep-findings-2026-11``) are today's
product pages where the deep check's newer findings (the claim audit, the photo check against the listing's own
claims, and the history, variation and cross-page checks as they land) should fire. Three steps:

1. ``check``: run the deep check on each page, as a deep check runs it (every skill but the paid reverse image search
   and the fingerprint store; network lookups on, since the pages are live; the URL given), with the scorer the
   service runs (``<corpus>/models/current.json``). LLM answers are recorded in the corpus's ``llm_cache.sqlite``
   (so a re-run, say after a new check lands, pays only for what is new). What each model call costs is added up
   from its token usage and the prices below; the run stops before the set (earlier runs included) would pass
   ``--budget-eur``. Per page: ``reports/deep-findings/results.jsonl`` (features, findings, which new findings
   fired, score, band, confidence), and ``reports/deep-findings/features.jsonl`` for ``evals.train --extra-features``.
2. ``queue``: order the pages by value (active learning) into ``deep_label_queue.jsonl``: first the pages where a new
   finding fired and the current scorer is unsure, then those where a new finding fired but the answer disagrees
   (the scorer is sure the page is fine), then a seeded random slice of the rest.
3. ``label``: a batch of pages (``--batch 50``), one screen each: the page facts, the scorer's answer, every new
   finding with its evidence, a short excerpt. Keys: ``l`` legit, ``j`` junk, ``s`` scam, ``r`` reject (not a
   product page, unusable), ``o`` open the page in the browser, ``k`` skip, ``q`` quit. After a label, each new
   finding on the page gets one key: ``c`` correct, ``w`` wrong, ``Enter`` or ``u`` unsure. Page labels go to
   ``labels.jsonl`` (``label_source`` ``human``), finding labels to ``finding_labels.jsonl`` (one record per finding:
   page, skill, type, evidence, verdict), so each check's precision can be measured and tuned on its own. Quitting
   halfway loses nothing.

Usage: ``uv run python -m evals.candidates check [--limit N] [--budget-eur 25] [--concurrency 3] [--rerun]``,
``... queue [--seed 7]``, ``... label [--batch 50]`` (``make deep-check``, ``make deep-queue``, ``make deep-label``).
"""

import argparse
import asyncio
import json
import os
import random
import subprocess
import textwrap
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from langchain_core.callbacks import UsageMetadataCallbackHandler

from evals.corpus import Corpus, LabelRecord, QueueItem, dump_json, now_iso
from evals.label import read_key
from evals.run import configure
from evals.sources.deep_candidates import DATASET
from unfret import scoring
from unfret.config import settings
from unfret.facts import parse_page
from unfret.graph import SKILLS, build_graph
from unfret.html_cleaner import clean_html

logger = structlog.get_logger("unfret.evals")

REPORTS = Path("reports") / "deep-findings"
QUEUE = "deep_label_queue.jsonl"
FINDING_LABELS = "finding_labels.jsonl"
EXCLUDED = ("reverse_image", "shop_links")  # paid per search; needs the fingerprint database
# The deep check's newer checks, by skill name (also those still being built: a name that contains one of these).
NEW_SKILLS: tuple[str, ...] = ("claim_audit", "photo_claims", "wayback", "variation", "cross_page")
# Feature suffixes that mean a new check found something, also without a finding (a count the photos don't show).
FIRED_FEATURES: tuple[str, ...] = ("contradicted", "count_mismatch", "size_mismatch", "padded", "mismatch")
UNSURE = 0.4  # scorer confidence below this is "unsure"
SURE_FINE = 35  # a score below this is the scorer saying the page is fine
_EXCERPT_CHARS = 900
FINDING_KEYS = {"c": "correct", "w": "wrong", "u": "unsure", "\r": "unsure", "\n": "unsure"}
PAGE_KEYS = {"l": "legit", "j": "junk", "s": "scam"}


def is_new(skill: str) -> bool:
    """Whether a skill is one of the deep check's newer checks."""
    return any(name in skill for name in NEW_SKILLS)


def prices() -> dict[str, tuple[Decimal, Decimal]]:
    """
    Euros per million input and output tokens, by model name (Scaleway's list prices, as the settings give them).

    :return: The prices; a model not listed is charged at the claim audit's (the dearest) rate.
    """
    small = (settings.photo_claims_input_eur_per_mtok, settings.photo_claims_output_eur_per_mtok)
    return {
        settings.llm_model: small,
        settings.photo_claims_model: small,
        settings.claim_audit_model: (
            settings.claim_audit_input_eur_per_mtok,
            settings.claim_audit_output_eur_per_mtok,
        ),
    }


def cost(usage: dict[str, Any]) -> Decimal:
    """
    What one page's model calls cost.

    :param usage: ``UsageMetadataCallbackHandler.usage_metadata``: token counts by model name.
    :return: Euros.
    """
    table = prices()
    dear = (settings.claim_audit_input_eur_per_mtok, settings.claim_audit_output_eur_per_mtok)
    total = Decimal(0)
    for model, tokens in usage.items():
        rate_in, rate_out = next((v for k, v in table.items() if k in model or model in k), dear)
        total += (
            rate_in * tokens.get("input_tokens", 0) / 1_000_000 + rate_out * tokens.get("output_tokens", 0) / 1_000_000
        )
    return total


def fired(features: dict[str, float], findings: list[dict[str, Any]]) -> list[str]:
    """
    The new checks that found something on a page.

    :param features: The page's features.
    :param findings: Its findings (dumped).
    :return: Skill names, sorted.
    """
    names = {f["skill"] for f in findings if is_new(f["skill"])}
    for name, value in features.items():
        skill, _, feature = name.partition(".")
        if is_new(skill) and value and any(feature.endswith(s) for s in FIRED_FEATURES):
            names.add(skill)
    return sorted(names)


def result_row(result: dict[str, Any]) -> dict[str, Any]:
    """
    What a deep check found on a page, for ``results.jsonl``.

    :param result: The graph's final state.
    :return: Page type, score, band, confidence, features, findings, the new ones, and which new checks fired.
    """
    findings = [f.model_dump() for f in result.get("findings", [])]
    features = dict(result.get("features", {}))
    verdict = result.get("verdict") or {}
    assessment = result.get("assessment") or {}
    return {
        "page_type": str(result.get("page_type", "")),
        "score": int(result["score"]),
        "band": str(verdict.get("band", "")),
        "confidence": assessment.get("confidence"),
        "risk": assessment.get("risk_score"),
        "features": features,
        "findings": findings,
        "new_findings": [f for f in findings if is_new(f["skill"])],
        "fired": fired(features, findings),
        "claims": len(result.get("listing_claims") or []),
        "piece_count": result.get("listing_piece_count"),
        "unavailable": sorted(k.split(".")[0] for k, v in features.items() if k.endswith(".unavailable") and v),
    }


def current_model(corpus: Corpus) -> str:
    """
    Point the scorer at the release the service runs (``models/current.json``), if there is one.

    :param corpus: The corpus.
    :return: The model version, or ``"baseline"``.
    """
    path = corpus.root / "models" / "current.json"
    version = json.loads(path.read_text()).get("model") if path.exists() else None
    if version:
        settings.model_path = str(corpus.root / "models" / version / "model.json")
        scoring.load_model.cache_clear()
    return version or "baseline"


def read_results(corpus: Corpus) -> dict[str, dict[str, Any]]:
    """The deep-check results so far, by page id."""
    path = corpus.root / REPORTS / "results.jsonl"
    if not path.exists():
        return {}
    rows = (json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return {row["id"]: row for row in rows}


def write_results(corpus: Corpus, results: dict[str, dict[str, Any]]) -> None:
    """Rewrite the results and the features file for training."""
    folder = corpus.root / REPORTS
    folder.mkdir(parents=True, exist_ok=True)
    rows = [results[k] for k in sorted(results)]
    (folder / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (folder / "features.jsonl").write_text(
        "".join(json.dumps({"id": r["id"], "features": r["features"]}) + "\n" for r in rows if "features" in r)
    )


def candidates(corpus: Corpus) -> list[LabelRecord]:
    """The candidate pages, by id."""
    return sorted((r for r in corpus.labels().values() if r.origin_dataset == DATASET), key=lambda r: r.id)


async def check(corpus: Corpus, limit: int | None, budget: Decimal, concurrency: int, rerun: bool) -> dict[str, Any]:
    """
    Deep-check the candidates not checked yet, within the budget.

    :param corpus: The corpus.
    :param limit: Pages at most.
    :param budget: Euros at most for the whole set, earlier runs included.
    :param concurrency: Pages checked at the same time.
    :param rerun: Check pages that have a result already (cached answers cost nothing).
    :return: The run's summary.
    """
    configure(corpus, replay=False)
    settings.network_lookups = True  # live pages: photos, domain age and registries are today's
    model = current_model(corpus)
    graph = build_graph([s for s in SKILLS if s.name not in EXCLUDED])
    results = read_results(corpus)
    todo = [r for r in candidates(corpus) if rerun or r.id not in results or "error" in results[r.id]]
    todo = todo[:limit] if limit else todo
    # The budget is for the whole set: what earlier runs spent counts (a re-run's cached answers are counted again,
    # so it errs on the safe side).
    before = Decimal(str(sum(row.get("cost_eur", 0) for row in results.values())))
    spent = before
    worst = Decimal("0.10")  # a page's cost at most until pages have been seen
    semaphore = asyncio.Semaphore(concurrency)
    stopped = False
    done = 0
    lock = asyncio.Lock()

    async def one(record: LabelRecord) -> None:
        nonlocal spent, worst, stopped, done
        async with semaphore:
            async with lock:
                if spent + worst * concurrency > budget:
                    stopped = True
                if stopped:
                    return
            usage = UsageMetadataCallbackHandler()
            start = time.perf_counter()
            row: dict[str, Any] = {"id": record.id, "checked_at": now_iso(), "model": model}
            try:
                async with asyncio.timeout(settings.analysis_timeout_s * 3):
                    result = await graph.ainvoke(
                        {"page_content": corpus.read_page(record.id), "url": record.origin_url, "flags": []},
                        config={"callbacks": [usage]},
                    )
            except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
                row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            else:
                row |= result_row(result)
            page_cost = cost(usage.usage_metadata)
            row["cost_eur"] = float(page_cost)
            row["usage"] = usage.usage_metadata
            row["seconds"] = round(time.perf_counter() - start, 1)
            async with lock:
                spent += page_cost
                worst = max(worst, page_cost * Decimal("1.5"))
                results[record.id] = row
                done += 1
                if done % 10 == 0:
                    write_results(corpus, results)
                    logger.info("deep_check_progress", done=done, total=len(todo), spent_eur=round(float(spent), 3))

    await asyncio.gather(*(one(r) for r in todo))
    write_results(corpus, results)
    summary = summarise(corpus, results)
    summary |= {
        "run_pages": done,
        "run_spent_eur": round(float(spent - before), 3),
        "stopped_for_budget": stopped,
        "budget_eur": float(budget),
        "model": model,
    }
    dump_json(corpus.root / REPORTS / "summary.json", summary)
    return summary


def summarise(corpus: Corpus, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """
    The set in numbers: pages per pool and category, how often each new check fired, errors and spend.

    :param corpus: The corpus.
    :param results: The deep-check results.
    :return: The summary.
    """
    records = candidates(corpus)
    notes = {r.id: json.loads(r.notes or "{}") for r in records}
    checked = [results[r.id] for r in records if r.id in results and "error" not in results[r.id]]
    per_skill: Counter[str] = Counter()
    per_type: Counter[str] = Counter()
    for row in checked:
        per_skill.update(row["fired"])
        per_type.update(f"{f['skill']}:{f['type']}" for f in row["new_findings"])
    by_pool: dict[str, Counter[str]] = {}
    for row in checked:
        pool = notes[row["id"]].get("pool", "")
        by_pool.setdefault(pool, Counter())["checked"] += 1
        if row["fired"]:
            by_pool[pool]["fired"] += 1
    return {
        "pages": len(records),
        "per_pool": dict(Counter(n.get("pool", "") for n in notes.values())),
        "per_category": dict(Counter(n.get("category", "") for n in notes.values())),
        "per_marketplace": dict(Counter(n["marketplace"] for n in notes.values() if "marketplace" in n)),
        "checked": len(checked),
        "errors": sum(1 for r in records if "error" in results.get(r.id, {})),
        "not_product": sum(1 for row in checked if row["score"] < 0),
        "fired_any": sum(1 for row in checked if row["fired"]),
        "fired_per_skill": dict(per_skill),
        "findings_per_type": dict(per_type),
        "fired_per_pool": {k: dict(v) for k, v in by_pool.items()},
        "claim_audit_unavailable": sum(1 for row in checked if "claim_audit" in row["unavailable"]),
        "photo_claims_checked": sum(1 for row in checked if row["features"].get("photo_claims.checked")),
        "spent_eur_total": round(sum(results[r.id].get("cost_eur", 0) for r in records if r.id in results), 3),
    }


def build_queue(corpus: Corpus, seed: int, random_share: int) -> list[QueueItem]:
    """
    Order the checked, unlabelled candidates by value.

    Tier 1 (priority 3–4): a new check fired and the scorer is unsure, least sure first. Tier 2 (2–3): a new check
    fired but the scorer says the page is fine, or is sure either way; the lower the score, the bigger the
    disagreement. Tier 3 (below 1): a seeded random slice of the rest, ``random_share`` pages.

    :param corpus: The corpus.
    :param seed: The random slice's seed.
    :param random_share: Pages in the random slice.
    :return: The queue.
    """
    results = read_results(corpus)
    items: list[QueueItem] = []
    rest: list[QueueItem] = []
    for record in candidates(corpus):
        row = results.get(record.id)
        if record.label is not None or row is None or "error" in row or row["score"] < 0:
            continue
        if json.loads(record.notes or "{}").get("rejected"):
            continue
        confidence = 1.0 if row.get("confidence") is None else float(row["confidence"])
        names = ", ".join(row["fired"])
        if row["fired"] and confidence < UNSURE:
            items.append(
                QueueItem(
                    id=record.id, reason=f"{names} fired; scorer unsure ({confidence:.2f})", priority=4 - confidence
                )
            )
        elif row["fired"]:
            gap = 0.99 * (1 - row["score"] / 100)
            why = "says fine" if row["score"] < SURE_FINE else f"score {row['score']}"
            items.append(QueueItem(id=record.id, reason=f"{names} fired; scorer {why}", priority=2 + gap))
        else:
            rest.append(QueueItem(id=record.id, reason="random slice", priority=0.0))
    rng = random.Random(seed)
    rng.shuffle(rest)
    for n, item in enumerate(rest[:random_share]):
        items.append(item.model_copy(update={"priority": 0.9 - n / (10 * max(1, random_share))}))
    return sorted(items, key=lambda i: -i.priority)


def read_queue(corpus: Corpus) -> list[QueueItem]:
    """The labelling queue, highest priority first."""
    path = corpus.root / QUEUE
    if not path.exists():
        return []
    return sorted(
        (QueueItem.model_validate_json(x) for x in path.read_text().splitlines() if x), key=lambda i: -i.priority
    )


def describe(record: LabelRecord, row: dict[str, Any], item: QueueItem, *, n: int, total: int, html: str) -> str:
    """
    One page as a compact screen of text.

    :param record: The page's record.
    :param row: Its deep-check result.
    :param item: Its queue item.
    :param n: Its position in the batch.
    :param total: The batch's size.
    :param html: The page's HTML.
    :return: The text.
    """
    facts, _ = parse_page(html, record.origin_url)
    notes = json.loads(record.notes or "{}")
    text = textwrap.shorten(" ".join(clean_html(html).split()), _EXCERPT_CHARS, placeholder=" …")
    where = notes.get("marketplace") or "own shop"
    seller = notes.get("marketplace_seller") or facts.seller
    conf = row.get("confidence")
    lines = [
        f"── {n}/{total} " + "─" * 60,
        f"{record.id}  ·  {where}  ·  {notes.get('pool', '')}  ·  {notes.get('category', '')}  ·  why: {item.reason}",
        f"url: {record.origin_url}",
        f"title: {facts.title[:150]}",
        (
            f"price: {facts.price} {facts.currency} (was {facts.list_price})  ·  seller: {seller}  ·  "
            f"brand: {facts.brand}"
        ),
        f"scorer: {row['score']}  ·  {row.get('band', '')}  ·  confidence {conf if conf is None else round(conf, 2)}",
    ]
    if row.get("piece_count"):
        piece = row["piece_count"]
        lines.append(f"piece count: {json.dumps(piece, ensure_ascii=False)[:300]}")
    news = row.get("new_findings", [])
    lines.append(f"new findings ({len(news)}):" if news else "new findings: none")
    lines += [f"  {i}. [{f['skill']}] {f['type']}: {f['evidence'][:300]}" for i, f in enumerate(news, start=1)]
    others = [f for f in row.get("findings", []) if not is_new(f["skill"])][:4]
    if others:
        lines.append("other findings: " + "; ".join(f"{f['type']}" for f in others))
    lines += ["", textwrap.fill(text, 110), "", "[l]egit  [j]unk  [s]cam  [r]eject  [o]pen  [k] skip  [q]uit"]
    return "\n".join(lines)


def save_finding_labels(corpus: Corpus, record: LabelRecord, verdicts: list[tuple[dict[str, Any], str]]) -> None:
    """
    Append the finding labels of one page (one record per finding).

    :param corpus: The corpus.
    :param record: The page.
    :param verdicts: Each finding and its verdict (correct, wrong, unsure).
    """
    stamp = now_iso()
    with (corpus.root / FINDING_LABELS).open("a") as f:
        for finding, verdict in verdicts:
            row = {
                "id": record.id,
                "skill": finding["skill"],
                "type": finding["type"],
                "evidence": finding["evidence"],
                "verdict": verdict,
                "labelled_at": stamp,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def ask_findings(findings: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """
    One key per finding: correct, wrong, or unsure (Enter).

    :param findings: The page's new findings.
    :return: Each finding with its verdict.
    """
    verdicts = []
    for i, finding in enumerate(findings, start=1):
        print(f"  finding {i}/{len(findings)} [{finding['type']}]: [c]orrect  [w]rong  [Enter/u] unsure")  # noqa: T201
        key = read_key().lower()
        verdict = FINDING_KEYS.get(key, "unsure")
        print(f"  → {verdict}")  # noqa: T201 - interactive tool
        verdicts.append((finding, verdict))
    return verdicts


def label(corpus: Corpus, batch: int) -> None:
    """
    Label the next ``batch`` pages of the queue, one keypress per page (and one per new finding).

    :param corpus: The corpus.
    :param batch: Pages in this batch.
    """
    results = read_results(corpus)
    known = corpus.labels()
    queue = [
        i
        for i in read_queue(corpus)
        if i.id in known and known[i.id].label is None and not json.loads(known[i.id].notes or "{}").get("rejected")
    ]
    if not queue:
        print("Nothing to label: run `make deep-queue` after `make deep-check`.")  # noqa: T201
        return
    todo = queue[:batch]
    print(f"{len(queue)} page(s) in the queue; this batch: {len(todo)}.")  # noqa: T201 - interactive tool
    tally: Counter[str] = Counter()
    for n, item in enumerate(todo, start=1):
        record = corpus.labels()[item.id]
        row = results[item.id]
        print("\n" + describe(record, row, item, n=n, total=len(todo), html=corpus.read_page(record.id)))  # noqa: T201
        while True:
            key = read_key().lower()
            if key != "o":
                break
            subprocess.Popen(["xdg-open", record.origin_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603, S607
        if key == "q":
            break
        if key == "r":
            notes = json.loads(record.notes or "{}") | {"rejected": True}
            corpus.update([record.model_copy(update={"notes": json.dumps(notes, sort_keys=True)})])
            tally["rejected"] += 1
            print("→ rejected")  # noqa: T201 - interactive tool
            continue
        if key not in PAGE_KEYS:
            tally["skipped"] += 1
            continue
        page_label = PAGE_KEYS[key]
        print(f"→ {page_label}")  # noqa: T201 - interactive tool
        if row.get("new_findings"):
            save_finding_labels(corpus, record, ask_findings(row["new_findings"]))
        corpus.update([record.model_copy(update={"label": page_label, "label_source": "human", "confidence": 1.0})])
        tally[page_label] += 1
    left = len(queue) - sum(tally[k] for k in ("legit", "junk", "scam", "rejected"))
    print(f"\nThis batch: {dict(tally)}. {left} left in the queue. Then: `make split`, and commit the corpus.")  # noqa: T201


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("check", help="deep-check the candidates (records LLM answers)")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--budget-eur", type=Decimal, default=Decimal(25))
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--rerun", action="store_true", help="check pages with a result again (new checks)")
    queue = sub.add_parser("queue", help="order the checked pages for labelling")
    queue.add_argument("--seed", type=int, default=7)
    queue.add_argument("--random", type=int, default=100, help="pages in the random slice")
    lab = sub.add_parser("label", help="label the next batch")
    lab.add_argument("--batch", type=int, default=int(os.getenv("BATCH", "50")))
    sub.add_parser("summary", help="print the set's numbers")
    args = parser.parse_args()
    corpus = Corpus()
    if args.command == "check":
        summary = asyncio.run(check(corpus, args.limit, args.budget_eur, args.concurrency, args.rerun))
        print(json.dumps(summary, indent=1))  # noqa: T201 - command-line report
    elif args.command == "queue":
        items = build_queue(corpus, args.seed, args.random)
        (corpus.root / QUEUE).write_text("".join(i.model_dump_json() + "\n" for i in items))
        tiers = Counter(
            "fired, unsure" if i.priority >= 3 else "fired" if i.priority >= 2 else "random" for i in items
        )
        print(f"{len(items)} pages queued: {dict(tiers)}")  # noqa: T201 - command-line report
    elif args.command == "label":
        label(corpus, args.batch)
    else:
        print(json.dumps(summarise(corpus, read_results(corpus)), indent=1))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
