"""
Shops the Czech Trade Inspection Authority (ČOI) warns against (``storefront``, ``scam``), from the Internet Archive.

The corpus's scam storefronts came mostly from one dataset (BEYOND PHISH), so "this source" predicted "scam". ČOI
publishes its list of risky web shops (*rizikové e-shopy*) as open data under its open-data licence (reuse,
including commercial, with ČOI named as the source and the meaning unchanged:
https://coi.gov.cz/pro-spotrebitele/otevrena-data/licence-otevrenych-dat/). Each entry is a domain, ČOI's reason
(an anonymous operator, or contact details that can't be verified in any business register) and, on the public list
page, the date of the warning.

Fake shops often take over expired domains of former real shops, so a capture counts only from the period the
warning covers: from :data:`WINDOW_BEFORE_DAYS` before the warning to :data:`WINDOW_AFTER_DAYS` after it. The home
page closest to the warning date must be from that period and look like a shop
(:func:`evals.shopcheck.looks_like_shop`); a product page it links to (as for :mod:`evals.sources.us_shops`) from
the same period is taken when there is one, else the home page. Labels are dataset labels until the second opinion
confirms them (strict for this dataset: anything but a confident "scam" or "junk" drops the page).

About one request a second. Misses are kept in ``raw/coi_shops_misses.txt`` and skipped on re-runs.

Usage: ``uv run python -m evals.sources.coi_shops [--target 170] [--seed 13]``.
"""

import argparse
import asyncio
import csv
import io
import json
import random
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.shopcheck import looks_like_shop
from evals.sources._http import PoliteClient
from evals.sources.us_shops import is_product_page, page_links, platform_of, product_path, read_misses
from evals.sources.wayback import WaybackUnavailableError, fetch_closest
from unfret.facts import parse_page
from unfret.html_cleaner import clean_html

logger = structlog.get_logger("unfret.evals")

DATASET = "coi-risky-eshops"
LIST_CSV = "https://www.coi.gov.cz/userdata/files/dokumenty-ke-stazeni/open-data/rizikove-seznam.csv"
LIST_PAGE = "https://coi.gov.cz/pro-spotrebitele/rizikove-e-shopy/"
WINDOW_BEFORE_DAYS = 365
WINDOW_AFTER_DAYS = 30
_DOMAIN_RE = re.compile(r"[a-z0-9.-]+\.[a-z]{2,}")
_DATE_RE = re.compile(r"\((\d\d)\. (\d\d)\. (\d{4})\)")
# ČOI's two standard reasons.
_REASONS = {"anonymn": "anonymous operator", "nepodařilo ověřit": "contact details not verifiable"}


def read_list(raw_csv: bytes) -> dict[str, str]:
    """
    The domains and ČOI's reason from the open-data CSV (``domain;'reason'``, Windows-1250).

    :param raw_csv: The downloaded file.
    :return: Reason (short English form, or ``other``) by domain.
    """
    text = raw_csv.decode("cp1250", errors="replace")
    out: dict[str, str] = {}
    for row in csv.reader(io.StringIO(text), delimiter=";", quotechar="'"):
        if not row or not _DOMAIN_RE.fullmatch(row[0].strip().lower()):
            continue
        reason = row[1] if len(row) > 1 else ""
        out[row[0].strip().lower()] = next((v for k, v in _REASONS.items() if k in reason), "other")
    return out


def warning_dates(page_html: str) -> dict[str, str]:
    """
    Each domain's warning date from the public list page, where a domain is followed by ``(dd. mm. yyyy)``.

    :param page_html: The list page's HTML.
    :return: ``YYYYMMDD`` by domain.
    """
    lines = [line.strip() for line in clean_html(page_html).splitlines() if line.strip()]
    out: dict[str, str] = {}
    current = ""
    for line in lines:
        if _DOMAIN_RE.fullmatch(line.lower()):
            current = line.lower()
        elif current and (m := _DATE_RE.fullmatch(line)):
            out.setdefault(current, f"{m.group(3)}{m.group(2)}{m.group(1)}")
            current = ""
    return out


def window(warned: str) -> tuple[str, str]:
    """The capture period a warning covers, as ``YYYYMMDD`` bounds."""
    day = date(int(warned[:4]), int(warned[4:6]), int(warned[6:8]))
    return (
        (day - timedelta(days=WINDOW_BEFORE_DAYS)).strftime("%Y%m%d"),
        (day + timedelta(days=WINDOW_AFTER_DAYS)).strftime("%Y%m%d"),
    )


async def collect(
    client: PoliteClient, domain: str, warned: str, reason: str, rng: random.Random
) -> tuple[LabelRecord, str] | str:
    """
    Fetch one warned shop's page from the period of the warning: a product page if there is one, else its home page.

    :param client: A rate-limited client.
    :param domain: The shop's domain.
    :param warned: The warning date, ``YYYYMMDD``.
    :param reason: ČOI's reason.
    :param rng: Picks among the product links.
    :return: The record and HTML, or the reason it was skipped.
    """
    start, end = window(warned)
    home = await fetch_closest(client, warned, f"http://{domain}/")
    if home is None or not start <= home[0][:8] <= end:
        return "no capture in the warning period"
    if not looks_like_shop(home[2]):
        return "capture is not a shop"
    found = (*home, "home")
    products, _ = page_links(home[2], home[1])
    for url in rng.sample(products, min(2, len(products))):
        capture = await fetch_closest(client, home[0][:8], url)
        if capture is None or not start <= capture[0][:8] <= end or product_path(urlsplit(capture[1]).path) is None:
            continue
        facts, text = await asyncio.to_thread(parse_page, capture[2], capture[1])
        if is_product_page(capture[2], facts, text):
            found = (*capture, "product")
            break
    timestamp, original, html, kind = found
    notes = {"warned": warned, "reason": reason, "page": kind, "platform": platform_of(html)}
    record = LabelRecord(
        id=page_id("coi", domain),
        label="scam",
        label_source="dataset",
        prior_label="scam",
        confidence=0.7,
        page_type="storefront",
        source="wayback",
        origin_dataset=DATASET,
        origin_url=original,
        origin_id=domain,
        collected_at=timestamp[:8],
        notes=json.dumps(notes, sort_keys=True),
    )
    return record, html


async def build(corpus: Corpus, target: int, seed_value: int) -> int:
    """
    Add up to ``target`` pages of shops ČOI warns against.

    :param corpus: The corpus.
    :param target: Pages of this dataset to have at most (pages already stored count).
    :param seed_value: Seed for the order shops are tried in.
    :return: The number of pages stored in this run.
    """
    raw = corpus.root / "raw"
    raw.mkdir(exist_ok=True)
    list_path, dates_path, misses_path = raw / "coi_rizikove.csv", raw / "coi_dates.json", raw / "coi_shops_misses.txt"
    client = PoliteClient(interval=1.0)
    stored = 0
    try:
        if not list_path.exists():
            response = await client.get(LIST_CSV)
            assert response is not None and response.status_code == 200, "ČOI list download failed"
            list_path.write_bytes(response.content)
        if not dates_path.exists():
            response = await client.get(LIST_PAGE)
            assert response is not None and response.status_code == 200, "ČOI list page download failed"
            dates_path.write_text(json.dumps(warning_dates(response.text), indent=1, sort_keys=True) + "\n")
        reasons = read_list(list_path.read_bytes())
        dates = json.loads(dates_path.read_text())
        misses = read_misses(misses_path)
        domains = sorted(d for d in reasons if d in dates)
        random.Random(seed_value).shuffle(domains)
        rng = random.Random(seed_value + 1)
        done = {r.id for r in corpus.labels().values() if r.origin_dataset == DATASET}
        batch: list[tuple[LabelRecord, str]] = []
        for domain in domains:
            if len(done) >= target:
                break
            if page_id("coi", domain) in done or domain in misses:
                continue
            try:
                result = await collect(client, domain, dates[domain], reasons[domain], rng)
            except WaybackUnavailableError:
                continue  # the archive didn't answer: tried again on the next run
            if isinstance(result, str):
                misses[domain] = result
                with misses_path.open("a") as f:
                    f.write(f"{domain}\t{result}\n")
                continue
            done.add(result[0].id)
            batch.append(result)
            if len(batch) >= 10:
                stored += corpus.add_many(batch)
                batch.clear()
                logger.info("coi_progress", stored=len(done), tried=len(misses) + len(done))
        if batch:
            stored += corpus.add_many(batch)
    finally:
        await client.aclose()
    logger.info("coi_shops_done", stored=stored)
    return stored


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--target", type=int, default=170)
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()
    asyncio.run(build(Corpus(), args.target, args.seed))


if __name__ == "__main__":
    main()
