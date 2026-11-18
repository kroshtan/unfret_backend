"""
The golden set: product pages of well-known, legitimate sellers, for measuring false alarms.

A false "Serious concerns" (or even a "Worth a look") on a seller everyone knows costs more trust than a missed scam,
so the corpus keeps a set of such pages apart (``origin_dataset`` = :data:`DATASET`, labelled ``legit``) and
:mod:`evals.golden` measures every scorer on it. Which sellers are in it is private: the list is the corpus's own
``raw/golden_seeds.json``, never this repository. Each seed is one seller::

    {"name": "…", "market": "US", "segment": "brand|retailer|marketplace", "start": ["https://…"],
     "product": "optional regex a product URL's path must match", "platform": "amazon|bol.com|walmart|",
     "brands": ["optional", "brands a marketplace page must name"], "pages": 1,
     "archive": ["optional host/path/ prefixes under which the archive holds its product pages"]}

For each seller the collector reads its start pages (home or category pages) and follows the links that look most
like one product's page (:func:`candidates`) until one reads as a product page with a price
(:func:`evals.sources.us_shops.is_product_page`). Live first, the way a URL-only check fetches
(:func:`unfret.page_fetch.fetch_page`, only where ``robots.txt`` lets Unfret's user agent in, one request at a
time per site); when the site turns a server away, the Internet Archive's capture closest to today (raw ``id_``
copies, captures from :data:`ARCHIVE_FROM` on, about one request a second): the product pages its start pages
link to, or, for a seed with ``archive`` prefixes, product pages the archive's index lists under them. A marketplace
seed takes only pages whose brand is one of its ``brands`` (a well-known brand's own listing, not whatever sells
best).

Labels are dataset labels (``label_source`` ``dataset``): the sellers are chosen by hand for being well known, which
no second opinion can add to. Misses are kept in ``raw/golden_misses.tsv`` with their reason and skipped on re-runs
(``--retry`` tries them again).

Usage: ``uv run python -m evals.sources.golden [--limit N] [--retry] [--only NAME ...] [--index-only]``.
"""

import argparse
import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.sources._http import PoliteClient
from evals.sources.eu_shops import robots_allows
from evals.sources.us_shops import is_product_page
from evals.sources.wayback import CDX_URL, WaybackUnavailableError, fetch_closest
from unfret.facts import PageFacts, parse_page
from unfret.market import shop_market
from unfret.page_fetch import PageUnreadableError, fetch_page

logger = structlog.get_logger("unfret.evals")

DATASET = "golden-well-known-sellers"
SEEDS = Path("raw") / "golden_seeds.json"
MISSES = Path("raw") / "golden_misses.tsv"
ARCHIVE_FROM = "20240101"  # older captures show a shop as it no longer is
MAX_TRIES = 8  # candidate product links fetched per seller and source before giving up

_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#\s]+)""", re.IGNORECASE)
# Links a page builds with scripts: absolute URLs in its JSON (``"url":"https://…/p/…"``).
_JSON_URL_RE = re.compile(r"""["'](https?://[^"'\s<>]{8,300})["']""")
# A marketplace listing's own address ends at its ASIN; the rest is tracking.
_ASIN_RE = re.compile(r"^(.*?/dp/[A-Z0-9]{10})")
# Path pieces that mark one product's page on the big platforms and retailers.
_PRODUCT_HINTS = re.compile(
    r"/(?:p|pd|dp|ip|t|product|products|item|items|prod|sku|buy|shop/product|artikel|produkt|produit|producto|"
    r"prodotto|a|gp/product)/",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"(?:\d{5,}|[A-Z0-9]{6,}(?=[./_-]|$)|\.p$|\.html?$)")
_NOT_PRODUCT = re.compile(
    r"/(?:c|cat|category|categories|collections?|search|s|cart|basket|checkout|account|login|signin|register|help|"
    r"support|stores?|store-locator|blog|stories|about|careers|customer-service|gift-?cards?|brands?|b|deals|sale|"
    r"content|pages|campaign|l|lp|w|explore|guides?|inspiration|magazine|klantenservice|service|kundenservice)"
    r"(?:/|$)",
    re.IGNORECASE,
)
_ASSET_RE = re.compile(r"\.(?:js|css|json|xml|png|jpe?g|gif|svg|webp|ico|pdf|woff2?)(?:$|\?)", re.IGNORECASE)


@dataclass
class Seed:
    """One well-known seller (see the module docstring)."""

    name: str
    market: str
    segment: str
    start: list[str]
    product: str = ""
    platform: str = ""
    brands: list[str] = field(default_factory=list)
    pages: int = 1
    archive: list[str] = field(default_factory=list)


def load_seeds(corpus: Corpus) -> list[Seed]:
    """
    Read the corpus's seed list.

    :param corpus: The corpus.
    :return: The seeds.
    """
    return [Seed(**item) for item in json.loads((corpus.root / SEEDS).read_text())]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def product_score(path: str) -> int:
    """
    How much a URL path looks like one product's page (3 or more: worth a try), by the generic hints.

    :param path: The path.
    :return: The score, 0 for paths that are something else (categories, search, account, help, …).
    """
    if _NOT_PRODUCT.search(path):
        return 0
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    return 3 * bool(_PRODUCT_HINTS.search(path)) + bool(_CODE_RE.search(path)) + (slug.count("-") >= 2)


def candidates(html: str, base: str, product: str = "") -> list[str]:
    """
    The links on a page that look most like one product's page on the same site, best first.

    :param html: The page's raw HTML.
    :param base: Its URL, for relative links.
    :param product: A regex the path must match (the seed's ``product``), or ``""`` for the generic hints.
    :return: Absolute URLs without query strings, at most 40 (by the generic hints when nothing matches
        ``product``).
    """
    pattern = re.compile(product) if product else None
    scored: dict[str, int] = {}
    links = _HREF_RE.findall(html) + _JSON_URL_RE.findall(html.replace("\\/", "/"))
    for href in links:
        url = urljoin(base, unescape(href))
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or _host(url) != _host(base) or _ASSET_RE.search(parts.path):
            continue
        path = m.group(1) if (m := _ASIN_RE.match(parts.path)) else parts.path
        if pattern is not None:
            if not pattern.search(path):
                continue
            score = 10
        elif (score := product_score(path)) < 3:
            continue
        clean = f"{parts.scheme}://{parts.netloc}{path}"
        scored[clean] = max(scored.get(clean, 0), score)
    if pattern is not None and not scored:
        return candidates(html, base)  # the seed's pattern is out of date: the generic hints
    return sorted(scored, key=lambda u: -scored[u])[:40]


def brand_matches(facts: PageFacts, brands: list[str]) -> bool:
    """
    Whether a marketplace page is a listing of one of the seed's brands (any page when there is no list).

    :param facts: The page facts.
    :param brands: The brands, case-insensitive.
    :return: ``True`` when it matches.
    """
    if not brands:
        return True
    named = f"{facts.brand} {facts.seller} {facts.title}".lower()
    return any(re.search(rf"\b{re.escape(b.lower())}\b", named) for b in brands)


@dataclass
class Found:
    """A product page found for a seed."""

    url: str
    html: str
    fetched: str  # live or wayback
    captured: str  # YYYYMMDD
    market: str


async def _accept(seed: Seed, url: str, html: str) -> tuple[bool, str]:
    facts, text = await asyncio.to_thread(parse_page, html, url)
    if not is_product_page(html, facts, text):
        return False, ""
    if not brand_matches(facts, seed.brands):
        return False, ""
    return True, shop_market(facts, url, text)


async def _fetch_live(url: str) -> tuple[str | None, str]:
    """
    Fetch a page the way a URL-only check does, where ``robots.txt`` allows it.

    :param url: The page.
    :return: ``(html, "")``, or ``(None, reason)``.
    """
    if not await robots_allows(url):
        return None, "robots"
    try:
        return await fetch_page(url), ""
    except PageUnreadableError as exc:
        return None, exc.reason


async def _live(seed: Seed, seen: set[str], want: int) -> tuple[list[Found], str]:
    found: list[Found] = []
    reason = "no product links"
    today = datetime.now(UTC).strftime("%Y%m%d")
    for start in seed.start:
        html, why = await _fetch_live(start)
        if html is None:
            reason = why
            continue
        tries = 0
        for url in candidates(html, start, seed.product):
            if url in seen:
                continue
            if tries >= MAX_TRIES or len(found) >= want:
                break
            seen.add(url)
            tries += 1
            page, why = await _fetch_live(url)
            ok, market = await _accept(seed, url, page) if page is not None else (False, "")
            if page is not None and ok:
                found.append(Found(url, page, "live", today, market))
            else:
                reason = why or "no product page"
        if len(found) >= want:
            break
    return found, reason


async def _archived(client: PoliteClient, seed: Seed, seen: set[str], want: int) -> tuple[list[Found], str]:
    found: list[Found] = []
    reason = "no capture"
    today = datetime.now(UTC).strftime("%Y%m%d")
    for start in seed.start:
        home = await fetch_closest(client, today, start)
        if home is None or home[0][:8] < ARCHIVE_FROM:
            continue
        tries = 0
        reason = "no product links in the capture"
        for url in candidates(home[2], home[1], seed.product):
            if url in seen:
                continue
            if tries >= MAX_TRIES or len(found) >= want:
                break
            seen.add(url)
            tries += 1
            capture = await fetch_closest(client, home[0][:8], url)
            if capture is None or capture[0][:8] < ARCHIVE_FROM:
                reason = "no capture of the product pages"
                continue
            ok, market = await _accept(seed, capture[1], capture[2])
            if ok:
                found.append(Found(capture[1], capture[2], "wayback", capture[0][:8], market))
            else:
                reason = "no product page"
        if len(found) >= want:
            break
    return found, reason


async def indexed(client: PoliteClient, prefix: str, product: str) -> list[tuple[str, str]]:
    """
    Product pages the archive holds under a URL prefix, from its index (CDX), captured since :data:`ARCHIVE_FROM`.

    :param client: A rate-limited client (with a long timeout: prefix queries can take a minute).
    :param prefix: ``host/path/``.
    :param product: A regex the path must match, or ``""``.
    :return: ``(timestamp, URL)`` pairs, one per page.
    """
    response = await client.get(
        CDX_URL,
        params={
            "url": prefix,
            "matchType": "prefix",
            "from": ARCHIVE_FROM,
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            "fl": "timestamp,original",
            "output": "json",
            "limit": "300",
        },
    )
    if response is None or response.status_code != 200:
        return []
    try:
        rows = response.json()[1:]
    except ValueError:
        return []
    pages: dict[str, str] = {}
    for timestamp, url in rows:
        parts = urlsplit(url)
        if not _ASSET_RE.search(parts.path):
            pages.setdefault(f"{parts.scheme}://{parts.netloc}{parts.path}", timestamp)
    pattern = re.compile(product) if product else None
    if pattern is not None and (matching := [(t, u) for u, t in pages.items() if pattern.search(urlsplit(u).path)]):
        return matching
    # The index lists every page under the prefix, and a product page's own check follows: a slug is enough here.
    return [(t, u) for u, t in pages.items() if product_score(urlsplit(u).path) >= 1]


async def _indexed(client: PoliteClient, seed: Seed, seen: set[str], want: int) -> tuple[list[Found], str]:
    found: list[Found] = []
    reason = "nothing in the archive's index"
    rng = random.Random(seed.name)
    for prefix in seed.archive:
        listed = await indexed(client, prefix, seed.product)
        tries = 0
        for timestamp, url in rng.sample(listed, len(listed)):
            if url in seen:
                continue
            if tries >= MAX_TRIES or len(found) >= want:
                break
            seen.add(url)
            tries += 1
            capture = await fetch_closest(client, timestamp, url)
            if capture is None:
                reason = "no capture of the product pages"
                continue
            ok, market = await _accept(seed, capture[1], capture[2])
            if ok:
                found.append(Found(capture[1], capture[2], "wayback", capture[0][:8], market))
            else:
                reason = "no product page"
        if len(found) >= want:
            break
    return found, reason


async def collect(client: PoliteClient, seed: Seed, *, index_only: bool = False) -> tuple[list[Found], str]:
    """
    Find up to ``seed.pages`` product pages of a seller: live, then from the archive for the rest.

    :param client: A rate-limited client for the archive.
    :param seed: The seller.
    :param index_only: Only the archive's index (a second pass over sellers the other ways missed).
    :return: The pages found, and the last reason a try failed.
    """
    seen: set[str] = set()
    found: list[Found] = []
    reason = "skipped"
    if not index_only:
        found, reason = await _live(seed, seen, seed.pages)
    if len(found) < seed.pages:
        archive_reason = "skipped"
        try:
            if not index_only:
                more, archive_reason = await _archived(client, seed, seen, seed.pages - len(found))
                found += more
            if len(found) < seed.pages and seed.archive:
                more, archive_reason = await _indexed(client, seed, seen, seed.pages - len(found))
                found += more
        except WaybackUnavailableError:
            archive_reason = "archive unavailable"
        reason = f"live: {reason}; archive: {archive_reason}"
    return found, reason


def to_record(seed: Seed, page: Found) -> LabelRecord:
    """
    The corpus record of a golden page.

    :param seed: Its seller.
    :param page: The page.
    :return: The record (``legit``, a dataset label).
    """
    notes = {
        "seller": seed.name,
        "market": seed.market,
        "segment": seed.segment,
        "fetched": page.fetched,
        "read_market": page.market,
    }
    return LabelRecord(
        id=page_id("gold", page.url),
        label="legit",
        label_source="dataset",
        prior_label="legit",
        confidence=0.95,
        platform=seed.platform,
        page_type="aggregate_listing" if seed.platform else "storefront",
        source=page.fetched,
        origin_dataset=DATASET,
        origin_url=page.url,
        collected_at=page.captured,
        notes=json.dumps(notes, sort_keys=True),
    )


async def build(
    corpus: Corpus, *, limit: int | None, retry: bool, only: set[str], index_only: bool = False, concurrency: int = 4
) -> int:
    """
    Collect the seeds' pages into the corpus.

    :param corpus: The corpus.
    :param limit: Stop after this many seeds (for trial runs).
    :param retry: Try earlier misses again.
    :param only: Seed names to collect (all when empty).
    :param index_only: Only the archive's index (see :func:`collect`).
    :param concurrency: Sellers collected at the same time (each a different site; the archive client is shared).
    :return: The number of pages added.
    """
    misses_path = corpus.root / MISSES
    misses: dict[str, str] = {}
    if misses_path.exists():
        misses = dict(line.split("\t", 1) for line in misses_path.read_text().splitlines() if "\t" in line)
    have: dict[str, int] = {}
    for record in corpus.labels().values():
        if record.origin_dataset == DATASET:
            seller = json.loads(record.notes).get("seller", "")
            have[seller] = have.get(seller, 0) + 1
    todo = [
        s
        for s in load_seeds(corpus)
        if have.get(s.name, 0) < s.pages and (retry or s.name not in misses) and (not only or s.name in only)
    ][:limit]
    client = PoliteClient(interval=1.0, timeout=90.0)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(seed: Seed) -> tuple[Seed, list[Found], str]:
        async with semaphore:
            pages, reason = await collect(client, seed, index_only=index_only)
        return seed, pages, reason

    added = 0
    try:
        for result in asyncio.as_completed([one(s) for s in todo]):
            seed, pages, reason = await result
            if pages:
                misses.pop(seed.name, None)
                added += corpus.add_many((to_record(seed, p), p.html) for p in pages)  # kept as they come in
            else:
                misses[seed.name] = reason
            logger.info("golden_seller", seller=seed.name, pages=len(pages), reason="" if pages else reason)
    finally:
        await client.aclose()
        misses_path.write_text("".join(f"{n}\t{r}\n" for n, r in sorted(misses.items())))
    logger.info("golden_done", added=added, missed=len(misses))
    return added


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry", action="store_true", help="try the sellers that were missed before again")
    parser.add_argument("--only", nargs="+", default=[], help="collect only these sellers (seed names)")
    parser.add_argument("--index-only", action="store_true", help="only the archive's index (with --retry: misses)")
    parser.add_argument("--concurrency", type=int, default=4, help="sellers collected at the same time")
    args = parser.parse_args()
    asyncio.run(
        build(
            Corpus(),
            limit=args.limit,
            retry=args.retry,
            only=set(args.only),
            index_only=args.index_only,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()
