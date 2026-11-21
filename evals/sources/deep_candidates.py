"""
Candidate pages for labelling the deep check's new findings: listings where padded counts and inflated specs abound.

The claim audit, the photo check against the listing's own claims, and the deep checks after them add features the
scorer can only learn from labelled pages on which they fire. The corpus has few: its storefronts are 2022–2023 home
pages and its marketplace pages are synthetic. This collector gathers today's product pages, unlabelled
(``origin_dataset`` = :data:`DATASET`, ``source`` = ``live``, ``label`` ``None``), for the deep check
(:mod:`evals.candidates`) and a human to label in batches.

Pools (``notes.pool``), each from a list the corpus already has or from its private seed file
(``raw/deep_candidates_seeds.json``, never this repository: it names shops):

- ``marketplace``: third-party listings on general marketplaces whose ``robots.txt`` lets Unfret in and whose
  product pages read without a browser, found through their own sitemaps or category and search pages the
  ``robots.txt`` allows, filtered by the category words below. Seeds: ``{"marketplace": [{"name", "platform",
  "sitemaps": [...], "start": [...], "product": "regex", "pages": N}]}``.
- ``coi-recent``: shops on ČOI's risky e-shop list warned about from :data:`COI_FROM` on that are still up
  (``raw/coi_dates.json``).
- ``old-domain``: domains from the 2023 scam lists (BEYOND PHISH, the Mendeley set's fraudulent shops) that hold a
  shop today: often an expired domain with a new shop on it.
- ``search``: small shops found by web searches for the categories (seed ``search_shops``: ``[{"domain", "query"}]``).
- ``legit-us``, ``legit-eu``, ``big-brand``: established shops (``raw/us_shops_seeds.json``, the EU shop list of
  :mod:`evals.sources.eu_shops`) and well-known sellers (``raw/golden_seeds.json``), so the set isn't one-sided.

For a shop, the home page must be fetchable the way a URL-only check fetches it (:func:`unfret.page_fetch.fetch_page`,
only where ``robots.txt`` allows Unfret's user agent) and look like a shop; then the product URLs its sitemaps list
(or, without one, its home page links to) are ranked by the category words (:data:`CATEGORIES`) and tried until one
reads as a product page (:func:`evals.sources.us_shops.is_product_page`). One page per shop, one request at a time
per site with :data:`PAUSE_S` between requests. Pages the corpus already has (by URL) are skipped, and so are shops
it already has a candidate from. Misses are kept in ``raw/deep_candidates_misses.tsv`` and skipped on re-runs.

Usage: ``uv run python -m evals.sources.deep_candidates [--pool NAME ...] [--target N] [--seed 7]``
(``make deep-candidates``).
"""

import argparse
import asyncio
import csv
import gzip
import json
import random
import re
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from itertools import zip_longest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, now_iso, page_id
from evals.shopcheck import looks_like_shop
from evals.sources.eu_shops import SHOPS as EU_SHOPS
from evals.sources.eu_shops import robots_allows
from evals.sources.golden import candidates, product_score
from evals.sources.us_shops import is_product_page
from unfret.facts import parse_page
from unfret.market import shop_market
from unfret.net import fetch_public
from unfret.page_fetch import PageUnreadableError, fetch_page

logger = structlog.get_logger("unfret.evals")

DATASET = "deep-findings-2026-11"
SEEDS = Path("raw") / "deep_candidates_seeds.json"
MISSES = Path("raw") / "deep_candidates_misses.tsv"
COI_FROM = "20250101"
PAUSE_S = 1.0  # between two requests to the same site
MAX_TRIES = 4  # product URLs fetched per shop before giving up
MAX_SITEMAPS = 4  # sitemap files read per shop
_SITEMAP_BYTES = 24 * 1024 * 1024

# The categories where padded counts and inflated specs are common, by words in a product's URL or title (English,
# Dutch, German, Czech, French: the corpus's markets). The last group is "any set or multipack".
CATEGORIES: dict[str, tuple[str, ...]] = {
    "art_markers": (
        "marker",
        "markers",
        "stift",
        "stiften",
        "fixy",
        "fixa",
        "feutre",
        "felt",
        "brush-pen",
        "pens",
        "pen-set",
        "colored-pencil",
        "kleurpotlod",
        "buntstift",
        "pastelky",
        "acrylic-paint",
        "paint-set",
        "verf",
        "aquarel",
        "watercolor",
        "crayon",
        "drawing",
        "sketch",
    ),
    "tool_kits": (
        "tool-kit",
        "toolkit",
        "tool-set",
        "gereedschap",
        "werkzeug",
        "naradi",
        "nářadí",
        "screwdriver",
        "schroevendraai",
        "drill",
        "boor",
        "bohrer",
        "socket",
        "wrench",
        "sleutel",
        "bit-set",
        "bits",
        "ratchet",
        "plier",
    ),
    "power_banks": ("power-bank", "powerbank", "power bank", "mah", "battery-pack", "accu", "nabijecka", "nabíječka"),
    "chargers": (
        "charger",
        "oplader",
        "ladegerat",
        "ladegerät",
        "charging",
        "laadkabel",
        "usb-c",
        "gan",
        "fast-charge",
        "wireless-charg",
        "adapter",
        "chargeur",
    ),
    "led_lights": (
        "led",
        "lamp",
        "lampe",
        "licht",
        "light",
        "lights",
        "lampa",
        "svetlo",
        "světlo",
        "flashlight",
        "zaklamp",
        "taschenlampe",
        "solar",
        "strip",
        "lumen",
        "fairy",
        "lichtslinger",
        "headlamp",
        "torch",
    ),
    "storage_sets": (
        "storage",
        "organizer",
        "organiser",
        "opberg",
        "aufbewahrung",
        "box-set",
        "container",
        "vacuum-bag",
        "drawer",
        "úložn",
        "ulozn",
        "jars",
        "bins",
        "baskets",
    ),
    "cosmetics_sets": (
        "makeup",
        "make-up",
        "cosmetic",
        "cosmetica",
        "kosmetik",
        "kosmetika",
        "brush-set",
        "kwasten",
        "pinsel",
        "skincare",
        "serum",
        "nail",
        "nagel",
        "lipstick",
        "eyeshadow",
        "palette",
        "beauty",
        "perfume",
        "parfum",
    ),
    "sets_multipacks": ("pcs", "pieces", "stuks", "stuck", "stück", "ks", "pack", "set", "delig", "teilig", "kit"),
}
# A count or a spec in a URL or title ("52-pcs", "20000mah", "3-in-1"): what the claim audit and the photo check read.
CLAIM_RE = re.compile(
    r"\d+\s*-?\s*(?:pcs|pc|pieces?|stuks?|delig|teilig|st|ks|kusu|mah|wh|w|lm|lumens?|leds?|in-1|in1|x)(?![a-z])",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_LINE_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


def category_of(text: str) -> str:
    """
    The category a product's URL or title points at (the first that matches), or ``"other"``.

    :param text: A URL path or a title.
    :return: A key of :data:`CATEGORIES`, or ``"other"``.
    """
    lowered = unescape(text).lower().replace("_", "-")
    words = set(_WORD_RE.findall(lowered)) | set(re.findall(r"[a-z0-9]+", lowered))
    for name, keys in CATEGORIES.items():
        for key in keys:
            if (" " in key or "-" in key) and key in lowered:
                return name
            if key in words or (len(key) >= 6 and key in lowered):
                return name
    return "other"


def rank(urls: Iterable[str]) -> list[str]:
    """
    Product URLs: those stating a count or spec first, then by category (named categories before plain sets).

    :param urls: Candidate product URLs.
    :return: The same URLs, best first, without duplicates.
    """
    order = {name: i for i, name in enumerate(CATEGORIES)}
    unique = list(dict.fromkeys(urls))

    def key(url: str) -> tuple[bool, int, int]:
        path = urlsplit(url).path
        return not CLAIM_RE.search(path), order.get(category_of(path), len(order)), -product_score(path)

    return sorted(unique, key=key)


@dataclass
class Shop:
    """One shop (or marketplace seed) to take candidate pages from."""

    pool: str
    start: str
    prior: str = ""  # what the list it came from says: "scam-list", "legit-list", "" (unknown)
    platform: str = ""
    pages: int = 1
    sitemaps: list[str] = field(default_factory=list)
    product: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        """The shop's host without ``www.``."""
        return (urlsplit(self.start).hostname or "").removeprefix("www.")


@dataclass
class Found:
    """An accepted candidate page."""

    shop: Shop
    url: str
    html: str
    category: str
    market: str
    seller: str


class Site:
    """Requests to one site, one at a time with :data:`PAUSE_S` between them."""

    def __init__(self) -> None:
        """Start with no request made."""
        self._last = 0.0

    async def _turn(self) -> None:
        delay = self._last + PAUSE_S - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()

    async def page(self, url: str) -> tuple[str | None, str]:
        """
        Fetch a page the way a URL-only check does, where ``robots.txt`` allows it.

        :param url: The page.
        :return: ``(html, "")`` or ``(None, reason)``.
        """
        await self._turn()
        if not await robots_allows(url):
            return None, "robots"
        await self._turn()
        try:
            return (await fetch_page(url)).html, ""
        except PageUnreadableError as exc:
            return None, exc.reason
        except Exception as exc:  # noqa: BLE001 - one bad site must not end the run
            return None, type(exc).__name__

    async def raw(self, url: str) -> str:
        """
        Fetch a text file (``robots.txt``, a sitemap); empty when it can't be had.

        :param url: The file.
        :return: Its text.
        """
        await self._turn()
        try:
            body = await fetch_public(url, max_bytes=_SITEMAP_BYTES)
        except Exception:  # noqa: BLE001 - no file is the same as an empty one here
            return ""
        if body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except OSError:
                return ""
        return str(body.decode("utf-8", errors="replace"))


async def sitemap_urls(site: Site, base: str, sitemaps: list[str], product: str = "") -> list[str]:
    """
    Product URLs from a site's sitemaps: those ``robots.txt`` names, else ``/sitemap.xml``; product sitemaps first.

    :param site: The site's request pacing.
    :param base: The site's home page.
    :param sitemaps: Sitemaps to start from instead (a seed's).
    :param product: A regex a product URL's path must match, or ``""`` for the generic hints.
    :return: Product URLs on the same host.
    """
    parts = urlsplit(base)
    root = f"{parts.scheme}://{parts.netloc}"
    queue = list(sitemaps)
    if not queue:
        queue = _SITEMAP_LINE_RE.findall(await site.raw(f"{root}/robots.txt")) or [f"{root}/sitemap.xml"]
    pattern = re.compile(product) if product else None
    found: list[str] = []
    read = 0
    while queue and read < MAX_SITEMAPS:
        text = await site.raw(queue.pop(0))
        read += 1
        locs = [unescape(u) for u in _LOC_RE.findall(text)]
        if "<sitemapindex" in text[:2000].lower():
            children = sorted(locs, key=lambda u: ("product" not in u.lower(), "post" in u.lower()))
            queue = children[:MAX_SITEMAPS] + queue
            continue
        for url in locs:
            path = urlsplit(url).path
            if urlsplit(url).hostname != parts.hostname:
                continue
            if pattern.search(path) if pattern else product_score(path) >= 3 or "/products/" in path:
                found.append(url)
    return found


async def accept(url: str, html: str) -> tuple[bool, str, str, str]:
    """
    Whether a fetched page is a product page, and its category, market and seller.

    :param url: The page's URL.
    :param html: Its HTML.
    :return: ``(ok, category, market, seller)``.
    """
    facts, text = await asyncio.to_thread(parse_page, html, url)
    if not is_product_page(html, facts, text):
        return False, "", "", ""
    category = category_of(facts.title)
    if category == "other":
        category = category_of(urlsplit(url).path)
    return True, category, shop_market(facts, url, text), facts.seller


async def product_urls(site: Site, shop: Shop, home_html: str, rng: random.Random) -> list[str]:
    """
    A shop's product URLs to try, best first (a marketplace's spread over the categories).

    :param site: The site's request pacing.
    :param shop: The shop.
    :param home_html: Its start page, or ``""`` for a seed read from sitemaps only.
    :param rng: For sampling a marketplace's sitemap.
    :return: The URLs.
    """
    urls = await sitemap_urls(site, shop.start, shop.sitemaps, shop.product)
    if shop.pool == "marketplace" and len(urls) > 4000:
        urls = rng.sample(urls, 4000)
    linked = candidates(home_html, shop.start, shop.product) if home_html else []
    ordered = rank(urls + linked)
    if shop.pool != "marketplace":
        return ordered
    # Spread a marketplace's pages over the categories instead of taking the first category's.
    by_category: dict[str, list[str]] = {}
    for url in ordered:
        by_category.setdefault(category_of(urlsplit(url).path), []).append(url)
    return [u for group in zip_longest(*by_category.values()) for u in group if u is not None]


async def collect(shop: Shop, seen: set[str], rng: random.Random) -> tuple[list[Found], str]:
    """
    The candidate pages of one shop or marketplace seed.

    :param shop: The shop.
    :param seen: URLs the corpus has, or that were tried already (updated).
    :param rng: For sampling a marketplace's sitemap.
    :return: The pages found, and the reason when there are none.
    """
    site = Site()
    home_html = ""
    if not shop.sitemaps:
        home, why = await site.page(shop.start)
        if home is None:
            return [], why
        if shop.pool != "marketplace" and not looks_like_shop(home):
            return [], "not_a_shop"
        home_html = home
    ordered = await product_urls(site, shop, home_html, rng)
    found: list[Found] = []
    reason = "no_product_links" if not ordered else "no_product_page"
    tries = 0
    budget = MAX_TRIES * shop.pages
    for url in ordered:
        if len(found) >= shop.pages or tries >= budget:
            break
        if url in seen:
            continue
        seen.add(url)
        tries += 1
        html, why = await site.page(url)
        if html is None:
            reason = why
            continue
        ok, category, market, seller = await accept(url, html)
        if ok:
            found.append(Found(shop, url, html, category, market, seller))
    return found, reason


def to_record(page: Found) -> LabelRecord:
    """
    The corpus record of a candidate page: unlabelled, with where it came from.

    :param page: The page.
    :return: The record.
    """
    shop = page.shop
    notes: dict[str, Any] = {
        "pool": shop.pool,
        "category": page.category,
        "market": page.market,
        "prior": shop.prior,
        "fetched": "live",
        **shop.extra,
    }
    if shop.platform and page.seller:
        notes["marketplace_seller"] = page.seller
    return LabelRecord(
        id=page_id("dfc", page.url),
        label=None,
        platform=shop.platform,
        page_type="aggregate_listing" if shop.platform else "storefront",
        source="live",
        origin_dataset=DATASET,
        origin_url=page.url,
        collected_at=now_iso(),
        notes=json.dumps(notes, sort_keys=True),
    )


def _https(domain: str) -> str:
    domain = domain.strip().lower()
    if domain.startswith("http"):
        parts = urlsplit(domain)
        return f"https://{parts.netloc}/"
    return f"https://{domain}/"


def _marketplaces(seeds: dict[str, Any]) -> list[Shop]:
    out: list[Shop] = []
    for seed in seeds.get("marketplace", []):
        start = seed.get("start") or [seed["sitemaps"][0]]
        out += [
            Shop(
                "marketplace",
                url,
                platform=seed.get("platform", ""),
                pages=int(seed.get("pages", 10)) // max(1, len(start)),
                sitemaps=[] if seed.get("start") else seed.get("sitemaps", []),
                product=seed.get("product", ""),
                extra={"marketplace": seed["name"]},
            )
            for url in start
        ]
    return out


def _old_domains(raw: Path) -> list[Shop]:
    with (raw / "mendeley_shops.csv").open(newline="") as f:
        mendeley = {_https(r["Online shop URL"]) for r in csv.DictReader(f) if r["Label"] == "fraudulent"}
    with (raw / "beyondphish.jsonl").open() as f:
        beyond = {_https(json.loads(line)["domain"]) for line in f}
    out = [Shop("old-domain", u, "scam-list-2023", extra={"list": "mendeley"}) for u in sorted(mendeley)]
    return out + [
        Shop("old-domain", u, "scam-list-2023", extra={"list": "beyondphish"}) for u in sorted(beyond - mendeley)
    ]


def _big_brands(raw: Path) -> list[Shop]:
    return [
        Shop(
            "big-brand",
            seed["start"][-1],
            "legit-list",
            platform=seed.get("platform", ""),
            product=seed.get("product", ""),
            extra={"segment": seed.get("segment", "")},
        )
        for seed in json.loads((raw / "golden_seeds.json").read_text())
    ]


def shops(corpus: Corpus, pool: str, rng: random.Random) -> list[Shop]:
    """
    The shops of one pool, shuffled (a marketplace's in seed order: each is a quota).

    :param corpus: The corpus (its ``raw/`` lists and seed file).
    :param pool: The pool's name.
    :param rng: The shuffle's randomness.
    :return: The shops.
    """
    raw = corpus.root / "raw"
    seeds: dict[str, Any] = json.loads((corpus.root / SEEDS).read_text()) if (corpus.root / SEEDS).exists() else {}
    if pool == "marketplace":
        return _marketplaces(seeds)
    out: list[Shop] = []
    if pool == "coi-recent":
        dates: dict[str, str] = json.loads((raw / "coi_dates.json").read_text())
        out = [Shop(pool, _https(d), "scam-list", extra={"warned": w}) for d, w in dates.items() if w >= COI_FROM]
    elif pool == "old-domain":
        out = _old_domains(raw)
    elif pool == "search":
        out = [Shop(pool, _https(s["domain"]), "", extra={"query": s["query"]}) for s in seeds.get("search_shops", [])]
    elif pool == "legit-us":
        items = json.loads((raw / "us_shops_seeds.json").read_text())
        out = [Shop(pool, _https(s["domain"]), "legit-list", extra={"origin": s["origin"]}) for s in items]
    elif pool == "legit-eu":
        out = [Shop(pool, url, "legit-list", extra={"country": c}) for c, url in EU_SHOPS]
    elif pool == "big-brand":
        out = _big_brands(raw)
    rng.shuffle(out)
    return out


POOLS: tuple[str, ...] = ("marketplace", "coi-recent", "old-domain", "search", "legit-us", "legit-eu", "big-brand")


def read_misses(path: Path) -> set[str]:
    """
    The shops tried before without a page.

    :param path: The misses file (``pool``, ``start``, ``reason`` per line).
    :return: Their start URLs.
    """
    if not path.exists():
        return set()
    return {line.split("\t")[1] for line in path.read_text().splitlines() if line.count("\t") >= 2}


async def build(
    corpus: Corpus, pools: list[str], targets: dict[str, int], seed: int, concurrency: int
) -> Counter[str]:
    """
    Collect candidate pages, pool by pool, until each pool's target.

    :param corpus: The corpus to add to.
    :param pools: The pools to collect.
    :param targets: Pages wanted per pool.
    :param seed: The shuffle's seed.
    :param concurrency: Shops fetched at the same time (each one request at a time).
    :return: Pages added per pool.
    """
    rng = random.Random(seed)
    records = corpus.labels().values()
    seen = {r.origin_url for r in records}
    hosts = Counter(
        (urlsplit(r.origin_url).hostname or "").removeprefix("www.") for r in records if r.origin_dataset == DATASET
    )
    have = Counter(json.loads(r.notes).get("pool", "") for r in records if r.origin_dataset == DATASET)
    misses_path = corpus.root / MISSES
    misses = read_misses(misses_path)
    added: Counter[str] = Counter()
    for pool in pools:
        want = targets.get(pool, 0) - have[pool]
        if want <= 0:
            continue
        todo = [s for s in shops(corpus, pool, rng) if s.start not in misses and (s.platform or not hosts[s.host])]
        semaphore = asyncio.Semaphore(1 if pool == "marketplace" else concurrency)
        done = 0
        stop = asyncio.Event()

        async def one(
            shop: Shop, pool: str = pool, semaphore: asyncio.Semaphore = semaphore, stop: asyncio.Event = stop
        ) -> None:
            nonlocal done, want
            async with semaphore:
                if stop.is_set():
                    return
                found, reason = await collect(shop, seen, rng)
                found = found[: max(0, want)]
                if found:
                    corpus.add_many((to_record(p), p.html) for p in found)
                    want -= len(found)
                    added[pool] += len(found)
                    hosts[shop.host] += 1
                    if want <= 0:
                        stop.set()
                elif not shop.platform:
                    with misses_path.open("a") as f:
                        f.write(f"{pool}\t{shop.start}\t{reason}\n")
                done += 1
                if done % 25 == 0:
                    logger.info("candidates_progress", pool=pool, tried=done, added=added[pool], left=want)

        await asyncio.gather(*(one(s) for s in todo))
        logger.info("candidates_pool_done", pool=pool, added=added[pool])
    return added


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--pool", nargs="+", choices=POOLS, default=list(POOLS))
    parser.add_argument(
        "--target",
        nargs="+",
        default=[],
        metavar="POOL=N",
        help="pages wanted per pool (default: marketplace=200 coi-recent=120 old-domain=60 search=40 legit-us=80 "
        "legit-eu=50 big-brand=50)",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    targets = {
        "marketplace": 200,
        "coi-recent": 120,
        "old-domain": 60,
        "search": 40,
        "legit-us": 80,
        "legit-eu": 50,
        "big-brand": 50,
    }
    for item in args.target:
        name, _, n = item.partition("=")
        targets[name] = int(n)
    started = datetime.now(UTC)
    added = asyncio.run(build(Corpus(), args.pool, targets, args.seed, args.concurrency))
    print(  # noqa: T201 - command-line report
        f"added {sum(added.values())} candidate pages in {datetime.now(UTC) - started}: "
        + ", ".join(f"{k} {v}" for k, v in sorted(added.items()))
    )


if __name__ == "__main__":
    main()
