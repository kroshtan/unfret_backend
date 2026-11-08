"""
An independent LLM second opinion on every corpus page, and the review queue built from it.

A stronger model than the pipeline's own (``VERIDEX_LABELLER_MODEL``) reads each page with a written rubric and
gives a label, a confidence and a reason. Then:

- **Agreement** (both unreliable, or both legit) upgrades a dataset or rule label to ``agreed``.
- **Disagreement** on a *dataset* label keeps the dataset label (it came from people), notes the dispute, and
  queues the case; on a *rule* label the page is withheld from evaluation until a human decides.
- **Unlabelled** pages with a confident LLM opinion are queued so one keypress can add them.

The queue is capped (``--queue-size``) and ranked so the most informative cases come first.

Usage: ``uv run python -m evals.second_opinion [--limit N] [--queue-size 25]``.
"""

import argparse
import asyncio
from collections import Counter, defaultdict
from typing import Literal, cast

import structlog
from langchain_core.globals import set_llm_cache
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from evals.config import eval_settings
from evals.corpus import UNRELIABLE, Corpus, LabelRecord, QueueItem, dump_json
from evals.llm_cache import SQLiteLLMCache
from veridex.config import settings
from veridex.html_cleaner import clean_html

logger = structlog.get_logger("veridex.evals")

_MAX_CHARS = 7_000
_CONFIDENT = 0.75

RUBRIC = """\
You label e-commerce pages for a consumer-protection test set. Read the page text and choose ONE label:

- scam: the shop is likely to take money without delivering what it promises. Typical signs: a fake or cloned
  shop, big-brand goods at impossible discounts, no verifiable company identity or address, copied or nonsensical
  policies, pressure tactics, a mismatch between the shop name and what it sells, payment via unusual channels.
- junk: generic, mass-produced goods of the kind sold on Temu or AliExpress, presented as if they were a brand
  product. The goods probably exist and ship, but the seller adds nothing: a meaningless or machine-generated
  brand name (random capitals, pinyin-like strings), keyword-stuffed titles, interchangeable white-label items,
  often with exaggerated claims or ratings and reviews that look bought or templated. Proof of deception is NOT
  required: an unknown generated brand selling generic marketplace goods is junk even with decent reviews.
- legit: an established brand or retailer, or a genuine independent maker or shop with its own identity,
  selling what it says at a plausible price.
- unsure: the page does not give enough to decide.

Judge only from the page. Prefer "unsure" over guessing; confidence must reflect real certainty (0-1).\
"""


class Opinion(BaseModel):
    """The labeller's verdict on one page."""

    label: Literal["legit", "junk", "scam", "unsure"] = Field(description="The single best label.")
    confidence: float = Field(ge=0, le=1, description="Certainty in the label, 0-1.")
    reason: str = Field(description="One sentence citing the decisive evidence on the page.")


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
    notes = record.notes.split(" | ", 1)[1] if record.notes.startswith("withheld:") else record.notes
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
        context = f"Platform: {record.platform or 'own shop'}\n\nPage text:\n{text}"
        async with semaphore:
            try:
                opinion = cast(
                    Opinion, await model.ainvoke([SystemMessage(content=RUBRIC), HumanMessage(content=context)])
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


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--queue-size", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument(
        "--reset", action="store_true", help="discard all LLM opinions first (e.g. after a rubric change)"
    )
    args = parser.parse_args()

    corpus = Corpus()
    if args.reset:
        corpus.update(reset(r) for r in corpus.labels().values())
    set_llm_cache(SQLiteLLMCache(corpus.root / "llm_cache.sqlite"))
    pending = [r for r in corpus.labels().values() if not r.llm_reason and r.label_source != "human"][: args.limit]
    logger.info("opinion_start", pending=len(pending), model=eval_settings.labeller_model)
    asyncio.run(collect_opinions(corpus, pending, args.concurrency))

    records = list(corpus.labels().values())
    reconciled = [reconcile(r) for r in records if r.llm_reason]
    corpus.update(r for r, _ in reconciled)
    items = sorted((i for _, i in reconciled if i is not None), key=lambda i: -i.priority)
    corpus.write_queue(items[: args.queue_size])

    agreement = agreement_report(records)
    report = {
        "labeller_model": eval_settings.labeller_model,
        "agreement": agreement,
        "queue_candidates": len(items),
        "queued": min(len(items), args.queue_size),
        "label_sources": dict(Counter(str(r.label_source) for r in corpus.labels().values())),
    }
    (corpus.root / "reports").mkdir(exist_ok=True)
    dump_json(corpus.root / "reports" / "second_opinion.json", report)
    logger.info("opinion_done", **{k: v for k, v in report.items() if k != "agreement"})
    for dataset, a in agreement.items():
        logger.info("agreement", dataset=dataset, **a)


if __name__ == "__main__":
    main()
