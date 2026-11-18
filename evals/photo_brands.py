"""
Replay the photo check's brand comparison on the corpus: the same photo under other brand names, or a brand's photo.

Both new findings: the same photo under other brand names, and a known brand's catalogue photo under another brand.

Usage::

    uv run python -m evals.photo_brands [--split test|train|all] [--dataset NAME ...] [--limit N] [--with-url]
        [--seed abo-seed.npz] [--wikidata]

Corpus replays leave the photo check out (:mod:`evals.run`: nothing has been checked by Unfret yet when a corpus page
is scored, and the index must not be filled with it). This replay builds the index the check would have seen had
Unfret checked every corpus page: each page's main photo (fetched once, fingerprinted on this machine, the prints
cached in ``<corpus>/cache/photo-prints.jsonl``) with the brand the page names, plus the catalogue seed when given
(``--seed``, :mod:`unfret.image_seed`). Then, per page, the recorded analysis (LLM answers replayed, no network
lookups, as :mod:`evals.run`) is completed with the photo check's lookup in that index, and the shopper verdict is
built twice with the same score: without the photo check and with it. The report counts how often each new reason
fires, its precision (the share of junk and scam pages among them), and every band that changes; it names no page
(only corpus IDs, and it stays in the corpus).

The catalogue brand's Wikidata entry is a fact about the brand today, not the archived page: ``--wikidata`` looks
it up (the only network lookup; without it the "known brand" finding can't fire). The features the check adds
(``reverse_image.other_brands``, ``reverse_image.brand_photo_reused``) are written next to the report
(``photo-brands-<stamp>-features.jsonl``), for training (``evals.train --extra-features``).

Coverage is what it is: archived pages' photos are often gone, so the index is thin outside the marketplace pages.
"""

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import numpy as np
import structlog

from evals.corpus import Corpus, LabelRecord, dump_json, is_product
from evals.run import DEFAULT_EXCLUDED, configure
from unfret import brands, image_index, image_seed, scoring, shop_store, shopper
from unfret.config import settings
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import PRODUCT_PAGES, reverse_image
from unfret.graph.state import Finding
from unfret.image_index import ImagePrint
from unfret.image_store import Listing, MemoryImageStore, StoredPrint, listing
from unfret.net import fetch_public

logger = structlog.get_logger("unfret.evals")

NEW_REASONS = ("rebranded_photo", "brand_photo_reused")
FEATURES = ("reverse_image.other_brands", "reverse_image.brand_photo_reused")
UNRELIABLE = {"junk", "scam"}
WARNING = {"careful", "junk_or_scam"}


def _hex(fp: ImagePrint) -> dict[str, Any]:
    return {"phash": fp.phash, "dhash": fp.dhash, "embedding": fp.embedding.hex(), "colour": fp.colour.hex()}


def _print(row: dict[str, Any]) -> ImagePrint:
    return ImagePrint(row["phash"], row["dhash"], bytes.fromhex(row["embedding"]), bytes.fromhex(row["colour"]))


class PrintCache:
    """Each page's photo prints (or why there are none), so a photo is fetched once."""

    def __init__(self, path: Path) -> None:
        """
        Read the cache.

        :param path: The JSON-lines file.
        """
        self.path = path
        self.rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.rows[row["key"]] = row

    def add(self, row: dict[str, Any]) -> None:
        """
        Keep a row, and append it to the file.

        :param row: ``key`` (the photo URL's page and address) and the prints or ``error``.
        """
        self.rows[row["key"]] = row
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")


async def _fingerprint(url: str, model: image_index.Embedder) -> dict[str, Any]:
    try:
        data = await fetch_public(url, timeout=8.0, max_bytes=reverse_image.MAX_IMAGE_BYTES)
        return _hex(await asyncio.to_thread(image_index.fingerprint, data, model))
    except Exception as exc:  # noqa: BLE001 - a page without a usable photo is just not in the index
        return {"error": type(exc).__name__}


def _seed(store: MemoryImageStore, path: Path) -> int:
    with np.load(path, allow_pickle=False) as npz:
        data = {name: npz[name] for name in npz.files}
    for i in range(len(data["asin"])):
        fp = ImagePrint(
            int(data["phash"][i]),
            int(data["dhash"][i]),
            data["emb"][i].astype(np.int8).tobytes(),
            data["colour"][i].astype(np.int8).tobytes(),
        )
        where = Listing(image_seed.DOMAIN, image_seed.listing_key(str(data["asin"][i])))
        key = (where.domain, where.listing_key)
        store.rows[key] = (
            StoredPrint(
                -(i + 1_000_000), "abo", where.domain, where.listing_key, fp, str(data["brand"][i])[:120] or None
            ),  # fmt: skip
            "",
            shop_store.today(),
            shop_store.today(),
        )
    return len(data["asin"])


async def _analyse(
    corpus: Corpus, records: list[LabelRecord], cache: PrintCache, *, with_url: bool, concurrency: int
) -> dict[str, dict[str, Any]]:
    """Each page's recorded analysis (without the photo check) and its main photo's address, fingerprinted once."""
    graph = build_graph([s for s in SKILLS if s.name not in DEFAULT_EXCLUDED])
    embedder = image_index.embedder()
    if not embedder.available():
        raise SystemExit(f"no image model at {settings.image_model_path} (python -m unfret.image_index <path>)")
    semaphore = asyncio.Semaphore(concurrency)
    states: dict[str, dict[str, Any]] = {}

    async def analyse(record: LabelRecord) -> None:
        async with semaphore:
            try:
                url = record.origin_url if with_url else ""
                state = await graph.ainvoke({"page_content": corpus.read_page(record.id), "url": url, "flags": []})
            except Exception as exc:  # noqa: BLE001
                logger.info("photo_brands_page_failed", id=record.id, error=type(exc).__name__)
                return
            photo = reverse_image.main_photo(state["facts"], state.get("page_type", "storefront"))
            if photo and record.origin_url:
                photo = urljoin(record.origin_url, photo)
                if photo not in cache.rows:
                    cache.add({"key": photo, **await _fingerprint(photo, embedder)})
            states[record.id] = {"state": state, "photo": photo, "record": record}

    await asyncio.gather(*(analyse(r) for r in records))
    return states


def _cached(found: dict[str, Any], cache: PrintCache) -> ImagePrint | None:
    row = cache.rows.get(found["photo"]) if found["photo"] else None
    return _print(row) if row and "error" not in row else None


async def _index(states: dict[str, dict[str, Any]], cache: PrintCache, seed: Path | None) -> MemoryImageStore:
    """The index the photo check would have had: every page's photo with its brand, and the catalogue seed."""
    store = MemoryImageStore()
    if seed:
        _seed(store, seed)
    for found in states.values():
        state, record, fp = found["state"], found["record"], _cached(found, cache)
        where = listing(record.origin_url, state["facts"])
        if fp is None or where is None:
            continue
        product = state.get("page_type", "storefront") in PRODUCT_PAGES
        label = brands.label(state["facts"].brand) if product else None
        await store.upsert(where, fp, image_url=found["photo"], day=shop_store.today(), source="seen", label=label)
    return store


async def _compare(found: dict[str, Any], fp: ImagePrint, *, needs_reason: bool) -> dict[str, Any]:
    """One page's verdict without and with the photo check (the same score: the photo check isn't in the scorer)."""
    state, record = found["state"], found["record"]

    async def fingerprint(image_url: str, model: image_index.Embedder) -> ImagePrint:
        return fp

    reverse_image._fingerprint = fingerprint  # the cached prints, no fetch
    photo_state: Any = {**state, "url": record.origin_url}
    out = await reverse_image.ReverseImageSkill().run(photo_state)
    added = {k: float(v) for k, v in out["features"].items()}
    findings = [
        *state.get("findings", []),
        *(Finding(skill="reverse_image", type=o.type, evidence=o.evidence, thumbnails=o.thumbnails)
          for o in out["findings"]),
    ]  # fmt: skip
    page_type = state.get("page_type", "storefront")
    common: dict[str, Any] = {"facts": state["facts"], "page_type": page_type}
    extra: dict[str, Any] = {
        "confidence": (state.get("assessment") or {}).get("confidence"),
        "needs_reason": needs_reason,
    }
    score = int(state["score"])
    before = shopper.build(score, features=state["features"], findings=state.get("findings", []), **common, **extra)
    after = shopper.build(score, features={**state["features"], **added}, findings=findings, **common, **extra)
    fired = shopper.fired_reasons(features={**state["features"], **added}, findings=findings, **common)
    return {
        "label": record.label,
        "dataset": record.origin_dataset,
        "recorded_band": (state.get("verdict") or {}).get("band"),
        "before": before.band,
        "after": after.band,
        "reasons": [k for k in fired if k in (*NEW_REASONS, "photo_reused")],
        "other_brands": added.get("reverse_image.other_brands", 0.0),
        "brand_photo_reused": added.get("reverse_image.brand_photo_reused", 0.0),
        "other_shops": added.get("reverse_image.other_shops", 0.0),
        "features": {k: added[k] for k in FEATURES if k in added},
    }


async def replay(
    corpus: Corpus, records: list[LabelRecord], *, with_url: bool, seed: Path | None, wikidata: bool, concurrency: int
) -> dict[str, Any]:
    """
    Run the replay (see the module docstring).

    :param corpus: The corpus.
    :param records: The product pages to replay.
    :param with_url: Give the analysis the page's URL (as the golden set's deep replay does).
    :param seed: The catalogue seed file, or ``None``.
    :param wikidata: Look the catalogue brands up on Wikidata.
    :param concurrency: Pages at a time.
    :return: The report, with ``per_page`` (corpus IDs only, each with the features the check adds).
    """
    configure(corpus, replay=True)
    model = scoring.load_model(scoring.configured_path(scoring.MODEL_PATH))
    cache = PrintCache(corpus.root / "cache" / "photo-prints.jsonl")
    states = await _analyse(corpus, records, cache, with_url=with_url, concurrency=concurrency)
    store = await _index(states, cache, seed)
    reverse_image.use_store(store)
    settings.fingerprints = True
    settings.network_lookups = wikidata  # only the catalogue brand's Wikidata entry is looked up from here on
    per_page: dict[str, dict[str, Any]] = {}
    for record_id, found in states.items():
        if (fp := _cached(found, cache)) is not None:
            per_page[record_id] = await _compare(found, fp, needs_reason=bool(model and model.needs_reason))
    await reverse_image.drain()
    return summarise(records, states=states, cache=cache, store=store, per_page=per_page)


def _precision(pages: list[dict[str, Any]]) -> dict[str, Any]:
    labels = Counter(p["label"] for p in pages)
    bad = sum(labels[k] for k in UNRELIABLE)
    return {"pages": len(pages), "labels": dict(labels), "precision": round(bad / len(pages), 3) if pages else None}


def summarise(
    records: list[LabelRecord],
    *,
    states: dict[str, Any],
    cache: PrintCache,
    store: MemoryImageStore,
    per_page: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """
    The report's aggregates.

    :param records: The pages asked for.
    :param states: The analysed pages.
    :param cache: The photo prints.
    :param store: The index built.
    :param per_page: Each looked-up page's outcome.
    :return: The report (without the features).
    """
    pages = list(per_page.values())
    seen_rows = [r for r, *_ in store.rows.values() if r.source == "seen"]
    changed = Counter((p["before"], p["after"]) for p in pages if p["before"] != p["after"])
    return {
        "pages": len(records),
        "analysed": len(states),
        "with_photo_print": len(pages),
        "photo_errors": dict(Counter(r.get("error") for r in cache.rows.values() if "error" in r)),
        "index": {
            "seen": len(seen_rows),
            "seen_with_brand": sum(1 for r in seen_rows if r.label),
            "catalogue": len(store.rows) - len(seen_rows),
        },
        "recorded_band_matches_before": sum(1 for p in pages if p["recorded_band"] == p["before"]),
        "same_photo_elsewhere": _precision([p for p in pages if p["other_shops"] >= 1]),
        "features": {
            "other_brands": _precision([p for p in pages if p["other_brands"] >= 1]),
            "brand_photo_reused": _precision([p for p in pages if p["brand_photo_reused"] >= 1]),
        },
        "reasons": {
            key: _precision([p for p in pages if key in p["reasons"]]) for key in (*NEW_REASONS, "photo_reused")
        },
        "band_changes": {f"{a} -> {b}": n for (a, b), n in sorted(changed.items())},
        "new_warnings_on_legit": sum(
            1 for p in pages if p["label"] == "legit" and p["after"] in WARNING and p["before"] not in WARNING
        ),
        "new_serious": sum(1 for p in pages if p["after"] == "junk_or_scam" and p["before"] != "junk_or_scam"),
        "per_page": per_page,
    }


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--split", choices=["test", "train", "all"], default="all")
    parser.add_argument("--dataset", nargs="+", default=[], help="only pages of these origin datasets")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--with-url", action="store_true", help="give the analysis the page's URL")
    parser.add_argument("--seed", type=Path, default=None, help="the catalogue seed (abo-seed.npz)")
    parser.add_argument("--wikidata", action="store_true", help="look the catalogue brands up on Wikidata")
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    corpus = Corpus()
    records = [
        r
        for r in corpus.labels().values()
        if is_product(r)
        and r.label
        and args.split in {"all", r.split}
        and (not args.dataset or r.origin_dataset in args.dataset)
    ][: args.limit]
    report = asyncio.run(
        replay(
            corpus,
            records,
            with_url=args.with_url,
            seed=args.seed,
            wikidata=args.wikidata,
            concurrency=args.concurrency,
        )
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    reports = corpus.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"photo-brands-{stamp}-features.jsonl").write_text(
        "".join(json.dumps({"id": i, "features": p["features"]}) + "\n" for i, p in sorted(report["per_page"].items()))
    )
    dump_json(reports / f"photo-brands-{stamp}.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "per_page"}, indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
