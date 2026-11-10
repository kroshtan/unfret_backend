"""
BEYOND PHISH fraudulent e-commerce websites (Bitaab et al., IEEE S&P 2023).

The authors released the fraudulent sites they collected through crowdsourcing (``dataset.jsonl``, about 6,100
pages with their saved HTML); the legitimate half was not released. The set covers eight kinds of fraudulent
e-commerce site, including fake charities and crypto schemes, so only pages that look like a shop are kept, and a
deterministic sample of those goes into the corpus. The repository states no license; the data is used here for
private evaluation only and is not redistributed.

Usage: ``uv run python -m evals.sources.beyondphish [--sample N]``.
"""

import argparse
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.shopcheck import looks_like_shop
from evals.sources._http import USER_AGENT

logger = structlog.get_logger("unfret.evals")

DATASET = "beyondphish-sp2023"
DOWNLOAD_URL = "https://www.dropbox.com/s/pdnkje0q9m5sp1u/dataset.jsonl?dl=1"


def iter_pages(path: Path) -> Iterator[tuple[str, str]]:
    """
    Yield ``(url, html)`` for every fraudulent page in the dataset file that has content.

    :param path: The downloaded ``dataset.jsonl``.
    :yield: URL and saved HTML pairs.
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if str(row.get("label")) == "1" and row.get("content"):
                yield row["url"], row["content"]


def sample_key(url: str) -> str:
    """A stable pseudo-random sort key, so the same sample is drawn on every run."""
    # The salt predates the rename to Unfret; another one would draw a different sample.
    return hashlib.sha256(f"veridex:{url}".encode()).hexdigest()


def build(corpus: Corpus, sample: int) -> None:
    """
    Add a deterministic sample of the dataset's shop-like pages to the corpus.

    :param corpus: The corpus to add to.
    :param sample: How many shop-like pages to keep.
    """
    raw = corpus.root / "raw" / "beyondphish.jsonl"
    if not raw.exists():
        raw.parent.mkdir(parents=True, exist_ok=True)
        with httpx.stream(
            "GET", DOWNLOAD_URL, follow_redirects=True, timeout=120, headers={"User-Agent": USER_AGENT}
        ) as response:
            response.raise_for_status()
            with raw.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)

    candidates = sorted(iter_pages(raw), key=lambda pair: sample_key(pair[0]))
    seen_domains: set[str] = set()
    picked: list[tuple[LabelRecord, str]] = []
    checked = 0
    for url, html in candidates:
        if len(picked) >= sample:
            break
        domain = httpx.URL(url).host.removeprefix("www.")
        if domain in seen_domains:
            continue
        checked += 1
        if not looks_like_shop(html):
            continue
        seen_domains.add(domain)
        picked.append(
            (
                LabelRecord(
                    id=page_id("bp", url),
                    label="scam",
                    label_source="dataset",
                    prior_label="scam",
                    confidence=0.8,
                    page_type="storefront",
                    source="dataset",
                    origin_dataset=DATASET,
                    origin_url=url,
                    collected_at="2022",
                    notes="Saved HTML from the dataset; relative asset links point at the authors' crawl.",
                ),
                html,
            )
        )
    corpus.add_many(picked)
    logger.info("beyondphish_done", checked=checked, kept=len(picked), candidates=len(candidates))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--sample", type=int, default=300)
    args = parser.parse_args()
    build(Corpus(), args.sample)


if __name__ == "__main__":
    main()
