"""
Long-established trade businesses (``service_site``, ``legit``) found on OpenStreetMap, from the Internet Archive.

OpenStreetMap (ODbL) maps tradespeople as ``craft=plumber|locksmith|electrician|hvac|roofer|…`` with their
``website``: a business volunteers saw at a real address. The Netherlands and a set of US states are queried with
the Overpass API (one query per area, with backoff between mirrors). A business counts as **long-established**
when the Internet Archive's first capture of its domain is from before :data:`ESTABLISHED_BEFORE`; its home page
is then taken from the capture closest to :data:`CAPTURE_AT` (the same era as the ACM rogue-trader captures, so the
two labels don't differ by date), raw (``id_``), about one request a second.

Only what the site itself publishes is stored (a business's own web page); the OpenStreetMap record contributes the
craft, the country and the element id. Labels are dataset labels until the second opinion agrees. National chains
and franchises can appear; they are legitimate too.

Usage: ``uv run python -m evals.sources.osm_trades [--per-country 120] [--seed 5]``.
"""

import argparse
import asyncio
import json
import random
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.sources._http import PoliteClient
from evals.sources.wayback import CDX_URL, closest_snapshot, fetch_snapshot
from unfret.html_cleaner import clean_html

logger = structlog.get_logger("unfret.evals")

DATASET = "osm-established-trades"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
CRAFTS = ("plumber", "locksmith", "electrician", "hvac", "roofer", "painter", "carpenter", "glaziery", "tiler")
ESTABLISHED_BEFORE = "2015"  # first archived before 2015: at least five years old at the 2020 capture
CAPTURE_AT = "20200701"
# Overpass area filters: the Netherlands, and US states spread over the country.
AREAS: dict[str, tuple[str, ...]] = {
    "NL": ('area["ISO3166-1"="NL"][admin_level=2]',),
    "US": tuple(
        f'area["ISO3166-2"="US-{s}"]' for s in ("TX", "CA", "WA", "OR", "CO", "MN", "IL", "NC", "FL", "OH", "MA", "PA")
    ),
}
# Directories, platforms and social sites are not the business's own website.
_NOT_OWN_SITE = re.compile(
    r"facebook\.|instagram\.|linkedin\.|google\.|yelp\.|werkspot\.|thumbtack\.|angi\.|homeadvisor\.|bbb\.org"
    r"|nextdoor\.|houzz\.|telefoonboek\.|goudengids\.|youtube\.|twitter\.|x\.com",
    re.IGNORECASE,
)
_MIN_TEXT = 300


@dataclass(frozen=True)
class Trade:
    """One business from OpenStreetMap."""

    osm_id: str
    country: str
    craft: str
    website: str
    domain: str


def overpass_query(area: str) -> str:
    """The Overpass QL query for every craft business with a website in one area."""
    crafts = "|".join(CRAFTS)
    return f'[out:json][timeout:120];{area}->.a;(nwr["craft"~"^({crafts})$"]["website"](area.a););out tags;'


def domain_of(website: str) -> str:
    """The registrable-looking host of a website tag (``www.`` dropped), or ``""`` when it isn't a web address."""
    value = website.strip()
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "http://" + value
    host = (urlsplit(value).hostname or "").lower().removeprefix("www.")
    return host if "." in host else ""


def trades_from_overpass(data: dict[str, object], country: str) -> list[Trade]:
    """
    The usable businesses in an Overpass response: a craft, a website of their own, one per domain.

    :param data: The parsed JSON response.
    :param country: ``NL`` or ``US``.
    :return: The businesses.
    """
    out: dict[str, Trade] = {}
    for element in data.get("elements", []):  # type: ignore[attr-defined]
        tags = element.get("tags", {})
        website = str(tags.get("website") or tags.get("contact:website") or "")
        craft = str(tags.get("craft", ""))
        domain = domain_of(website)
        if not domain or craft not in CRAFTS or _NOT_OWN_SITE.search(domain):
            continue
        out.setdefault(domain, Trade(f"{element.get('type')}/{element.get('id')}", country, craft, website, domain))
    return list(out.values())


async def fetch_trades(client: PoliteClient, country: str) -> list[Trade]:
    """
    Query every area of a country, trying the Overpass mirrors in turn with backoff.

    :param client: A rate-limited client.
    :param country: ``NL`` or ``US``.
    :return: The businesses, one per domain.
    """
    found: dict[str, Trade] = {}
    for area in AREAS[country]:
        query = overpass_query(area)
        for attempt in range(6):
            endpoint = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
            response = await client.get(endpoint, params={"data": query})
            if response is not None and response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("elements") is not None and not data.get("remark"):
                    for trade in trades_from_overpass(data, country):
                        found.setdefault(trade.domain, trade)
                    break
            await asyncio.sleep(10 * (attempt + 1))
        logger.info("osm_area", country=country, area=area, total=len(found))
    return list(found.values())


async def first_capture(client: PoliteClient, domain: str) -> str | None:
    """
    The timestamp of the Internet Archive's earliest 200-status capture of a domain's pages.

    :param client: A rate-limited client.
    :param domain: The domain.
    :return: ``YYYYMMDDhhmmss``, or ``None`` when it was never archived.
    """
    response = await client.get(
        CDX_URL,
        params={
            "url": domain,
            "filter": "statuscode:200",
            "fl": "timestamp",
            "output": "json",
            "limit": "1",
        },
    )
    if response is None or response.status_code != 200:
        return None
    try:
        rows = response.json()
    except ValueError:
        return None
    return str(rows[1][0]) if len(rows) > 1 else None


async def collect(client: PoliteClient, trade: Trade) -> tuple[LabelRecord, str] | None:
    """
    Check one business's age and fetch its 2020 home page.

    :param client: A rate-limited client.
    :param trade: The business.
    :return: The record and HTML, or ``None`` when it is too young, not archived or not readable.
    """
    first = await first_capture(client, trade.domain)
    if first is None or first[:4] >= ESTABLISHED_BEFORE:
        return None
    snapshot = await closest_snapshot(client, trade.domain + "/", CAPTURE_AT)
    if snapshot is None:
        return None
    timestamp, original = snapshot
    html = await fetch_snapshot(client, timestamp, original)
    if html is None or len(clean_html(html)) < _MIN_TEXT:
        return None
    notes = {
        "craft": trade.craft,
        "country": trade.country,
        "osm": trade.osm_id,
        "first_archived": first[:8],
        "synthetic": False,
    }
    record = LabelRecord(
        id=page_id("osm", trade.domain),
        label="legit",
        label_source="dataset",
        prior_label="legit",
        confidence=0.8,
        page_type="service_site",
        source="wayback",
        origin_dataset=DATASET,
        origin_url=original,
        origin_id=trade.osm_id,
        collected_at=timestamp[:8],
        notes=json.dumps(notes, sort_keys=True),
    )
    return record, html


async def build(corpus: Corpus, per_country: int, seed: int) -> int:
    """
    Find, age-check and fetch up to ``per_country`` businesses per country.

    :param corpus: The corpus.
    :param per_country: Pages to add per country at most.
    :param seed: Seed for the order businesses are tried in.
    :return: The number of pages stored.
    """
    client = PoliteClient(interval=1.0, timeout=150.0)
    existing = set(corpus.labels())
    added: list[tuple[LabelRecord, str]] = []
    try:
        for country in AREAS:
            trades = sorted(await fetch_trades(client, country), key=lambda t: t.domain)
            random.Random(seed).shuffle(trades)
            kept = 0
            for trade in trades:
                if kept >= per_country:
                    break
                if page_id("osm", trade.domain) in existing:
                    kept += 1
                    continue
                result = await collect(client, trade)
                if result is not None:
                    added.append(result)
                    kept += 1
                    if kept % 10 == 0:
                        logger.info("osm_progress", country=country, kept=kept)
            logger.info("osm_country_done", country=country, candidates=len(trades), kept=kept)
    finally:
        await client.aclose()
    stored = corpus.add_many(added) if added else 0
    logger.info("osm_trades_done", stored=stored)
    return stored


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--per-country", type=int, default=120)
    parser.add_argument("--seed", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(build(Corpus(), args.per_country, args.seed))


if __name__ == "__main__":
    main()
