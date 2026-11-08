"""
The "Fraudulent and Legitimate Online Shops" dataset (Kaunas University of Technology, 2023, CC BY 4.0).

DOI 10.17632/m7xtkx7g5m.1: 1,140 shop URLs labelled fraudulent or legitimate, collected June-July 2023. The
dataset has URLs and URL features only, so the HTML comes from the Internet Archive capture closest to the
collection date. Pages that no longer look like a shop (parked domains, error pages) are dropped.

Usage: ``uv run python -m evals.sources.mendeley [--limit N]``. Re-runs skip pages already in the corpus and
URLs that had no usable capture before.
"""

import argparse
import asyncio
import csv
from pathlib import Path

import httpx
import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.shopcheck import looks_like_shop
from evals.sources._http import PoliteClient
from evals.sources.wayback import closest_snapshot, fetch_snapshot

logger = structlog.get_logger("veridex.evals")

DATASET = "mendeley-m7xtkx7g5m"
DOWNLOAD_URL = (
    "https://data.mendeley.com/public-files/datasets/m7xtkx7g5m/files/33f913ef-0f84-4f36-b2de-3c4e281b24b1"
    "/file_downloaded"
)
COLLECTED = "20230701"
_LABELS = {"fraudulent": "scam", "legitimate": "legit"}
_BATCH = 20


def read_rows(path: Path) -> list[tuple[str, str]]:
    """
    Read ``(url, label)`` pairs from the dataset CSV.

    :param path: The downloaded CSV.
    :return: Pairs with the label mapped to ``scam`` or ``legit``; rows with other labels are skipped.
    """
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [
            (row["Online shop URL"].strip(), _LABELS[row["Label"].strip()])
            for row in reader
            if row["Label"].strip() in _LABELS
        ]


async def build(corpus: Corpus, limit: int | None) -> None:
    """
    Fetch archived HTML for the dataset's shops and add the shop-like ones to the corpus.

    :param corpus: The corpus to add to.
    :param limit: Stop after this many dataset rows (for trial runs).
    """
    raw = corpus.root / "raw" / "mendeley_shops.csv"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(httpx.get(DOWNLOAD_URL, follow_redirects=True, timeout=60).content)
    misses_path = corpus.root / "raw" / "mendeley_wayback_misses.txt"
    misses = set(misses_path.read_text().splitlines()) if misses_path.exists() else set()
    existing = set(corpus.labels())

    rows = read_rows(raw)[:limit]
    client = PoliteClient(interval=1.0)
    batch: list[tuple[LabelRecord, str]] = []
    kept = 0
    try:
        for i, (url, label) in enumerate(rows, start=1):
            record_id = page_id("mdl", url)
            if record_id in existing or url in misses:
                continue
            snapshot = await closest_snapshot(client, url, COLLECTED)
            html = await fetch_snapshot(client, *snapshot) if snapshot else None
            if not html or not looks_like_shop(html):
                misses.add(url)
                misses_path.write_text("\n".join(sorted(misses)) + "\n")
                continue
            assert snapshot is not None  # noqa: S101 - html is only set when there was a snapshot
            batch.append(
                (
                    LabelRecord(
                        id=record_id,
                        label=label,  # type: ignore[arg-type]
                        label_source="dataset",
                        prior_label=label,  # type: ignore[arg-type]
                        confidence=0.8,
                        page_type="storefront",
                        source="wayback",
                        origin_dataset=DATASET,
                        origin_url=url,
                        collected_at=snapshot[0],
                        notes=f"Wayback capture {snapshot[0]} of {snapshot[1]}",
                    ),
                    html,
                )
            )
            kept += 1
            if len(batch) >= _BATCH:
                corpus.add_many(batch)
                batch.clear()
                logger.info("mendeley_progress", done=i, total=len(rows), kept=kept, misses=len(misses))
    finally:
        if batch:
            corpus.add_many(batch)
        await client.aclose()
    logger.info("mendeley_done", kept=kept, misses=len(misses))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(build(Corpus(), args.limit))


if __name__ == "__main__":
    main()
