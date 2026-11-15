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
``notes``. Nothing of theirs goes to the review queue.

Usage: ``uv run python -m evals.second_opinion [--limit N] [--queue-size 25] [--class p2p_listing]``.
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

logger = structlog.get_logger("unfret.evals")

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

P2P_RUBRIC = """\
You label classified ads by private sellers (Marktplaats, 2dehands, eBay, Vinted, Craigslist, Facebook Marketplace)
for a consumer-protection test set. Read the ad page and choose ONE label:

- scam: the ad is likely a fraud: the buyer would pay and get nothing, or be phished. Typical signs: moving the
  deal off the platform (WhatsApp, e-mail, a phone number to text), payment in advance by bank transfer, gift
  cards, crypto, wire or "friends and family", a payment link sent by the seller, shipping only because the seller
  is "abroad", a price far below what the item sells for, a brand-new account with no reviews selling sought-after
  goods, a rental with a deposit before any viewing or keys sent by post, pressure to decide today.
- legit: an ordinary ad by a genuine private seller: a plausible price, specific honest details (including flaws),
  pickup or the platform's own payment and buyer protection, a seller with a history on the platform.
- unsure: the page does not give enough to decide.

Do not use "junk" for ads. A single weak sign (a new account alone, a low price alone) is not enough for scam.
Judge only from the page. Prefer "unsure" over guessing; confidence must reflect real certainty (0-1).\
"""

SERVICE_RUBRIC = """\
You label pages of service providers (plumbers, locksmiths, electricians, cleaners, removals, roofers, pest control
...) for a consumer-protection test set: a company's own website, or its profile on a platform such as Werkspot,
Thumbtack or Angi. Read the page and choose ONE label:

- scam: likely a rogue or fake trader. Typical signs: a fake "local" business that is really one call centre
  behind many city pages ("Locksmith <city>", the same national or premium number everywhere), no verifiable
  company identity (no address, no KvK or licence number where one would be expected, or an identity that does
  not fit), bait prices ("call-out from 0", "$19 service call") with vague real rates, cash or payment up front,
  "there within 15 minutes, 24/7, everywhere", generic or templated reviews, stock photos.
- legit: a genuine business with its own identity: a real address and history, a company or licence number,
  clear prices or written quotes, a plausible service area, specific work and reviews.
- unsure: the page does not give enough to decide.

Do not use "junk" for service providers. Missing one detail (no street address for a mobile trade) is not enough
for scam. Judge only from the page. Prefer "unsure" over guessing; confidence must reflect real certainty (0-1).\
"""

RUBRICS: dict[str, str] = {
    "p2p_listing": P2P_RUBRIC,
    "service_site": SERVICE_RUBRIC,
    "service_listing": SERVICE_RUBRIC,
}


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
    if page_class(record) in NEW_CLASSES:
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
    Keep a new-class page's source label only when the second opinion confidently agrees; drop it otherwise.

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
    Kept and dropped counts of one new page class, per source label, source and origin dataset.

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
        if not r.llm_reason and r.label_source != "human" and args.page_class in {None, page_class(r)}
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
        if members and args.page_class in {None, cls}:
            out = corpus.root / "reports" / cls
            out.mkdir(exist_ok=True)
            asked = sum(1 for r in pending if page_class(r) == cls)
            dump_json(out / "second_opinion.json", class_report(members, asked))
    logger.info("opinion_done", **{k: v for k, v in report.items() if k != "agreement"})
    for dataset, a in agreement.items():
        logger.info("agreement", dataset=dataset, **a)


if __name__ == "__main__":
    main()
