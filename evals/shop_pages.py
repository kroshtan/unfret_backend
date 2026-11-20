"""
The claim audit across a shop's own pages, measured: record the pages, then compare the deep check with and without.

Usage::

    uv run python -m evals.shop_pages collect [--junk 40]
    uv run python -m evals.shop_pages compare [--junk 40] [--replay]

``collect`` reads the shop pages (:mod:`unfret.shop_pages`: returns, shipping, about, contact, …) of the golden set's
web shops and of a seeded sample of junk and scam web shops, and keeps them in ``<corpus>/cache/shop_pages/<id>.json``
so a comparison is reproducible. A page collected live (the golden set's live pages) is read live, where
``robots.txt`` lets Unfret in, timed as a deep check would fetch it; an archived page's shop pages are read from the
Internet Archive, the capture closest to the page's own (within :data:`MAX_CAPTURE_GAP_DAYS`: a scam's domain is
often someone else's later), about one request a second.

``compare`` runs the deep tier on those pages twice, as :mod:`evals.golden` does (recorded LLM answers, the URL
given, no other network lookups): once on the product page alone, once with the recorded shop pages
(:func:`unfret.shop_pages.use_source`), and reports per page the band, the claim audit's findings that quote the
shop's other pages, and the audit's tokens and cost. The report goes to ``<corpus>/reports/shop-pages-<stamp>.json``
and ``.md``; it names the sellers, so it stays in the corpus.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from evals.corpus import Corpus, LabelRecord, dump_json
from evals.golden import run_tier
from evals.run import configure
from evals.sources import wayback
from evals.sources._http import PoliteClient
from evals.sources.eu_shops import robots_allows
from evals.sources.golden import DATASET
from unfret import shop_pages
from unfret.config import settings
from unfret.facts import parse_page
from unfret.graph.skills import claim_audit
from unfret.net import fetch_public
from unfret.page_fetch import is_challenge_page

logger = structlog.get_logger("unfret.evals")

MAX_CAPTURE_GAP_DAYS = 365
_SEED = "unfret-shop-pages"


def _cache(corpus: Corpus) -> Path:
    path = corpus.root / "cache" / "shop_pages"
    path.mkdir(parents=True, exist_ok=True)
    return path


def select(corpus: Corpus, junk: int) -> list[LabelRecord]:
    """
    The pages measured: the golden set, and a seeded sample of junk and scam web shops with their URL.

    :param corpus: The corpus.
    :param junk: How many junk and scam web shops.
    :return: The records, golden first.
    """
    labels = corpus.labels().values()
    golden = sorted((r for r in labels if r.origin_dataset == DATASET and r.label), key=lambda r: r.id)
    bad = sorted(
        (
            r
            for r in labels
            if r.label in {"junk", "scam"}
            and r.page_type == "storefront"
            and r.source == "wayback"
            and r.origin_url.startswith("http")
        ),
        key=lambda r: r.id,
    )
    rng = random.Random(_SEED)
    candidates = [r for r in bad if _links(corpus, r)]
    return golden + rng.sample(candidates, min(junk, len(candidates)))


def _links(corpus: Corpus, record: LabelRecord) -> list[tuple[str, str]]:
    facts, _ = parse_page(corpus.read_page(record.id), record.origin_url)
    if not shop_pages.own_site(facts, record.page_type or ""):
        return []
    return shop_pages.page_urls(facts, record.origin_url)[: shop_pages.MAX_PAGES]


def _days(a: str, b: str) -> int:
    def day(stamp: str) -> datetime:
        return datetime.strptime(stamp[:8], "%Y%m%d").replace(tzinfo=UTC)

    return abs((day(a) - day(b)).days)


async def _live(url: str) -> tuple[str | None, float, str]:
    if not await robots_allows(url):
        return None, 0.0, "robots"
    start = time.monotonic()
    try:
        body = await fetch_public(
            url,
            timeout=shop_pages.FETCH_TIMEOUT_S,
            total_timeout=shop_pages.FETCH_TIMEOUT_S,
            max_bytes=shop_pages.MAX_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 - a page that can't be read is recorded as such
        return None, time.monotonic() - start, type(exc).__name__
    html = body.decode("utf-8", "replace")
    return (None if is_challenge_page(html) else html), time.monotonic() - start, ""


async def _archived(client: PoliteClient, url: str, when: str) -> tuple[str | None, str]:
    try:
        found = await wayback.fetch_closest(client, when, url)
    except wayback.WaybackUnavailableError:
        return None, "archive unavailable"
    if found is None:
        return None, "no capture"
    stamp, _, html = found
    if _days(stamp, when) > MAX_CAPTURE_GAP_DAYS:
        return None, "capture too far"
    return (None if is_challenge_page(html) else html), ""


def _when(record: LabelRecord) -> str:
    stamp = record.origin_id if record.origin_id[:8].isdigit() else record.collected_at
    return stamp[:8] or "2024"


async def collect(corpus: Corpus, records: list[LabelRecord]) -> None:
    """
    Read and keep each page's shop pages (``<corpus>/cache/shop_pages/<id>.json``); pages kept already are skipped.

    :param corpus: The corpus.
    :param records: The pages (:func:`select`).
    """
    client = PoliteClient(interval=1.0, retries=2)
    cache = _cache(corpus)

    async def one(record: LabelRecord) -> None:
        path = cache / f"{record.id}.json"
        if path.exists():
            return
        entry: dict[str, Any] = {"pages": {}, "why": {}, "seconds": {}}
        for _, url in _links(corpus, record):
            if record.source == "live":
                html, seconds, why = await _live(url)
                entry["seconds"][url] = round(seconds, 2)
            else:
                html, why = await _archived(client, url, _when(record))
            page = await asyncio.to_thread(shop_pages.read_html, html, url) if html else None
            entry["pages"][url] = page.__dict__ if page else None
            if why:
                entry["why"][url] = why
        dump_json(path, entry)

    semaphore = asyncio.Semaphore(4)

    async def bounded(record: LabelRecord) -> None:
        async with semaphore:
            await one(record)

    for i, task in enumerate(asyncio.as_completed([bounded(r) for r in records]), start=1):
        await task
        if i % 20 == 0:
            logger.info("shop_pages_progress", done=i, total=len(records))


def _source(corpus: Corpus, records: list[LabelRecord]) -> shop_pages.Source:
    pages: dict[str, shop_pages.ReadPage | None] = {}
    for record in records:
        path = _cache(corpus) / f"{record.id}.json"
        if path.exists():
            for url, page in json.loads(path.read_text())["pages"].items():
                pages[url] = shop_pages.ReadPage(**page) if page else None

    async def source(url: str) -> shop_pages.ReadPage | None:
        return pages.get(url)

    return source


class _Audits:
    """Every claim audit's usage during a run (wraps :func:`claim_audit._ask`)."""

    def __init__(self) -> None:
        self.usage: list[claim_audit.Usage] = []
        self._ask = claim_audit._ask

    async def __call__(self, document: str) -> claim_audit.ClaimAudit:
        audit = await self._ask(document)
        self.usage.append(audit.usage)
        return audit


def _told(result: dict[str, Any]) -> list[str]:
    verdict = result.get("verdict") or {}
    return [r.get("detail", "") for r in verdict.get("reasons", []) if r.get("type") == "claim_contradicted"]


async def _run(
    corpus: Corpus, records: list[LabelRecord], concurrency: int, *, with_pages: bool
) -> tuple[dict[str, Any], _Audits]:
    claim_audit._CALLS.clear()
    shop_pages.use_source(_source(corpus, records) if with_pages else None)
    audits = _Audits()
    claim_audit._ask = audits
    try:
        outcomes, results = await run_tier("deep", corpus, records, concurrency)
    finally:
        claim_audit._ask = audits._ask
        shop_pages.use_source(None)
    out = {}
    for o in outcomes:
        result = results.get(o.id) or {}
        out[o.id] = {"band": o.band, "error": o.error, "told": _told(result)}
    return out, audits


def _usage(audits: _Audits) -> dict[str, Any]:
    usage = audits.usage
    if not usage:
        return {"audits": 0}
    cost = [float(u.cost_eur()) for u in usage]
    return {
        "audits": len(usage),
        "input_tokens_mean": round(statistics.mean(u.input_tokens for u in usage)),
        "output_tokens_mean": round(statistics.mean(u.output_tokens for u in usage)),
        "cost_eur_mean": round(statistics.mean(cost), 5),
        # Only calls made in this run have a real duration (a recorded answer comes back at once).
        "seconds_median_of_new_calls": round(statistics.median(s), 2)
        if (s := [u.seconds for u in usage if u.seconds > 0.3])
        else None,
    }


def _fetch_timing(corpus: Corpus, records: list[LabelRecord]) -> dict[str, Any]:
    slowest: list[float] = []
    reasons: Counter[str] = Counter()
    read = wanted = 0
    for record in records:
        path = _cache(corpus) / f"{record.id}.json"
        if not path.exists():
            continue
        entry = json.loads(path.read_text())
        wanted += len(entry["pages"])
        read += sum(1 for p in entry["pages"].values() if p)
        reasons.update(entry["why"].values())
        if entry["seconds"]:
            slowest.append(max(entry["seconds"].values()))  # the pages are fetched in parallel
    return {
        "pages_wanted": wanted,
        "pages_read": read,
        "not_read": dict(reasons),
        "live_fetch_s_median": round(statistics.median(slowest), 2) if slowest else None,
        "live_fetch_s_p90": round(sorted(slowest)[int(len(slowest) * 0.9)], 2) if slowest else None,
        "live_shops_timed": len(slowest),
    }


def _seller(record: LabelRecord) -> str:
    try:
        notes = json.loads(record.notes or "{}")
    except json.JSONDecodeError:
        notes = {}
    return str(notes.get("seller") or record.origin_url.split("/")[2])


async def compare(corpus: Corpus, records: list[LabelRecord], concurrency: int) -> dict[str, Any]:
    """
    The deep check with and without the shop pages, per page.

    :param corpus: The corpus.
    :param records: The pages (:func:`select`).
    :param concurrency: Pages scored at the same time.
    :return: The report.
    """
    before, before_audits = await _run(corpus, records, concurrency, with_pages=False)
    after, after_audits = await _run(corpus, records, concurrency, with_pages=True)
    groups: dict[str, Any] = {}
    for name, members in (
        ("golden", [r for r in records if r.origin_dataset == DATASET]),
        ("junk_scam", [r for r in records if r.origin_dataset != DATASET]),
    ):
        pages = []
        for record in members:
            b, a = before.get(record.id, {}), after.get(record.id, {})
            new = [t for t in a.get("told", []) if t not in b.get("told", [])]
            pages.append(
                {
                    "id": record.id,
                    "seller": _seller(record),
                    "label": record.label,
                    "band_before": b.get("band"),
                    "band_after": a.get("band"),
                    "errors": [e for e in (b.get("error"), a.get("error")) if e],
                    "new_notes": new,
                    "gone_notes": [t for t in b.get("told", []) if t not in a.get("told", [])],
                }
            )
        groups[name] = {
            "pages": len(pages),
            "with_shop_pages": sum(
                1 for r in members if (_cache(corpus) / f"{r.id}.json").exists() and _links(corpus, r)
            ),
            "notes_added": sum(1 for p in pages if p["new_notes"]),
            "band_changes": [p for p in pages if p["band_before"] != p["band_after"]],
            "bands_after": dict(Counter(p["band_after"] for p in pages)),
            "changed": [p for p in pages if p["new_notes"] or p["gone_notes"] or p["band_before"] != p["band_after"]],
        }
    return {
        "usage_before": _usage(before_audits),
        "usage_after": _usage(after_audits),
        "fetch": _fetch_timing(corpus, records),
        "groups": groups,
    }


def to_markdown(report: dict[str, Any]) -> str:
    """
    The report as Markdown.

    :param report: From :func:`compare`.
    :return: The Markdown.
    """
    lines = [f"# Claim audit across the shop's pages {report['stamp']}", ""]
    lines += [f"- usage before: {report['usage_before']}", f"- usage after: {report['usage_after']}"]
    lines += [f"- shop page fetches: {report['fetch']}", ""]
    for name, group in report["groups"].items():
        lines += [
            f"## {name}: {group['pages']} pages, {group['with_shop_pages']} with shop pages",
            "",
            (
                f"Notes added on {group['notes_added']} pages; band changes: {len(group['band_changes'])}; "
                f"bands after: {group['bands_after']}"
            ),
            "",
        ]
        for page in group["changed"]:
            lines.append(f"- **{page['seller']}** ({page['label']}, {page['band_before']} → {page['band_after']})")
            lines += [f"  - new: {t}" for t in page["new_notes"]]
            lines += [f"  - gone: {t}" for t in page["gone_notes"]]
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("command", choices=["collect", "compare"])
    parser.add_argument("--junk", type=int, default=40)
    parser.add_argument("--replay", action="store_true", help="only use cached LLM responses")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    corpus = Corpus()
    records = select(corpus, args.junk)
    if args.command == "collect":
        asyncio.run(collect(corpus, records))
        print(json.dumps(_fetch_timing(corpus, records), indent=1))  # noqa: T201 - command-line report
        return
    configure(corpus, replay=args.replay)
    settings.network_lookups = False
    settings.expansion_classes = True  # as dev and production run
    report = asyncio.run(compare(corpus, records, args.concurrency))
    report["stamp"] = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    reports = corpus.root / "reports"
    dump_json(reports / f"shop-pages-{report['stamp']}.json", report)
    markdown = to_markdown(report)
    (reports / f"shop-pages-{report['stamp']}.md").write_text(markdown)
    print(markdown)  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
