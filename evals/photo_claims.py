"""
The photo check against the listing's own claims (:mod:`unfret.graph.skills.photo_claims`), on real listings, live.

Usage::

    uv run python -m evals.photo_claims [--corpus] [--abo N]

Two sets of pages, each run through the skill as a deep check runs it (its photo fetching, the vision model, the
mismatch rules), with live model calls (they are cheap; the report gives the cost):

- ``--corpus``: the corpus's product pages whose title or product data states a piece count or a size, by label.
  Their photos are fetched where the page had them, else from the Internet Archive's capture of the photo.
- ``--abo N``: up to N listings of the Amazon Berkeley Objects catalogue (ABO, © Amazon.com, CC BY 4.0) whose name
  states a count or a size: Amazon's catalogue of branded products (Amazon's own brands among them), a set of
  well-known sellers on which every flag is a false alarm. Their photos are ABO's own copies.

The report (every check, every flag with its evidence, cost and time per check) goes to
``<corpus>/reports/photo_claims-<stamp>.json``: it names pages, so it stays in the corpus.
"""

import argparse
import asyncio
import csv
import gzip
import io
import json
import random
import time
import urllib.request
from datetime import UTC, datetime
from typing import Any

from evals.corpus import Corpus, dump_json
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.facts.extract import parse_page
from unfret.graph.skills import photo_claims
from unfret.graph.skills.photo_claims import PhotoClaimsSkill, claims
from unfret.net import fetch_public

ABO = "https://amazon-berkeley-objects.s3.amazonaws.com"
SEED = 20261120


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed ABO https URLs
        return bytes(response.read())


def _value(field: Any) -> str:
    if not isinstance(field, list) or not field:
        return ""
    english = [f for f in field if str(f.get("language_tag", "")).startswith("en")]
    return str((english or field)[0].get("value", ""))[:300]


def abo_pages(n: int, shards: int = 2) -> list[dict[str, Any]]:
    """
    ABO listings whose name states a piece count or a size, with their photos.

    :param n: At most this many (a seeded sample).
    :param shards: How many of ABO's 16 listing files to read.
    :return: Pages: an id, the label (``legit``), the facts.
    """
    with gzip.open(io.BytesIO(_get(f"{ABO}/images/metadata/images.csv.gz")), "rt") as index:
        paths = {row["image_id"]: row["path"] for row in csv.DictReader(index)}
    pages: list[dict[str, Any]] = []
    for i in range(shards):
        for line in gzip.decompress(_get(f"{ABO}/listings/metadata/listings_{i:x}.json.gz")).splitlines():
            listing = json.loads(line)
            ids = [listing.get("main_image_id"), *listing.get("other_image_id", [])]
            images = [f"{ABO}/images/original/{paths[x]}" for x in ids if x in paths]
            facts = PageFacts(title=_value(listing.get("item_name")), brand=_value(listing.get("brand")))
            stated = claims(facts)
            if images and (stated.count or stated.size):
                facts.image_urls = images[:10]
                pages.append({"id": f"abo-{listing['item_id']}", "label": "legit", "facts": facts, "wayback": None})
    random.Random(SEED).shuffle(pages)
    return pages[:n]


def corpus_pages(corpus: Corpus) -> list[dict[str, Any]]:
    """
    The corpus's product pages that state a piece count or a size.

    :param corpus: The corpus.
    :return: Pages: an id, the label, the facts, the capture's timestamp (for the Internet Archive).
    """
    pages: list[dict[str, Any]] = []
    for line in (corpus.root / "labels.jsonl").read_text().splitlines():
        record = json.loads(line)
        if record.get("page_type") not in {"storefront", "aggregate_listing"}:
            continue
        path = corpus.root / "pages" / f"{record['id']}.html"
        if not path.exists():
            continue
        url = record.get("origin_url") or "https://example.com/"
        facts, _ = parse_page(path.read_text(errors="ignore"), url)
        stated = claims(facts)
        if stated.count or stated.size:
            facts.image_urls = [u for u in facts.image_urls if u.startswith("http")]
            stamp = record.get("origin_id") if record.get("source") == "wayback" else None
            pages.append({"id": record["id"], "label": record.get("label"), "facts": facts, "wayback": stamp})
    return pages


def _fetcher(wayback: str | None) -> Any:
    async def fetch(url: str) -> bytes:
        try:
            return await fetch_public(url, timeout=6.0, max_bytes=photo_claims.MAX_IMAGE_BYTES)
        except Exception:
            if not wayback:
                raise
            return await fetch_public(f"https://web.archive.org/web/{wayback}im_/{url}", timeout=10.0)

    return fetch


_ASK = photo_claims.ask


async def check(page: dict[str, Any]) -> dict[str, Any]:
    """
    Run the skill on one page, with its own photo fetcher, counting the tokens its questions use.

    :param page: The page.
    :return: What it found, the tokens, the cost and the time.
    """
    tokens = [0, 0, 0]

    async def ask(photo: str, title: str) -> tuple[Any, int, int]:
        answer, given, taken = await _ASK(photo, title)
        tokens[0], tokens[1], tokens[2] = tokens[0] + 1, tokens[1] + given, tokens[2] + taken
        return answer, given, taken

    photo_claims._fetch = _fetcher(page["wayback"])
    photo_claims.ask = ask
    start = time.monotonic()
    facts: PageFacts = page["facts"]
    state: Any = {"facts": facts, "url": facts.image_urls[0] if facts.image_urls else ""}
    try:
        out = await PhotoClaimsSkill().run(state)
    except Exception as exc:  # noqa: BLE001 - one page fewer
        return {"id": page["id"], "label": page["label"], "error": type(exc).__name__}
    stated = claims(facts)
    usage = photo_claims.Usage(*tokens)
    return {
        "id": page["id"],
        "label": page["label"],
        "title": facts.title[:160],
        "count": stated.count.text if stated.count else None,
        "size": stated.size.text if stated.size else None,
        "features": out["features"],
        "findings": [f.evidence for f in out["findings"]],
        "summary": out["skill_results"][0],
        "seconds": round(time.monotonic() - start, 1),
        "images": usage.images,
        "cost_eur": float(usage.cost_eur()),
    }


async def main() -> None:
    """Run the chosen sets and write the report."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--corpus", action="store_true")
    parser.add_argument("--abo", type=int, default=0)
    args = parser.parse_args()
    settings.photo_claims = True
    settings.network_lookups = True
    corpus = Corpus()
    pages = (corpus_pages(corpus) if args.corpus else []) + (abo_pages(args.abo) if args.abo else [])
    results = [await check(p) for p in pages]  # one at a time: the photo fetcher is swapped per page
    checked = [r for r in results if r.get("features", {}).get("photo_claims.checked")]
    flagged = [r for r in checked if r["findings"]]
    cost = sum(r["cost_eur"] for r in checked)
    by_label: dict[str, dict[str, int]] = {}
    for r in results:
        row = by_label.setdefault(str(r["label"]), {"pages": 0, "checked": 0, "flagged": 0})
        row["pages"] += 1
        row["checked"] += int(r in checked)
        row["flagged"] += int(r in flagged)
    report = {
        "model": settings.photo_claims_model,
        "pages": len(pages),
        "checked": len(checked),
        "by_label": by_label,
        "flagged": flagged,
        "cost_eur": round(cost, 5),
        "cost_eur_per_check": round(cost / len(checked), 6) if checked else None,
        "seconds_per_check": round(sum(r["seconds"] for r in checked) / len(checked), 1) if checked else None,
        "results": results,
    }
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M")
    dump_json(corpus.root / "reports" / f"photo_claims-{stamp}.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=1, ensure_ascii=False))  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
