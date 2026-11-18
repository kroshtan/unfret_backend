"""
An independent LLM second opinion on every corpus page, and the review queue built from it.

A stronger model than the pipeline's own (``UNFRET_LABELLER_MODEL``) reads each page with a written rubric and
gives a label, a confidence and a reason. Then:

- **Agreement** (both unreliable, or both legit) upgrades a dataset or rule label to ``agreed``.
- **Disagreement** on a *dataset* label keeps the dataset label (it came from people), notes the dispute, and
  queues the case; on a *rule* label the page is withheld from evaluation until a human decides.
- **Unlabelled** pages with a confident LLM opinion are queued so one keypress can add them.

The queue is capped (``--queue-size``) and ranked so the most informative cases come first.

The new page classes (``p2p_listing``, ``service_site``, ``service_listing``) are stricter, because nobody labels
them by hand: each class has its own rubric, a page keeps its source label only when the second opinion confidently
agrees, and any other outcome (disagreement, unsure, not confident) **drops** the label with the reason in
``notes``. Nothing of theirs goes to the review queue. The same strict rule holds for the product datasets in
:data:`STRICT_DATASETS`, added to balance the corpus without anyone reviewing them.

Usage: ``uv run python -m evals.second_opinion [--limit N] [--queue-size 25] [--class p2p_listing]
[--dataset us-established-shops]``.
"""

import argparse
import asyncio
from collections import Counter, defaultdict
from typing import cast

import structlog
from langchain_core.globals import set_llm_cache
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from evals.config import eval_settings
from evals.corpus import (
    NEW_CLASSES,
    PAGE_CLASSES,
    PRODUCT_CLASS,
    UNRELIABLE,
    Corpus,
    LabelRecord,
    QueueItem,
    dump_json,
    is_product,
    page_class,
)
from evals.llm_cache import SQLiteLLMCache
from unfret.config import settings
from unfret.html_cleaner import clean_html
from unfret.reports.second_opinion import RUBRIC, RUBRICS, Opinion

logger = structlog.get_logger("unfret.evals")

_MAX_CHARS = 7_000
_CONFIDENT = 0.75
# Product datasets whose labels nobody reviews by hand: kept only when the second opinion confidently agrees, like the
# new page classes (evals.sources.us_shops, evals.sources.coi_shops).
STRICT_DATASETS: frozenset[str] = frozenset({"us-established-shops", "coi-risky-eshops"})


def unreliable(label: str | None) -> bool | None:
    """``True`` for junk or scam, ``False`` for legit, ``None`` for no or unsure label."""
    if label in UNRELIABLE:
        return True
    if label == "legit":
        return False
    return None


def reconcile(record: LabelRecord) -> tuple[LabelRecord, QueueItem | None]:
    """
    Combine a record's prior label with its LLM opinion.

    :param record: A record with ``llm_label`` set (``None`` meaning unsure).
    :return: The updated record, and a queue item if a human should decide.
    """
    if page_class(record) in NEW_CLASSES or record.origin_dataset in STRICT_DATASETS:
        return reconcile_strict(record), None
    prior = record.prior_label
    llm_side = unreliable(record.llm_label) if record.llm_confidence >= _CONFIDENT else None
    prior_side = unreliable(prior)
    update: dict[str, object] = {}
    item: QueueItem | None = None

    if prior is None:
        if llm_side is not None:
            item = QueueItem(
                id=record.id,
                reason=f"unlabelled; LLM says {record.llm_label} ({record.llm_confidence:.2f}): {record.llm_reason}",
                priority=0.5 * record.llm_confidence,
            )
    elif llm_side is None:
        pass  # LLM unsure: keep the prior label as it is
    elif llm_side == prior_side:
        update = {"label_source": "agreed", "confidence": max(record.confidence, record.llm_confidence)}
    else:
        dispute = f"{record.prior_label} ({record.origin_dataset}) vs LLM {record.llm_label}: {record.llm_reason}"
        priority = record.llm_confidence * (1.0 if record.source == "synthetic" else 0.8)
        item = QueueItem(id=record.id, reason=f"disagreement: {dispute}", priority=priority)
        if record.label_source == "rule":
            update = {"label": None, "label_source": None, "notes": f"withheld: {dispute} | {record.notes}"}
    if record.label_source == "human":
        update, item = {}, None
    return record.model_copy(update=update), item


_DROPPED = "dropped: "


def reconcile_strict(record: LabelRecord) -> LabelRecord:
    """
    Keep a strict page's label only when the second opinion confidently agrees; drop it otherwise.

    Strict pages are the new page classes and the product pages of :data:`STRICT_DATASETS`.

    Nobody labels these classes by hand, so a dispute is never queued: the label goes (``label=None``) and the
    reason is kept in ``notes`` so the drop can be counted and reconsidered after a rubric change (``--reset``).

    :param record: A new-class record with an LLM opinion.
    :return: The record, ``agreed`` or dropped.
    """
    if record.label_source == "human" or record.prior_label is None or record.notes.startswith(_DROPPED):
        return record
    llm_side = unreliable(record.llm_label) if record.llm_confidence >= _CONFIDENT else None
    if llm_side is not None and llm_side == unreliable(record.prior_label):
        return record.model_copy(
            update={"label_source": "agreed", "confidence": max(record.confidence, record.llm_confidence)}
        )
    if llm_side is None:
        why = f"LLM {record.llm_label or 'unsure'} ({record.llm_confidence:.2f}), not confident"
    else:
        why = f"{record.prior_label} ({record.origin_dataset}) vs LLM {record.llm_label} ({record.llm_confidence:.2f})"
    reason = f"{why}: {record.llm_reason}"
    return record.model_copy(
        update={"label": None, "label_source": None, "split": None, "notes": f"{_DROPPED}{reason} | {record.notes}"}
    )


def reset(record: LabelRecord) -> LabelRecord:
    """
    Drop a record's LLM opinion and undo its reconciliation, restoring the dataset or rule label.

    :param record: The record.
    :return: The record as it was before any second opinion (human labels are kept).
    """
    if record.label_source == "human":
        return record.model_copy(update={"llm_label": None, "llm_confidence": 0.0, "llm_reason": ""})
    source = None
    if record.prior_label is not None:
        source = "dataset" if record.source in {"dataset", "wayback"} else "rule"
        if page_class(record) in NEW_CLASSES:
            source = "rule" if record.source == "synthetic" else "dataset"
    dropped = record.notes.startswith(("withheld:", _DROPPED))
    notes = record.notes.split(" | ", 1)[1] if dropped and " | " in record.notes else record.notes
    return record.model_copy(
        update={
            "label": record.prior_label,
            "label_source": source,
            "notes": notes,
            "llm_label": None,
            "llm_confidence": 0.0,
            "llm_reason": "",
        }
    )


async def collect_opinions(corpus: Corpus, records: list[LabelRecord], concurrency: int) -> list[LabelRecord]:
    """
    Ask the labeller model about every record that has no opinion yet.

    :param corpus: The corpus holding the pages.
    :param records: Records to label.
    :param concurrency: Calls in flight at the same time.
    :return: The records with ``llm_*`` fields filled in (records that failed are returned unchanged).
    """
    model = ChatOpenAI(
        model=eval_settings.labeller_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0,
        timeout=120,
        max_retries=2,
    ).with_structured_output(Opinion, method="function_calling")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(record: LabelRecord) -> LabelRecord:
        text = clean_html(corpus.read_page(record.id))[:_MAX_CHARS]
        rubric = RUBRICS.get(page_class(record), RUBRIC)
        own = "own shop" if rubric is RUBRIC else "own website"
        context = f"Platform: {record.platform or own}\n\nPage text:\n{text}"
        async with semaphore:
            try:
                opinion = cast(
                    Opinion, await model.ainvoke([SystemMessage(content=rubric), HumanMessage(content=context)])
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("opinion_failed", id=record.id, error=type(exc).__name__)
                return record
        return record.model_copy(
            update={
                "llm_label": None if opinion.label == "unsure" else opinion.label,
                "llm_confidence": opinion.confidence,
                "llm_reason": opinion.reason[:300],
            }
        )

    done: list[LabelRecord] = []
    for i, task in enumerate(asyncio.as_completed([one(r) for r in records]), start=1):
        done.append(await task)
        if i % 25 == 0:
            corpus.update(done[-25:])
            logger.info("opinion_progress", done=i, total=len(records))
    corpus.update(done)
    return done


def agreement_report(records: list[LabelRecord]) -> dict[str, dict[str, float]]:
    """
    Binary agreement between prior labels and confident LLM opinions, per origin dataset.

    :param records: Records with priors and opinions.
    :return: Per dataset: compared count, agreement rate, and how often the LLM was unsure or not confident.
    """
    stats: dict[str, Counter[str]] = defaultdict(Counter)
    for r in records:
        if r.prior_label is None or not r.llm_reason:
            continue
        s = stats[r.origin_dataset]
        llm_side = unreliable(r.llm_label) if r.llm_confidence >= _CONFIDENT else None
        if llm_side is None:
            s["unsure"] += 1
            continue
        s["compared"] += 1
        s["agree"] += llm_side == unreliable(r.prior_label)
    return {
        dataset: {
            "compared": s["compared"],
            "agreement": round(s["agree"] / s["compared"], 3) if s["compared"] else 0.0,
            "llm_unsure_or_unconfident": s["unsure"],
        }
        for dataset, s in sorted(stats.items())
    }


def class_report(records: list[LabelRecord], requested: int) -> dict[str, object]:
    """
    Kept and dropped counts of one new page class or strict dataset, per source label, source and origin dataset.

    :param records: The class's records.
    :param requested: Opinions asked for in this run (LLM calls, cache hits included).
    :return: The report.
    """
    kept: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    waiting: Counter[str] = Counter()
    for r in records:
        key = f"{r.prior_label}/{r.source}/{r.origin_dataset}"
        if r.label is not None and r.label_source == "agreed":
            kept[key] += 1
        elif r.notes.startswith(_DROPPED):
            dropped[key] += 1
        else:
            waiting[key] += 1
    return {
        "labeller_model": eval_settings.labeller_model,
        "opinions_requested_this_run": requested,
        "kept": dict(sorted(kept.items())),
        "dropped": dict(sorted(dropped.items())),
        "without_opinion": dict(sorted(waiting.items())),
        "dropped_reasons": [
            {"id": r.id, "reason": r.notes.removeprefix(_DROPPED).split(" | ", 1)[0][:300]}
            for r in sorted(records, key=lambda r: r.id)
            if r.notes.startswith(_DROPPED)
        ],
    }


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--queue-size", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument(
        "--class", dest="page_class", choices=PAGE_CLASSES, default=None, help="only ask about pages of this class"
    )
    parser.add_argument("--dataset", default=None, help="only ask about pages of this origin dataset")
    parser.add_argument(
        "--reset", action="store_true", help="discard all LLM opinions first (e.g. after a rubric change)"
    )
    args = parser.parse_args()

    corpus = Corpus()
    if args.reset:
        corpus.update(reset(r) for r in corpus.labels().values())
    set_llm_cache(SQLiteLLMCache(corpus.root / "llm_cache.sqlite"))
    pending = [
        r
        for r in corpus.labels().values()
        if not r.llm_reason
        and r.label_source != "human"
        and args.page_class in {None, page_class(r)}
        and args.dataset in {None, r.origin_dataset}
    ][: args.limit]
    logger.info("opinion_start", pending=len(pending), model=eval_settings.labeller_model)
    asyncio.run(collect_opinions(corpus, pending, args.concurrency))

    records = list(corpus.labels().values())
    reconciled = [reconcile(r) for r in records if r.llm_reason]
    corpus.update(r for r, _ in reconciled)
    items = sorted((i for _, i in reconciled if i is not None), key=lambda i: -i.priority)
    corpus.write_queue(items[: args.queue_size])

    # The product report covers product pages only, as before the new classes; they get a report each.
    agreement = agreement_report([r for r in records if is_product(r)])
    report = {
        "labeller_model": eval_settings.labeller_model,
        "agreement": agreement,
        "queue_candidates": len(items),
        "queued": min(len(items), args.queue_size),
        "label_sources": dict(Counter(str(r.label_source) for r in corpus.labels().values() if is_product(r))),
    }
    (corpus.root / "reports").mkdir(exist_ok=True)
    if args.page_class in {None, PRODUCT_CLASS}:
        dump_json(corpus.root / "reports" / "second_opinion.json", report)
    for cls in NEW_CLASSES:
        members = [r for r in corpus.labels().values() if page_class(r) == cls]
        if members and args.page_class in {None, cls} and args.dataset is None:
            out = corpus.root / "reports" / cls
            out.mkdir(exist_ok=True)
            asked = sum(1 for r in pending if page_class(r) == cls)
            dump_json(out / "second_opinion.json", class_report(members, asked))
    for dataset in sorted(STRICT_DATASETS):
        members = [r for r in corpus.labels().values() if r.origin_dataset == dataset]
        if members and args.dataset in {None, dataset} and args.page_class in {None, PRODUCT_CLASS}:
            out = corpus.root / "reports" / dataset
            out.mkdir(exist_ok=True)
            asked = sum(1 for r in pending if r.origin_dataset == dataset)
            dump_json(out / "second_opinion.json", class_report(members, asked))
    logger.info("opinion_done", **{k: v for k, v in report.items() if k != "agreement"})
    for dataset, a in agreement.items():
        logger.info("agreement", dataset=dataset, **a)


if __name__ == "__main__":
    main()
