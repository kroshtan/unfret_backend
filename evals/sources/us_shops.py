"""
Product pages of long-established US shops (``storefront``, ``legit``), from the Internet Archive.

The corpus's US storefronts were almost all scams (92.5%) and its legit storefronts almost all European, so "a US
shop" predicted the label on its own. This source adds real product pages of established US shops across segments
and platforms (Shopify brands, WooCommerce, Magento and BigCommerce independents, mid-size and large retailers):

- **Seeds** (public, licence-clean): Wikidata (CC0) companies in the United States with an official website, in a
  retail or consumer-goods industry and not dissolved; and OpenStreetMap (ODbL) independent shops (no ``brand``
  tag) with a website in a set of US states.
- **Product page**: the shop's home page as archived closest to :data:`CAPTURE_AT` (within :data:`CAPTURE_FROM` to
  :data:`CAPTURE_TO`), then one of the product pages it links to (``/products/…``, ``/product/…``, ``/p/…``,
  ``/shop/…``, ``/item/…``; through one category page when the home page links none), as archived closest to the
  home page. It counts only when its raw HTML reads as a product page (a price in the page facts, product JSON-LD or
  ``og:type`` product or a title, a cart) of a shop selling into the US market (:func:`unfret.market.shop_market`).
  The archive's redirects find the captures, so the slow index (CDX) is never queried.
- **Established**: the archive's first capture of the domain's home page is at least :data:`MIN_AGE_YEARS` years
  older than the product capture, and a Wikidata founding date, when there is one, too.

About one request a second, raw ``id_`` captures only (the archive's robots.txt sets no limits). Only the shop's
own page is stored; the seed contributes the company's Wikidata id or the shop's OpenStreetMap element and its
segment. Labels are dataset labels until the second opinion confirms them (:mod:`evals.second_opinion`, strict for
this dataset: a disagreement drops the page). Misses are kept in ``raw/us_shops_misses.txt`` and skipped on re-runs.

Usage: ``uv run python -m evals.sources.us_shops [--target 330] [--seed 11]``.
"""

import argparse
import asyncio
import json
import random
import re
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.shopcheck import shop_signals
from evals.sources._http import PoliteClient
from evals.sources.osm_trades import OVERPASS_ENDPOINTS, domain_of
from evals.sources.wayback import earliest_capture, fetch_closest
from unfret.facts import PageFacts, parse_page
from unfret.market import shop_market

logger = structlog.get_logger("unfret.evals")

DATASET = "us-established-shops"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
# Retail and consumer-goods industries (P452): retail, e-commerce, online shop, fashion, clothing, cosmetics,
# personal care products, consumer goods, furniture, sporting goods and sports equipment, jewellery, footwear,
# bicycles, kitchenware, bookselling, musical instruments, coffee, direct-to-consumer.
INDUSTRIES = [
    "Q126793",
    "Q484847",
    "Q4382945",
    "Q12684",
    "Q11828862",
    "Q11460",
    "Q12752882",
    "Q3246832",
    "Q56573357",
    "Q11990434",
    "Q24928712",
    "Q768186",
    "Q57262277",
    "Q5915560",
    "Q17018236",
    "Q1521410",
    "Q998426",
    "Q1955150",
    "Q10302044",
    "Q368344",
    "Q5280224",
]
WIKIDATA_QUERY = (
    "SELECT ?c ?site ?inc ?ind WHERE { VALUES ?ind { "
    + " ".join(f"wd:{q}" for q in INDUSTRIES)
    + " } ?c wdt:P452 ?ind; wdt:P17 wd:Q30; wdt:P856 ?site . OPTIONAL { ?c wdt:P571 ?inc }"
    " FILTER NOT EXISTS { ?c wdt:P576 ?dissolved } }"
)
# Independent shops that sell goods a shopper might order online.
OSM_SHOPS = (
    "clothes",
    "shoes",
    "outdoor",
    "bicycle",
    "books",
    "jewelry",
    "gift",
    "furniture",
    "cosmetics",
    "sports",
    "toys",
    "musical_instrument",
    "hardware",
    "houseware",
    "kitchen",
    "art",
    "craft",
    "pet",
    "tea",
    "coffee",
    "chocolate",
    "fabric",
    "boutique",
    "bag",
    "leather",
    "watches",
    "games",
    "hobby",
    "model",
    "fishing",
    "hunting",
    "garden_centre",
    "bed",
    "lighting",
    "electronics",
    "stationery",
)
OSM_STATES = ("CA", "NY", "TX", "WA", "OR", "CO", "MA", "IL", "PA", "NC", "VT", "MN", "GA", "FL")
CAPTURE_AT = "20230701"  # the Mendeley shops' collection date, so the labels don't differ by date
CAPTURE_FROM = "20210101"
CAPTURE_TO = "20250630"
MIN_AGE_YEARS = 5
PRODUCT_PATHS = ("/products/", "/product/", "/p/", "/shop/", "/item/")
# Marketplaces, social sites and platforms are not a shop's own website.
_NOT_OWN_SITE = re.compile(
    r"facebook\.|instagram\.|linkedin\.|google\.|yelp\.|etsy\.|ebay\.|amazon\.|youtube\.|twitter\.|x\.com"
    r"|square\.site|wixsite\.|weebly\.|myshopify\.|tiktok\.|pinterest\.",
    re.IGNORECASE,
)
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-%.]{2,120}/?$")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#\s]+)""", re.IGNORECASE)
_CATEGORY_RE = re.compile(r"/(?:collections|category|product-category|categories|c|shop|catalog)/[^/?#]+/?$")
_NOT_PRODUCT = (".json", ".js", ".xml", ".oembed", ".atom", ".jpg", ".png", ".pdf")
_PLATFORMS = (
    ("shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.IGNORECASE)),
    ("woocommerce", re.compile(r"woocommerce", re.IGNORECASE)),
    ("magento", re.compile(r"Magento_|mage/cookies|/static/version\d+/frontend/", re.IGNORECASE)),
    ("bigcommerce", re.compile(r"bigcommerce\.com|cdn\d*\.bigcommerce", re.IGNORECASE)),
    ("salesforce", re.compile(r"demandware\.(?:static|store)|/on/demandware", re.IGNORECASE)),
    ("squarespace", re.compile(r"static1\.squarespace\.com", re.IGNORECASE)),
    ("wix", re.compile(r"static\.wixstatic\.com|wix-code", re.IGNORECASE)),
)
# No platform may take more than this share of the pages, so the legit side doesn't become "a Shopify shop".
_MAX_PLATFORM_SHARE = 0.4
_PARALLEL = 4


@dataclass(frozen=True)
class Seed:
    """One shop to try: its domain and where it came from."""

    domain: str
    origin: str  # "wikidata" or "osm"
    ref: str  # Wikidata id or OpenStreetMap element
    segment: str  # Wikidata industry id or OSM shop tag
    founded: str = ""  # Wikidata inception year, when known


def seeds_from_wikidata(data: dict[str, object]) -> list[Seed]:
    """
    The usable companies in a Wikidata SPARQL response, one per domain, with their earliest founding year.

    :param data: The parsed ``application/sparql-results+json`` response.
    :return: The seeds.
    """
    out: dict[str, Seed] = {}
    for row in data.get("results", {}).get("bindings", []):  # type: ignore[attr-defined]
        domain = domain_of(row.get("site", {}).get("value", ""))
        if not domain or _NOT_OWN_SITE.search(domain):
            continue
        qid = row["c"]["value"].rsplit("/", 1)[-1]
        industry = row.get("ind", {}).get("value", "").rsplit("/", 1)[-1]
        year = row.get("inc", {}).get("value", "")[:4]
        year = year if year.isdigit() else ""
        old = out.get(domain)
        if old is None:
            out[domain] = Seed(domain, "wikidata", qid, industry, year)
        elif year and (not old.founded or year < old.founded):
            out[domain] = Seed(domain, "wikidata", old.ref, old.segment, year)
    return list(out.values())


def osm_query(state: str) -> str:
    """The Overpass QL query for independent shops with a website in one US state."""
    shops = "|".join(OSM_SHOPS)
    return (
        f'[out:json][timeout:180];area["ISO3166-2"="US-{state}"]->.a;'
        f'(nwr["shop"~"^({shops})$"]["website"][!"brand"][!"brand:wikidata"](area.a););out tags;'
    )


def seeds_from_overpass(data: dict[str, object]) -> list[Seed]:
    """
    The usable shops in an Overpass response: an independent shop with a website of its own, one per domain.

    :param data: The parsed JSON response.
    :return: The seeds.
    """
    out: dict[str, Seed] = {}
    for element in data.get("elements", []):  # type: ignore[attr-defined]
        tags = element.get("tags", {})
        domain = domain_of(str(tags.get("website") or tags.get("contact:website") or ""))
        shop = str(tags.get("shop", ""))
        if not domain or shop not in OSM_SHOPS or _NOT_OWN_SITE.search(domain):
            continue
        out.setdefault(domain, Seed(domain, "osm", f"{element.get('type')}/{element.get('id')}", shop))
    return list(out.values())


def _same_site(url: str, base: str) -> bool:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return bool(host) and host == (urlsplit(base).hostname or "").removeprefix("www.")


def product_path(path: str) -> str | None:
    """
    A link path reduced to one product's page, or ``None`` when it isn't one.

    ``/collections/x/products/mug`` becomes Shopify's canonical ``/products/mug``; otherwise the path must be exactly
    one slug under one of :data:`PRODUCT_PATHS`.

    :param path: A URL path.
    :return: The product page path, or ``None``.
    """
    if m := re.search(r"/products/[^/]+/?$", path):
        path = m.group(0)
    for prefix in PRODUCT_PATHS:
        slug = path[len(prefix) :] if path.startswith(prefix) else ""
        if _SLUG_RE.match(slug) and not slug.rstrip("/").lower().endswith(_NOT_PRODUCT):
            return path
    return None


def page_links(html: str, base: str) -> tuple[list[str], list[str]]:
    """
    The product and category pages a page links to on its own site, without query strings, in page order.

    :param html: The page's raw HTML (an ``id_`` capture keeps the original links).
    :param base: The page's URL, for relative links.
    :return: ``(product URLs, category URLs)``.
    """
    products: dict[str, None] = {}
    categories: dict[str, None] = {}
    for href in _HREF_RE.findall(html):
        url = urljoin(base, unescape(href))
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not _same_site(url, base):
            continue
        if (path := product_path(parts.path)) is not None:
            products.setdefault(f"{parts.scheme}://{parts.netloc}{path}", None)
        elif _CATEGORY_RE.search(parts.path):
            categories.setdefault(f"{parts.scheme}://{parts.netloc}{parts.path}", None)
    return list(products), list(categories)


def platform_of(html: str) -> str:
    """The shop platform a page was built with, or ``custom``."""
    return next((name for name, pattern in _PLATFORMS if pattern.search(html)), "custom")


def is_product_page(html: str, facts: PageFacts, text: str) -> bool:
    """
    Whether a page reads as one product's page in a shop: a price, product markup or a product title, and a cart.

    :param html: The raw HTML.
    :param facts: Its page facts.
    :param text: Its visible text.
    :return: ``True`` for a product page.
    """
    product_markup = any("product" in str(item.get("@type", "")).lower() for item in facts.json_ld) or bool(
        re.search(r"""property=["']og:type["']\s+content=["'](?:og:)?product""", html, re.IGNORECASE)
    )
    signals = shop_signals(html)
    return (
        facts.price is not None
        and (product_markup or bool(facts.title))
        and len(text) >= 200
        and (signals["cart"] > 0 or signals["platform"] > 0)
    )


def old_enough(first: str, capture: str, founded: str) -> bool:
    """
    Whether a shop was established at capture time: first archived, and founded if known, five years earlier.

    :param first: The domain's first capture, ``YYYYMMDD…``.
    :param capture: The product page's capture, ``YYYYMMDD…``.
    :param founded: The Wikidata founding year, or ``""``.
    :return: ``True`` when old enough.
    """
    limit = int(capture[:4]) - MIN_AGE_YEARS
    archived_early = (int(first[:4]), first[4:8]) <= (limit, capture[4:8])
    return archived_early and (not founded or int(founded) <= limit)


async def fetch_seeds(client: PoliteClient) -> list[Seed]:
    """
    Query Wikidata and OpenStreetMap for candidate shops.

    :param client: A rate-limited client.
    :return: All seeds, one per domain (Wikidata first when a domain is in both).
    """
    found: dict[str, Seed] = {}
    response = await client.get(
        WIKIDATA_SPARQL, params={"query": WIKIDATA_QUERY}, headers={"Accept": "application/sparql-results+json"}
    )
    if response is not None and response.status_code == 200:
        for seed in seeds_from_wikidata(response.json()):
            found.setdefault(seed.domain, seed)
    logger.info("us_shops_wikidata", seeds=len(found))
    for state in OSM_STATES:
        for attempt in range(6):
            endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
            response = await client.get(endpoint, params={"data": osm_query(state)})
            data = None
            if response is not None and response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = None
            if isinstance(data, dict) and data.get("elements") is not None and not data.get("remark"):
                for seed in seeds_from_overpass(data):
                    found.setdefault(seed.domain, seed)
                break
            await asyncio.sleep(10 * (attempt + 1))
        logger.info("us_shops_osm", state=state, seeds=len(found))
    return list(found.values())


def _within(timestamp: str, start: str, end: str) -> bool:
    return start <= timestamp[:8] <= end


async def product_links(client: PoliteClient, home: tuple[str, str, str], rng: random.Random) -> list[str]:
    """
    The product pages a home page links to, or, when it links none, those of one of its category pages.

    :param client: A rate-limited client.
    :param home: The home page capture: ``(timestamp, URL, HTML)``.
    :param rng: Picks the category page.
    :return: Product page URLs.
    """
    products, categories = page_links(home[2], home[1])
    if not products and categories:
        category = await fetch_closest(client, home[0][:8], rng.choice(categories[:20]))
        if category is not None:
            products, _ = page_links(category[2], category[1])
    return products


async def collect(client: PoliteClient, seed: Seed, rng: random.Random) -> tuple[LabelRecord, str] | str:
    """
    Find, age-check and fetch one archived product page of a shop.

    :param client: A rate-limited client.
    :param seed: The shop.
    :param rng: Picks among the product links.
    :return: The record and HTML, or the reason it was skipped.
    """
    first = await earliest_capture(client, seed.domain)
    home = await fetch_closest(client, CAPTURE_AT, f"http://{seed.domain}/") if first else None
    if first is None or home is None or not _within(home[0], CAPTURE_FROM, CAPTURE_TO):
        return "no home page capture in the period"
    if not old_enough(first, home[0], seed.founded):
        return "too young"
    products = await product_links(client, home, rng)
    if not products:
        return "no product links"
    for url in rng.sample(products, min(2, len(products))):
        capture = await fetch_closest(client, home[0][:8], url)
        if capture is None or not old_enough(first, capture[0], seed.founded):
            continue
        timestamp, original, html = capture
        if not _within(timestamp, CAPTURE_FROM, CAPTURE_TO) or product_path(urlsplit(original).path) is None:
            continue
        facts, text = await asyncio.to_thread(parse_page, html, original)
        if not is_product_page(html, facts, text):
            continue
        if shop_market(facts, original, text) != "us":
            return "not the US market"
        notes = asdict(seed) | {"first_archived": first[:8], "platform": platform_of(html)}
        record = LabelRecord(
            id=page_id("uss", seed.domain),
            label="legit",
            label_source="dataset",
            prior_label="legit",
            confidence=0.8,
            page_type="storefront",
            source="wayback",
            origin_dataset=DATASET,
            origin_url=original,
            origin_id=seed.ref,
            collected_at=timestamp[:8],
            notes=json.dumps(notes, sort_keys=True),
        )
        return record, html
    return "no product page"


def interleave(seeds: list[Seed], rng: random.Random) -> list[Seed]:
    """
    The order to try shops in: Wikidata and OpenStreetMap seeds alternately, each shuffled, then the rest.

    :param seeds: All seeds.
    :param rng: Shuffles each origin's seeds.
    :return: The seeds in trying order.
    """
    by_origin = {o: [s for s in seeds if s.origin == o] for o in ("wikidata", "osm")}
    for group in by_origin.values():
        rng.shuffle(group)
    order = [s for pair in zip(*by_origin.values(), strict=False) for s in pair]
    longest = max(by_origin.values(), key=len)
    return order + longest[len(order) // 2 :]


async def load_seeds(client: PoliteClient, path: Path) -> list[Seed]:
    """
    The seeds saved by an earlier run, or freshly fetched (and saved) on the first one.

    :param client: A rate-limited client.
    :param path: Where the seeds are kept.
    :return: The seeds, sorted by domain.
    """
    if path.exists():
        return [Seed(**s) for s in json.loads(path.read_text())]
    seeds = sorted(await fetch_seeds(client), key=lambda s: s.domain)
    path.write_text(json.dumps([asdict(s) for s in seeds], indent=1) + "\n")
    return seeds


async def build(corpus: Corpus, target: int, seed_value: int) -> int:
    """
    Add up to ``target`` product pages, alternating Wikidata and OpenStreetMap seeds and capping each platform.

    :param corpus: The corpus.
    :param target: Pages of this dataset to have at most (pages already stored count).
    :param seed_value: Seed for the order shops are tried in.
    :return: The number of pages stored in this run.
    """
    raw = corpus.root / "raw"
    raw.mkdir(exist_ok=True)
    seeds_path = raw / "us_shops_seeds.json"
    misses_path = raw / "us_shops_misses.txt"
    client = PoliteClient(interval=1.0, timeout=200.0)
    stored = 0
    try:
        seeds = await load_seeds(client, seeds_path)
        misses = (
            dict(line.split("\t", 1) for line in misses_path.read_text().splitlines()) if misses_path.exists() else {}
        )
        order = interleave(seeds, random.Random(seed_value))
        mine = [r for r in corpus.labels().values() if r.origin_dataset == DATASET]
        platforms = [json.loads(r.notes).get("platform", "custom") for r in mine]
        done = {r.id for r in mine}
        batch: list[tuple[LabelRecord, str]] = []
        pending = [s for s in order if page_id("uss", s.domain) not in done and s.domain not in misses]
        for start in range(0, len(pending), _PARALLEL):
            if len(done) >= target:
                break
            chunk = pending[start : start + _PARALLEL]
            # A few shops at a time, so the client's one request a second isn't spent waiting on slow captures.
            results = await asyncio.gather(
                *(collect(client, s, random.Random(f"{seed_value}:{s.domain}")) for s in chunk)
            )
            for seed, result in zip(chunk, results, strict=True):
                reason = result if isinstance(result, str) else ""
                if not isinstance(result, str):
                    if len(done) >= target:
                        break
                    platform = json.loads(result[0].notes)["platform"]
                    if platforms.count(platform) >= max(10, _MAX_PLATFORM_SHARE * target):
                        reason = f"platform cap ({platform})"
                if reason or isinstance(result, str):
                    misses[seed.domain] = reason
                    with misses_path.open("a") as f:
                        f.write(f"{seed.domain}\t{reason}\n")
                    continue
                platforms.append(platform)
                done.add(result[0].id)
                batch.append(result)
            if len(batch) >= 10:
                stored += corpus.add_many(batch)
                batch.clear()
                logger.info("us_shops_progress", stored=len(done), tried=len(misses) + len(done))
        if batch:
            stored += corpus.add_many(batch)
    finally:
        await client.aclose()
    logger.info("us_shops_done", stored=stored)
    return stored


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--target", type=int, default=330)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    asyncio.run(build(Corpus(), args.target, args.seed))


if __name__ == "__main__":
    main()
