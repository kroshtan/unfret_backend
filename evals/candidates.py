"""
Deep-check the unlabelled candidate pages, order them for labelling by value, and label them in batches.

The candidates (:mod:`evals.sources.deep_candidates`, ``origin_dataset`` ``deep-findings-2026-11``) are today's
product pages where the deep check's newer findings (the claim audit, the photo check against the listing's own
claims, and the history, variation and cross-page checks as they land) should fire. Three steps:

1. ``check``: run the deep check on each page, as a deep check runs it (every skill but the paid reverse image search
   and the fingerprint store; network lookups on, since the pages are live; the URL given), with the scorer the
   service runs (``<corpus>/models/current.json``). LLM answers are recorded in the corpus's ``llm_cache.sqlite``
   (so a re-run, say after a new check lands, pays only for what is new). What each recorded model call costs (an
   answer the cache stores, at the prices below; one replayed is free) is added up; the run stops before the set
   (earlier runs and ``evals.new_findings`` included) would pass
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
   halfway loses nothing. A page from elsewhere in the corpus can be put at the front of the queue to have its
   findings judged (``check --pin ID --why TEXT``, kept in ``deep_label_pins.json``); its label then becomes a
   human one. ``label --review`` serves the LLM labeller's review queue instead (:mod:`evals.llm_label`: the pages
   it was unsure of, then a random audit sample, those blind), ``label --test-set`` the human-only test set.

Usage: ``uv run python -m evals.candidates check [--limit N] [--budget-eur 55] [--concurrency 3] [--rerun]``,
``... queue [--seed 7]``, ``... label [--batch 50]`` (``make deep-check``, ``make deep-queue``, ``make deep-label``).
"""

import argparse
import asyncio
import contextvars
import hashlib
import json
import os
import random
import re
import subprocess
import textwrap
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from langchain_core.globals import get_llm_cache

from evals.corpus import Corpus, LabelRecord, QueueItem, dump_json, is_llm, now_iso
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
# One line per decision: page, label, the label before, whether the scorer's answer was hidden (anchoring).
DECISIONS = "label_decisions.jsonl"
BLIND_ONE_IN = 5
# Pages from elsewhere in the corpus put at the front of the queue, with why (``check --pin ID --why TEXT``).
PINS = "deep_label_pins.json"
EXCLUDED = ("reverse_image", "shop_links")  # paid per search; needs the fingerprint database
# The deep check's newer checks, by skill name (also those still being built: a name that contains one of these).
NEW_SKILLS: tuple[str, ...] = ("claim_audit", "photo_claims", "domain_history", "variant", "cross_page")
# Findings of those that only give context (what an archive copy showed), not something found: not asked about.
NEUTRAL_FINDINGS: frozenset[str] = frozenset({"archive_copy", "no_archive_copies"})
# Feature suffixes that mean a new check found something, also without a finding (a count the photos don't show).
FIRED_FEATURES: tuple[str, ...] = (
    "contradicted",
    "count_off",
    "count_mismatch",
    "size_mismatch",
    "padded",
    "mismatch",
    "changed",
    "parked",
    "grouped",
    "bait_price",
)
UNSURE = 0.4  # scorer confidence below this is "unsure"
SURE_FINE = 35  # a score below this is the scorer saying the page is fine
_EXCERPT_CHARS = 900
FINDING_KEYS = {"c": "correct", "w": "wrong", "u": "unsure", "\r": "unsure", "\n": "unsure"}
PAGE_KEYS = {"l": "legit", "j": "junk", "s": "scam"}


def notes_of(record: LabelRecord) -> dict[str, Any]:
    """A record's ``notes`` as a dict (empty when they aren't a JSON object)."""
    try:
        notes = json.loads(record.notes or "{}")
    except json.JSONDecodeError:
        return {}
    return notes if isinstance(notes, dict) else {}


def blind(page: str) -> bool:
    """
    Whether a page is labelled blind: without the scorer's answer, one page in five, fixed by its id.

    :param page: The page id.
    :return: ``True`` for about one page in five.
    """
    return int(hashlib.sha256(page.encode()).hexdigest()[:8], 16) % BLIND_ONE_IN == 0


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


# What the current page's recorded model calls cost: each new answer the cache stores is a call that was paid for.
_METER: contextvars.ContextVar[list[Decimal] | None] = contextvars.ContextVar("meter", default=None)
_MODEL_RE = re.compile(r'"model_name":\s*"([^"]+)"')


def miss_cost(llm_string: str, generations: Any) -> Decimal:
    """
    What one model call cost, from the answer the cache is about to store.

    :param llm_string: The cache's model key (holds ``model_name``).
    :param generations: The answer's generations (their messages carry the token usage).
    :return: Euros.
    """
    found = _MODEL_RE.search(llm_string)
    tokens = {"input_tokens": 0, "output_tokens": 0}
    for generation in generations or []:
        usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
        tokens["input_tokens"] += int(usage.get("input_tokens", 0))
        tokens["output_tokens"] += int(usage.get("output_tokens", 0))
    return cost({found.group(1) if found else "": tokens})


def meter_misses() -> None:
    """
    Count what the recording cache stores, so a run's spend is the calls it paid for (an answer replayed is free).

    Call after :func:`evals.run.configure`; a page's calls add up in the list :func:`metered` sets.
    """
    cache = get_llm_cache()
    if cache is None or getattr(cache, "_unfret_metered", False):
        return
    original = cache.update

    def update(prompt: str, llm_string: str, return_val: Any) -> None:
        original(prompt, llm_string, return_val)
        if (spent := _METER.get()) is not None:
            spent.append(miss_cost(llm_string, return_val))

    cache.update = update  # type: ignore[method-assign]
    cache._unfret_metered = True  # type: ignore[attr-defined]


def metered() -> list[Decimal]:
    """Start counting the current task's paid calls; the returned list fills as they are recorded."""
    spent: list[Decimal] = []
    _METER.set(spent)
    return spent


def fired(features: dict[str, float], findings: list[dict[str, Any]]) -> list[str]:
    """
    The new checks that found something on a page.

    :param features: The page's features.
    :param findings: Its findings (dumped).
    :return: Skill names, sorted.
    """
    names = {f["skill"] for f in findings if is_new(f["skill"]) and f["type"] not in NEUTRAL_FINDINGS}
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
        "new_findings": [f for f in findings if is_new(f["skill"]) and f["type"] not in NEUTRAL_FINDINGS],
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
    return {row["id"]: with_new_findings(row) for row in rows}


def with_new_findings(row: dict[str, Any]) -> dict[str, Any]:
    """
    A result row with its new findings and fired checks worked out again from its findings and features.

    So a row recorded before a check counted as new reads the same as one recorded now.

    :param row: The row as recorded.
    :return: The row.
    """
    if "error" in row:
        return row
    found = row.get("findings", [])
    news = [f for f in found if is_new(f["skill"]) and f["type"] not in NEUTRAL_FINDINGS]
    return row | {"new_findings": news, "fired": fired(row.get("features", {}), found)}


def total_spent(corpus: Corpus) -> float:
    """What the candidates' deep checks have cost so far, in euros (from their recorded token usage)."""
    return float(sum(row.get("cost_eur", 0) for row in read_results(corpus).values()))


def other_spend(corpus: Corpus) -> Decimal:
    """
    What the corpus's other recorded runs of the new checks cost (``evals.new_findings``), which share the budget.

    :param corpus: The corpus.
    :return: Euros.
    """
    path = corpus.root / "reports" / "new-findings" / "results.jsonl"
    if not path.exists():
        return Decimal(0)
    rows = (json.loads(x) for x in path.read_text().splitlines() if x.strip())
    return Decimal(str(sum(row.get("cost_eur", 0) for row in rows)))


def write_results(corpus: Corpus, results: dict[str, dict[str, Any]]) -> None:
    """Rewrite the results and the features file for training."""
    folder = corpus.root / REPORTS
    folder.mkdir(parents=True, exist_ok=True)
    rows = [results[k] for k in sorted(results)]
    (folder / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    (folder / "features.jsonl").write_text(
        "".join(json.dumps({"id": r["id"], "features": r["features"]}) + "\n" for r in rows if "features" in r)
    )


def read_pins(corpus: Corpus) -> dict[str, str]:
    """The pinned pages and why, by page id."""
    path = corpus.root / PINS
    return dict(json.loads(path.read_text())) if path.exists() else {}


def pin(corpus: Corpus, ids: list[str], why: str) -> None:
    """
    Pin pages (any in the corpus) to the front of the labelling queue.

    :param corpus: The corpus.
    :param ids: The page ids.
    :param why: What the label should decide, shown on the page's screen.
    """
    pins = read_pins(corpus) | dict.fromkeys(ids, why)
    dump_json(corpus.root / PINS, pins)


def candidates(corpus: Corpus) -> list[LabelRecord]:
    """The candidate pages, by id."""
    return sorted((r for r in corpus.labels().values() if r.origin_dataset == DATASET), key=lambda r: r.id)


def to_check(
    corpus: Corpus, results: dict[str, dict[str, Any]], *, rerun: bool, ids: list[str] | None, limit: int | None
) -> list[LabelRecord]:
    """
    The pages a run checks: the given ones, else the candidates without a result (all of them with ``rerun``).

    :param corpus: The corpus.
    :param results: The results so far.
    :param rerun: Check candidates with a result again.
    :param ids: Check these pages only.
    :param limit: Pages at most.
    :return: The records.
    """
    if ids:
        known = corpus.labels()
        return [known[i] for i in ids]
    todo = [r for r in candidates(corpus) if rerun or not checked(results.get(r.id))]
    return todo[:limit] if limit else todo


def checked(row: dict[str, Any] | None) -> bool:
    """Whether a candidate has had its deep check (not just some checks added by :mod:`evals.new_findings`)."""
    return row is not None and "error" not in row and "score" in row


async def check(
    corpus: Corpus,
    *,
    limit: int | None,
    budget: Decimal,
    concurrency: int,
    rerun: bool = False,
    ids: list[str] | None = None,
) -> dict[str, Any]:
    """
    Deep-check the candidates not checked yet, within the budget.

    :param corpus: The corpus.
    :param limit: Pages at most.
    :param budget: Euros at most for the whole set, earlier runs and ``evals.new_findings`` included.
    :param concurrency: Pages checked at the same time.
    :param rerun: Check pages that have a result already (cached answers cost nothing).
    :param ids: Check only these pages (any in the corpus, checked again if they have a result).
    :return: The run's summary.
    """
    configure(corpus, replay=False)
    meter_misses()
    settings.network_lookups = True  # live pages: photos, domain age and registries are today's
    model = current_model(corpus)
    graph = build_graph([s for s in SKILLS if s.name not in EXCLUDED])
    results = read_results(corpus)
    todo = to_check(corpus, results, rerun=rerun, ids=ids, limit=limit)
    # The budget is for the whole set: what earlier runs paid counts (a row's cost is what its page's calls cost,
    # summed over runs; an answer replayed from the cache costs nothing).
    before = Decimal(str(total_spent(corpus)))
    before += other_spend(corpus)
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
            paid = metered()
            start = time.perf_counter()
            row: dict[str, Any] = {"id": record.id, "checked_at": now_iso(), "model": model}
            try:
                async with asyncio.timeout(settings.analysis_timeout_s * 3):
                    result = await graph.ainvoke(
                        {"page_content": corpus.read_page(record.id), "url": record.origin_url, "flags": []},
                    )
            except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
                row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            else:
                row |= result_row(result)
            page_cost = sum(paid, Decimal(0))
            earlier = results.get(record.id, {}).get("cost_eur", 0)
            row["cost_eur"] = float(page_cost) + earlier
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
        "spent_eur_with_new_findings": round(float(spent), 3),
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
    notes = {r.id: notes_of(r) for r in records}
    done = [results[r.id] for r in records if checked(results.get(r.id))]
    per_skill: Counter[str] = Counter()
    per_type: Counter[str] = Counter()
    for row in done:
        per_skill.update(row["fired"])
        per_type.update(f"{f['skill']}:{f['type']}" for f in row["new_findings"])
    by_pool: dict[str, Counter[str]] = {}
    for row in done:
        pool = notes[row["id"]].get("pool", "")
        by_pool.setdefault(pool, Counter())["checked"] += 1
        if row["fired"]:
            by_pool[pool]["fired"] += 1
    return {
        "pages": len(records),
        "per_pool": dict(Counter(n.get("pool", "") for n in notes.values())),
        "per_category": dict(Counter(n.get("category", "") for n in notes.values())),
        "per_marketplace": dict(Counter(n["marketplace"] for n in notes.values() if "marketplace" in n)),
        "checked": len(done),
        "errors": sum(1 for r in records if "error" in results.get(r.id, {})),
        "not_product": sum(1 for row in done if row["score"] < 0),
        "fired_any": sum(1 for row in done if row["fired"]),
        "fired_per_skill": dict(per_skill),
        "findings_per_type": dict(per_type),
        "fired_per_pool": {k: dict(v) for k, v in by_pool.items()},
        "claim_audit_unavailable": sum(1 for row in done if "claim_audit" in row["unavailable"]),
        "photo_claims_checked": sum(1 for row in done if row["features"].get("photo_claims.checked")),
        "spent_eur_total": round(sum(results[r.id].get("cost_eur", 0) for r in records if r.id in results), 3),
    }


RELABEL = "relabel"  # a queue reason's prefix: a labelled page that goes through the queue again
PINNED = "pinned"


def corpus_rows(corpus: Corpus) -> dict[str, dict[str, Any]]:
    """
    The new checks' results on the rest of the corpus (:mod:`evals.new_findings`), shaped like a candidate's row.

    :param corpus: The corpus.
    :return: Rows by page id, without a score (that replay runs the new checks only).
    """
    from evals import new_findings  # noqa: PLC0415 - it imports this module

    return {k: row | {"score": None} for k, row in new_findings.read_results(corpus).items() if "error" not in row}


def label_rows(corpus: Corpus) -> dict[str, dict[str, Any]]:
    """Every page's row for the labelling screen: a candidate's deep check, else the new checks' replay."""
    return corpus_rows(corpus) | read_results(corpus)


def _candidate_item(record: LabelRecord, row: dict[str, Any]) -> tuple[QueueItem, bool]:
    confidence = 1.0 if row.get("confidence") is None else float(row["confidence"])
    names = ", ".join(row["fired"])
    if row["fired"] and confidence < UNSURE:
        reason = f"{names} fired; scorer unsure ({confidence:.2f})"
        return QueueItem(id=record.id, reason=reason, priority=4 - confidence), True
    if row["fired"]:
        gap = 0.99 * (1 - row["score"] / 100)
        why = "says fine" if row["score"] < SURE_FINE else f"score {row['score']}"
        return QueueItem(id=record.id, reason=f"{names} fired; scorer {why}", priority=2 + gap), True
    return QueueItem(id=record.id, reason="random slice", priority=0.0), False


def _corpus_item(record: LabelRecord, row: dict[str, Any]) -> QueueItem | None:
    names = ", ".join(row["fired"])
    if record.label == "legit" and record.label_source != "human" and not is_llm(record.label_source) and row["fired"]:
        return QueueItem(id=record.id, reason=f"{RELABEL}: legit, {names} fired", priority=5.5)
    if record.label is None and row["fired"]:
        return QueueItem(id=record.id, reason=f"unlabelled, {names} fired", priority=1.5)
    if record.label is None:
        return QueueItem(id=record.id, reason="random slice (unlabelled)", priority=0.0)
    return None


def build_queue(corpus: Corpus, seed: int, random_share: int | None = None) -> list[QueueItem]:
    """
    Order the pages to label by value. Nothing is dropped unless ``random_share`` limits the last tier.

    - Pinned pages (10): pages from anywhere in the corpus whose findings a label should decide.
    - Relabels (5–6): ``legit`` pages (not labelled by a human yet) on which a new check fired in the corpus replay
      (:mod:`evals.new_findings`): their label is checked by hand; ``scam`` and ``junk`` labels stand as they are.
    - Candidates where a new check fired and the scorer is unsure (3–4), least sure first; then those where a new
      check fired but the scorer is sure (2–3), the lower the score the bigger the disagreement.
    - The corpus's other unlabelled product pages on which a new check fired (1–2).
    - The rest of the unlabelled pages, candidates and others, in a seeded random order (below 1).

    :param corpus: The corpus.
    :param seed: The random order's seed.
    :param random_share: Pages in the random tier at most (``None``: all of them).
    :return: The queue.
    """
    results = read_results(corpus)
    others = corpus_rows(corpus)
    items: list[QueueItem] = []
    rest: list[QueueItem] = []
    known = corpus.labels()
    for page, why in read_pins(corpus).items():
        if page in label_rows(corpus) and page in known and known[page].label_source != "human":
            items.append(QueueItem(id=page, reason=f"{PINNED}: {why}", priority=10.0))
    pinned = {i.id for i in items}
    for record in known.values():
        if record.id in pinned or notes_of(record).get("rejected"):
            continue
        if record.origin_dataset == DATASET:
            row = results.get(record.id)
            if record.label is not None or row is None or not checked(row) or row["score"] < 0:
                continue
            item, fired_here = _candidate_item(record, row)
            (items if fired_here else rest).append(item)
            continue
        if (row := others.get(record.id)) is not None and (found := _corpus_item(record, row)) is not None:
            (items if found.priority else rest).append(found)
    rng = random.Random(seed)
    rest.sort(key=lambda i: i.id)
    rng.shuffle(rest)
    kept = rest if random_share is None else rest[:random_share]
    for n, item in enumerate(kept):
        items.append(item.model_copy(update={"priority": 0.9 - 0.8 * n / max(1, len(kept))}))
    return sorted(items, key=lambda i: (-i.priority, i.id))


def read_queue(corpus: Corpus) -> list[QueueItem]:
    """The labelling queue, highest priority first."""
    path = corpus.root / QUEUE
    if not path.exists():
        return []
    return sorted(
        (QueueItem.model_validate_json(x) for x in path.read_text().splitlines() if x), key=lambda i: -i.priority
    )


def describe(
    record: LabelRecord,
    row: dict[str, Any],
    item: QueueItem,
    *,
    n: int,
    total: int,
    html: str,
    hidden: bool = False,
    llm: dict[str, Any] | None = None,
) -> str:
    """
    One page as a compact screen of text.

    :param record: The page's record.
    :param row: Its deep-check result.
    :param item: Its queue item.
    :param n: Its position in the batch.
    :param total: The batch's size.
    :param html: The page's HTML.
    :param hidden: Leave out the scorer's answer, the LLM's and why the page was queued (a blind page).
    :param llm: The LLM labeller's answer for the page (``llm_labels.jsonl``), shown unless ``hidden``.
    :return: The text.
    """
    facts, _ = parse_page(html, record.origin_url)
    notes = notes_of(record)
    text = textwrap.shorten(" ".join(clean_html(html).split()), _EXCERPT_CHARS, placeholder=" …")
    where = notes.get("marketplace") or "own shop"
    seller = notes.get("marketplace_seller") or facts.seller
    conf = row.get("confidence")
    lines = [
        f"── {n}/{total} " + "─" * 60,
        f"{record.id}  ·  {where}  ·  {notes.get('pool', '')}  ·  {notes.get('category', '')}  ·  "
        + ("blind: the scorer's answer is hidden on this page" if hidden else f"why: {item.reason}"),
        f"url: {record.origin_url}",
        f"title: {facts.title[:150]}",
        (
            f"price: {facts.price} {facts.currency} (was {facts.list_price})  ·  seller: {seller}  ·  "
            f"brand: {facts.brand}"
        ),
    ]
    if not hidden and row.get("score") is not None:
        sure = conf if conf is None else round(conf, 2)
        lines.append(f"scorer: {row['score']}  ·  {row.get('band', '')}  ·  confidence {sure}")
    if record.label:
        lines.insert(3, f"in the corpus as {record.label} ({record.label_source}, {record.origin_dataset})")
    if row.get("piece_count"):
        piece = row["piece_count"]
        lines.append(f"piece count: {json.dumps(piece, ensure_ascii=False)[:300]}")
    news = row.get("new_findings", [])
    shown = llm if llm and not hidden else None
    if shown:
        lines.append(f"LLM ({shown['model']}): {shown['label']} ({shown['confidence']:.2f}): {shown['reason'][:300]}")
    said = {f["evidence"]: f for f in shown["findings"]} if shown else {}
    lines.append(f"new findings ({len(news)}):" if news else "new findings: none")
    for i, f in enumerate(news, start=1):
        lines.append(f"  {i}. [{f['skill']}] {f['type']}: {f['evidence'][:300]}")
        if (verdict := said.get(f["evidence"])) is not None:
            lines.append(f"     LLM: {verdict['verdict']}: {verdict['why'][:150]}")
    others = [f for f in row.get("findings", []) if not is_new(f["skill"])][:4]
    if others:
        lines.append("other findings: " + "; ".join(f"{f['type']}" for f in others))
    lines += ["", textwrap.fill(text, 110), "", "[l]egit  [j]unk  [s]cam  [r]eject  [o]pen  [k] skip  [q]uit"]
    return "\n".join(lines)


def decide(corpus: Corpus, record: LabelRecord, label: str | None, *, hidden: bool) -> None:
    """
    Log one decision: the page, its new label (``None``: rejected), the label before, and whether it was blind.

    :param corpus: The corpus.
    :param record: The page as it was.
    :param label: The decision.
    :param hidden: Whether the scorer's answer was hidden.
    """
    row = {
        "id": record.id,
        "label": label,
        "before": record.label,
        "before_source": record.label_source,
        "blind": hidden,
        "decided_at": now_iso(),
    }
    with (corpus.root / DECISIONS).open("a") as f:
        f.write(json.dumps(row) + "\n")


def save_finding_labels(
    corpus: Corpus, record: LabelRecord, verdicts: list[tuple[dict[str, Any], str]], *, hidden: bool = False
) -> None:
    """
    Append the finding labels of one page (one record per finding).

    :param corpus: The corpus.
    :param record: The page.
    :param verdicts: Each finding and its verdict (correct, wrong, unsure).
    :param hidden: Whether the page was labelled blind.
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
                "blind": hidden,
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


def pages_to_label(corpus: Corpus, mode: str = "queue") -> list[QueueItem]:
    """
    The pages a labelling session serves, in order.

    - ``queue``: the labelling queue's pages without a human or LLM label (the reserved test set's pages included).
    - ``review``: the LLM labeller's review queue (:mod:`evals.llm_label`): its unsure pages, then a random audit
      sample; pages labelled by hand since drop out.
    - ``test``: the human-only test set (``deep_test_set.json``) pages not labelled by hand yet, in queue order.

    :param corpus: The corpus.
    :param mode: ``queue``, ``review`` or ``test``.
    :return: The queue items.
    """
    from evals import llm_label  # noqa: PLC0415 - it imports this module

    results = label_rows(corpus)
    known = corpus.labels()

    def usable(item: QueueItem) -> bool:
        return item.id in known and item.id in results and not notes_of(known[item.id]).get("rejected")

    if mode == "review":
        path = corpus.root / llm_label.REVIEW_QUEUE
        items = [QueueItem.model_validate_json(x) for x in path.read_text().splitlines() if x] if path.exists() else []
        return [i for i in items if usable(i) and is_llm(known[i.id].label_source)]
    queue = [i for i in read_queue(corpus) if usable(i)]
    if mode == "test":
        path = corpus.root / llm_label.TEST_SET
        reserved = set(json.loads(path.read_text())["ids"]) if path.exists() else set()
        return [i for i in queue if i.id in reserved and known[i.id].label_source != "human"]
    return [
        i
        for i in queue
        if known[i.id].label is None
        or (
            i.reason.startswith((PINNED, RELABEL))
            and known[i.id].label_source != "human"
            and not is_llm(known[i.id].label_source)
        )
    ]


def label(corpus: Corpus, batch: int, mode: str = "queue") -> None:
    """
    Label the next ``batch`` pages, one keypress per page (and one per new finding).

    :param corpus: The corpus.
    :param batch: Pages in this batch.
    :param mode: Which pages (:func:`pages_to_label`): ``queue``, ``review`` (the LLM's labels to check) or
        ``test`` (the human-only test set).
    """
    from evals import llm_label  # noqa: PLC0415 - it imports this module

    results = label_rows(corpus)
    queue = pages_to_label(corpus, mode)
    answers = {r["id"]: r for r in llm_label.read_jsonl(corpus.root / llm_label.LLM_LABELS)}
    if not queue:
        hint = {
            "review": "run `uv run python -m evals.llm_label label` (or `review-queue`) first",
            "test": "the test set is labelled, or not reserved yet (`evals.llm_label label`)",
        }.get(mode, "run `make deep-queue` after `make deep-check`")
        print(f"Nothing to label: {hint}.")  # noqa: T201 - interactive tool
        return
    todo = queue[:batch]
    print(f"{len(queue)} page(s) in the queue; this batch: {len(todo)}.")  # noqa: T201 - interactive tool
    tally: Counter[str] = Counter()
    for n, item in enumerate(todo, start=1):
        record = corpus.labels()[item.id]
        row = results[item.id]
        # An audit page of the LLM's review queue is blind (unanchored by the LLM's answer), as is one page in five.
        hidden = blind(record.id) or item.reason.startswith(llm_label.AUDIT)
        llm = answers.get(record.id) if mode == "review" else None
        screen = describe(
            record, row, item, n=n, total=len(todo), html=corpus.read_page(record.id), hidden=hidden, llm=llm
        )
        print("\n" + screen)  # noqa: T201 - interactive tool
        while True:
            key = read_key().lower()
            if key != "o":
                break
            subprocess.Popen(["xdg-open", record.origin_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603, S607
        if key == "q":
            break
        if key == "r":
            notes = notes_of(record) | {"rejected": True}
            corpus.update([record.model_copy(update={"notes": json.dumps(notes, sort_keys=True)})])
            decide(corpus, record, None, hidden=hidden)
            tally["rejected"] += 1
            print("→ rejected")  # noqa: T201 - interactive tool
            continue
        if key not in PAGE_KEYS:
            tally["skipped"] += 1
            continue
        page_label = PAGE_KEYS[key]
        print(f"→ {page_label}")  # noqa: T201 - interactive tool
        if row.get("new_findings"):
            save_finding_labels(corpus, record, ask_findings(row["new_findings"]), hidden=hidden)
        decide(corpus, record, page_label, hidden=hidden)
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
    run.add_argument("--budget-eur", type=Decimal, default=Decimal(55))
    run.add_argument("--concurrency", type=int, default=3)
    run.add_argument("--rerun", action="store_true", help="check pages with a result again (new checks)")
    run.add_argument("--pin", nargs="+", default=[], metavar="ID", help="check these corpus pages, first in the queue")
    run.add_argument("--why", default="", help="what the pinned pages' labels should decide")
    queue = sub.add_parser("queue", help="order the checked pages for labelling")
    queue.add_argument("--seed", type=int, default=7)
    queue.add_argument("--random", type=int, default=None, help="pages in the random tier at most (default: all)")
    lab = sub.add_parser("label", help="label the next batch")
    lab.add_argument("--batch", type=int, default=int(os.getenv("BATCH", "50")))
    lab.add_argument(
        "--review", dest="mode", action="store_const", const="review", default="queue", help="the LLM's review queue"
    )
    lab.add_argument("--test-set", dest="mode", action="store_const", const="test", help="the human-only test set")
    sub.add_parser("summary", help="print the set's numbers")
    args = parser.parse_args()
    corpus = Corpus()
    if args.command == "check":
        if args.pin:
            pin(corpus, args.pin, args.why)
        summary = asyncio.run(
            check(
                corpus,
                limit=args.limit,
                budget=args.budget_eur,
                concurrency=args.concurrency,
                rerun=args.rerun,
                ids=args.pin or None,
            )
        )
        print(json.dumps(summary, indent=1))  # noqa: T201 - command-line report
    elif args.command == "queue":
        items = build_queue(corpus, args.seed, args.random)
        (corpus.root / QUEUE).write_text("".join(i.model_dump_json() + "\n" for i in items))
        tiers = Counter(
            "fired, unsure" if i.priority >= 3 else "fired" if i.priority >= 2 else "random" for i in items
        )
        print(f"{len(items)} pages queued: {dict(tiers)}")  # noqa: T201 - command-line report
    elif args.command == "label":
        label(corpus, args.batch, args.mode)
    else:
        print(json.dumps(summarise(corpus, read_results(corpus)), indent=1))  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
