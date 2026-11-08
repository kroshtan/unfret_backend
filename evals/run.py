"""
Run the analysis graph over the labelled corpus and report how well it separates unreliable from legit pages.

Usage::

    uv run python -m evals.run [--limit N] [--split test|train|all] [--record | --replay] [--skills ...] [--with-url]

``--record`` (the default) calls the LLM on cache misses and stores every response in the corpus's
``llm_cache.sqlite``; ``--replay`` serves only cached responses and fails any page that would need a new call, so
re-scoring a recorded run costs nothing. Reports go to ``<corpus>/reports/``.

Network lookups are off: domain age and VIES would look up today's registry data for pages captured years ago,
which tells the model whether a scam domain has since died rather than anything about the page, and reverse image
search is paid per search. URLs are withheld for the same reason unless ``--with-url`` is given. Embeddings are
recorded and replayed like LLM responses. Each page's skill features are written next to the report, for training
the scorer.
"""

import argparse
import asyncio
import hashlib
import json
import time
from collections import defaultdict
from datetime import UTC, datetime

import structlog
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.globals import set_llm_cache
from pydantic import SecretStr

from evals.corpus import Corpus, LabelRecord, dump_json
from evals.embedding_cache import CachedEmbeddings
from evals.llm_cache import SQLiteLLMCache
from evals.metrics import Outcome, summarise, to_markdown
from veridex import llm
from veridex.config import settings
from veridex.graph import SKILLS, build_graph

logger = structlog.get_logger("veridex.evals")

DEFAULT_EXCLUDED = ("reverse_image",)


def select(records: list[LabelRecord], limit: int | None) -> list[LabelRecord]:
    """
    Pick up to ``limit`` labelled records, balanced over (label, source) groups and stable between runs.

    :param records: Labelled records.
    :param limit: Maximum number to return, or ``None`` for all.
    :return: The selection, interleaved round-robin over the groups.
    """
    groups: dict[tuple[str, str], list[LabelRecord]] = defaultdict(list)
    for r in sorted(records, key=lambda r: hashlib.sha256(r.id.encode()).hexdigest()):
        groups[(r.label or "", r.source)].append(r)
    picked: list[LabelRecord] = []
    queues = [groups[k] for k in sorted(groups)]
    while queues and (limit is None or len(picked) < limit):
        for q in list(queues):
            if not q:
                queues.remove(q)
                continue
            picked.append(q.pop(0))
            if limit is not None and len(picked) >= limit:
                break
    return picked


async def evaluate(
    corpus: Corpus,
    records: list[LabelRecord],
    skill_names: list[str],
    with_url: bool,
    concurrency: int,
    *,
    features: dict[str, dict[str, float]] | None = None,
) -> list[Outcome]:
    """
    Run the graph on each record's page.

    :param corpus: The corpus holding the pages.
    :param records: The records to evaluate.
    :param skill_names: Names of the skills to enable.
    :param with_url: Pass the page's original URL to the graph.
    :param concurrency: Pages evaluated at the same time.
    :param features: If given, filled with each scored page's skill features, by record id.
    :return: One outcome per record.
    """
    graph = build_graph([s for s in SKILLS if s.name in skill_names])
    semaphore = asyncio.Semaphore(concurrency)

    async def one(record: LabelRecord) -> Outcome:
        assert record.label is not None
        usage = UsageMetadataCallbackHandler()
        async with semaphore:
            start = time.perf_counter()
            try:
                async with asyncio.timeout(settings.analysis_timeout_s * 2):
                    result = await graph.ainvoke(
                        {
                            "page_content": corpus.read_page(record.id),
                            "url": record.origin_url if with_url else "",
                            "flags": [],
                        },
                        config={"callbacks": [usage]},
                    )
            except Exception as exc:  # noqa: BLE001
                return Outcome(
                    id=record.id,
                    label=record.label,
                    score=None,
                    expected_page_type=record.page_type,
                    error=f"{type(exc).__name__}: {str(exc)[:160]}",
                )
        if features is not None:
            features[record.id] = dict(result.get("features", {}))
        tokens = list(usage.usage_metadata.values())
        unavailable = sum(1 for r in result.get("skill_results", []) if r.endswith("Skill unavailable."))
        return Outcome(
            id=record.id,
            label=record.label,
            score=int(result["score"]),
            expected_page_type=record.page_type,
            predicted_page_type=str(result.get("page_type", "")),
            latency_s=round(time.perf_counter() - start, 2),
            input_tokens=sum(t.get("input_tokens", 0) for t in tokens),
            output_tokens=sum(t.get("output_tokens", 0) for t in tokens),
            note=f"{unavailable} skill(s) unavailable" if unavailable else "",
        )

    outcomes: list[Outcome] = []
    for i, outcome in enumerate(asyncio.as_completed([one(r) for r in records]), start=1):
        outcomes.append(await outcome)
        if i % 10 == 0:
            logger.info("eval_progress", done=i, total=len(records))
    return outcomes


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record", action="store_true", help="call the LLM on cache misses (default)")
    mode.add_argument("--replay", action="store_true", help="only use cached LLM responses")
    parser.add_argument("--skills", nargs="+", default=[s.name for s in SKILLS if s.name not in DEFAULT_EXCLUDED])
    parser.add_argument("--with-url", action="store_true")
    parser.add_argument("--split", choices=["test", "train", "all"], default="test", help="which split to score")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()

    corpus = Corpus()
    set_llm_cache(SQLiteLLMCache(corpus.root / "llm_cache.sqlite", strict=args.replay))
    settings.network_lookups = False
    # Each page fans out to several parallel LLM calls; let the client back off on rate limits (429) instead of
    # turning them into "skill unavailable" findings or failed pages.
    settings.llm_max_retries = max(settings.llm_max_retries, 6)
    llm.get_llm.cache_clear()
    if args.replay and not settings.llm_api_key.get_secret_value():
        # The client refuses to start without a key; in replay mode it never sends one.
        settings.llm_api_key = SecretStr("replay")
        llm.get_llm.cache_clear()
    llm.use_embeddings(
        CachedEmbeddings(
            llm.get_embeddings(), settings.embedding_model, corpus.root / "llm_cache.sqlite", strict=args.replay
        )
    )

    labelled = [r for r in corpus.labels().values() if r.label and args.split in {"all", r.split}]
    records = select(labelled, args.limit)
    logger.info("eval_start", pages=len(records), skills=args.skills, replay=args.replay)
    page_features: dict[str, dict[str, float]] = {}
    outcomes = asyncio.run(
        evaluate(corpus, records, args.skills, args.with_url, args.concurrency, features=page_features)
    )

    report = summarise(outcomes)
    report["skills"] = args.skills
    report["mode"] = "replay" if args.replay else "record"
    report["split"] = args.split
    report["model"] = settings.llm_model
    report["pages_with_unavailable_skills"] = [{"id": o.id, "note": o.note} for o in outcomes if o.note]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    reports = corpus.root / "reports"
    reports.mkdir(exist_ok=True)
    dump_json(reports / f"{stamp}.json", report)
    markdown = to_markdown(report, f"Veridex evaluation {stamp} ({report['mode']}, {settings.llm_model})")
    (reports / f"{stamp}.md").write_text(markdown)
    (reports / "latest.md").write_text(markdown)
    (reports / f"{stamp}-outcomes.jsonl").write_text("".join(json.dumps(o.__dict__) + "\n" for o in outcomes))
    (reports / f"{stamp}-features.jsonl").write_text(
        "".join(json.dumps({"id": i, "features": f}) + "\n" for i, f in sorted(page_features.items()))
    )
    print(markdown)  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
