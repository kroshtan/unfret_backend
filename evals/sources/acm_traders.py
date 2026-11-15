"""
Rogue trader websites named in public ACM decisions (``service_site``, label ``scam``), from the Internet Archive.

The Dutch consumer authority ACM published three decisions against networks of fake "local" emergency-trade
websites, naming the sites:

- **KeyService Nederland / Plumberservice** (fine, January 2022): locksmith sites under made-up local names
  ("Slotenmaker Gerard", "Slotenmaker Jan Willem", …) with fake reviews and fake quality checks.
- **Allfree** (order under penalty, August 2021): lead-generation sites with a town in the domain name
  (``deventer-slotenmaker.nl``, …) and no company details.
- **Leadle** (order under penalty, August 2021): the same pattern for locksmiths and plumbers, with a made-up
  quality mark.

Each site's captures from the period the decision covers are listed with the Wayback CDX API; per site the home
page and up to ``--per-site`` city and content pages (the doorway pages) are fetched raw (``id_``, no toolbar), at
about one request a second. Policy pages, assets and pages that don't read as a trade site are skipped. Labels are
dataset labels (``label_source="dataset"``): the decision is the authority; the second opinion still has to agree
before a page counts. ``loodgieters.nl`` is used only up to June 2021, when the ACM decision says a genuine plumber
took the domain over; ``slotenmaker-strijbos.nl`` is left out because KeyService denied it was theirs.

Usage: ``uv run python -m evals.sources.acm_traders [--per-site 8]``.
"""

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.sources._http import PoliteClient
from evals.sources.wayback import CDX_URL, fetch_snapshot
from unfret.html_cleaner import clean_html

logger = structlog.get_logger("unfret.evals")

DATASET = "acm-rogue-traders"

KEYSERVICE = (
    "https://www.acm.nl/sites/default/files/documents/"
    "besluit-acm-legt-boete-op-aan-keyservice-voor-misleidende-en-agressieve-praktijken.pdf"
)
ALLFREE = "https://www.acm.nl/sites/default/files/documents/last-onder-dwangsom-allfree-bv.pdf"
LEADLE = "https://www.acm.nl/sites/default/files/documents/last-onder-dwangsom-leadle-bv.pdf"


@dataclass(frozen=True)
class Decision:
    """An ACM decision and the period its findings cover (``YYYYMMDD``)."""

    url: str
    operator: str
    start: str
    end: str


DECISIONS = {
    "keyservice": Decision(KEYSERVICE, "KeyService Nederland / Plumberservice", "20190101", "20220131"),
    "allfree": Decision(ALLFREE, "Allfree", "20200101", "20211231"),
    "leadle": Decision(LEADLE, "Leadle", "20190101", "20210630"),
}

# (domain, decision key): every site the three decisions name as the operator's.
SITES: tuple[tuple[str, str], ...] = (
    ("slotenmakergerard.nl", "keyservice"),
    ("slotenmaker-janwillem.nl", "keyservice"),
    ("slotenmakerjanwillem.nl", "keyservice"),
    ("slotenmakersmid.nl", "keyservice"),
    ("slotenmakerslim.nl", "keyservice"),
    ("slotenmaker-slim.nl", "keyservice"),
    ("slotenmaker-graag.nl", "keyservice"),
    ("slotenmakergraag.nl", "keyservice"),
    ("slotenmakerherman.nl", "keyservice"),
    ("slotenmakerhart24.nl", "keyservice"),
    ("slotenmakerijnijmegen.nl", "keyservice"),
    ("slotenmakerijarnhem.nl", "keyservice"),
    ("amersfoortslotenmakers.nl", "allfree"),
    ("amersfoort-slotenmakers.nl", "allfree"),
    ("rotterdam-slotenmakers.nl", "allfree"),
    ("deventer-slotenmaker.nl", "allfree"),
    ("wageningen-slotenmaker.nl", "allfree"),
    ("nieuwegein-slotenmakers.nl", "allfree"),
    ("oosterhoutslotenmaker.nl", "allfree"),
    ("slotenmaker-waalwijk.nl", "allfree"),
    ("zwolle-slotenmakers.nl", "allfree"),
    ("hengelo-slotenmaker.nl", "allfree"),
    ("denhelder-slotenmaker.nl", "allfree"),
    ("hoogeveen-slotenmaker.nl", "allfree"),
    ("geleen-slotenmaker.nl", "allfree"),
    ("kerkradeslotenmaker.nl", "allfree"),
    ("zeeland-slotenmaker.nl", "allfree"),
    ("denbosch-slotenmakers.nl", "allfree"),
    ("slotenmaker-zaandam.nl", "leadle"),
    ("loodgieters-leiden.nl", "leadle"),
    ("040-slotenmakereindhoven.nl", "leadle"),
    ("040-slotenmaker-eindhoven.nl", "leadle"),
    ("020loodgieter-amsterdam.nl", "leadle"),
    ("loodgieters.nl", "leadle"),
)

_SKIP_PATH = re.compile(
    r"privacy|voorwaarden|cookie|disclaimer|cdn-cgi|wp-json|wp-content|wp-includes|xmlrpc|/feed|/tag/|/author/"
    r"|sitemap|login|\.(?:ico|png|jpe?g|gif|svg|css|js|xml|txt|pdf)$",
    re.IGNORECASE,
)
_TRADE_RE = re.compile(r"slotenmaker|loodgieter|ontstop|sleutel|lekkage|cilinder", re.IGNORECASE)
_MIN_TEXT = 400


def pick_pages(rows: list[list[str]], per_site: int) -> list[tuple[str, str]]:
    """
    Choose which captures of one site to fetch: the home page first, then other pages, one capture per address.

    :param rows: CDX rows ``[timestamp, original]`` (header row already removed).
    :param per_site: Pages to keep at most.
    :return: ``(timestamp, original)`` pairs.
    """
    by_path: dict[str, tuple[str, str]] = {}
    for timestamp, original in rows:
        parts = urlsplit(original)
        if parts.query or _SKIP_PATH.search(parts.path):
            continue
        path = parts.path.rstrip("/") or "/"
        by_path.setdefault(path, (timestamp, original))
    home = [by_path.pop("/")] if "/" in by_path else []
    rest = [by_path[p] for p in sorted(by_path, key=lambda p: (p.count("/"), p))]
    return (home + rest)[:per_site]


def reads_as_trade_site(text: str) -> bool:
    """Whether a capture's text is a trade site's page (not a parked domain, an error or an empty shell)."""
    return len(text) >= _MIN_TEXT and bool(_TRADE_RE.search(text))


async def captures(client: PoliteClient, domain: str, decision: Decision) -> list[list[str]]:
    """
    List a site's 200-status HTML captures in the decision's period, one per address.

    :param client: A rate-limited client.
    :param domain: The site's domain.
    :param decision: The decision naming it.
    :return: CDX rows ``[timestamp, original]``.
    """
    response = await client.get(
        CDX_URL,
        params={
            "url": domain,
            "matchType": "domain",
            "from": decision.start,
            "to": decision.end,
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
        rows = response.json()
    except ValueError:
        return []
    return [list(r) for r in rows[1:]]


async def build(corpus: Corpus, per_site: int) -> int:
    """
    Fetch the named sites' captures and add those that read as trade pages.

    :param corpus: The corpus.
    :param per_site: Pages per site at most.
    :return: The number of pages stored.
    """
    client = PoliteClient(interval=1.0)
    existing = set(corpus.labels())
    added: list[tuple[LabelRecord, str]] = []
    skipped = 0
    try:
        for domain, key in SITES:
            decision = DECISIONS[key]
            rows = await captures(client, domain, decision)
            for timestamp, original in pick_pages(rows, per_site):
                rid = page_id("acm", original)
                if rid in existing:
                    continue
                html = await fetch_snapshot(client, timestamp, original)
                if html is None or not reads_as_trade_site(clean_html(html)):
                    skipped += 1
                    continue
                notes = {
                    "operator": decision.operator,
                    "decision": decision.url,
                    "domain": domain,
                    "synthetic": False,
                }
                added.append(
                    (
                        LabelRecord(
                            id=rid,
                            label="scam",
                            label_source="dataset",
                            prior_label="scam",
                            confidence=0.85,
                            page_type="service_site",
                            source="wayback",
                            origin_dataset=DATASET,
                            origin_url=original,
                            origin_id=timestamp,
                            collected_at=timestamp[:8],
                            notes=json.dumps(notes, sort_keys=True),
                        ),
                        html,
                    )
                )
            logger.info("acm_site", domain=domain, captures=len(rows), added=len(added))
    finally:
        await client.aclose()
    stored = corpus.add_many(added) if added else 0
    logger.info("acm_traders_done", stored=stored, skipped=skipped)
    return stored


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--per-site", type=int, default=8)
    args = parser.parse_args()
    asyncio.run(build(Corpus(), args.per_site))


if __name__ == "__main__":
    main()
