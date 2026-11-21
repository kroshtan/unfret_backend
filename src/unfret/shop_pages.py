"""
A web shop's own other pages (returns, shipping, about, contact, imprint, terms), read once per analysis.

A product page says "family business in Ohio" and "30-day free returns"; the returns page gives a return address in
Shenzhen and "14 days, at the buyer's cost". The claim audit (:mod:`unfret.graph.skills.claim_audit`) holds the
product page's claims against these pages, and the pages against each other; the EU merchant check
(:mod:`unfret.graph.skills.eu_registry`) reads the contact and terms pages for the company's details. Both read them
through :func:`read`, so a page two steps want is fetched once.

Only a shop's own pages: the analysed page must be a web shop (``storefront``) on its own site (not a marketplace's
or a platform's pages, which aren't the seller's words), each link on the shop's registrable domain, never a PDF or
the product page itself. At most :data:`MAX_PAGES` pages, fetched in parallel through :func:`unfret.net.fetch_public`
with :data:`FETCH_TIMEOUT_S` for the whole fetch, :data:`MAX_BYTES` each; a bot check, an error or a timeout is
simply a page not read. What is kept is the page's visible text (:func:`unfret.facts.parse_page`), and of a long
one the lines that carry what claims are checked against (:func:`excerpt`): at most :data:`PAGE_CHARS` a page and
:data:`TOTAL_CHARS` in all.

A network lookup: off in evaluation runs and in the quick tier, unless an evaluation supplies the pages itself
(:func:`use_source`). The pages are kept in memory only, a couple of minutes, keyed by a hash of their URL: never
logged or stored.
"""

import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urldefrag

from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.net import fetch_public, hostname
from unfret.price_store import registrable_domain

# The kinds read, in this order (the first MAX_PAGES distinct pages): what the claims are most often checked against.
KINDS = ("refund", "shipping", "about", "contact", "imprint", "terms")
LABELS = {
    "refund": "returns page",
    "shipping": "shipping page",
    "about": "about page",
    "contact": "contact page",
    "imprint": "imprint page",
    "terms": "terms page",
}
# Pages fetched per analysis, at most.
MAX_PAGES = 4
# Visible text kept from one page, and from all of them, in characters.
PAGE_CHARS = 4_000
TOTAL_CHARS = 12_000
# One page's whole fetch, redirects included (a shop's own page answers in a second or two), and its size.
FETCH_TIMEOUT_S = 6.0
MAX_BYTES = 3 * 1024 * 1024
# Platforms whose pages are a shop's own site; on the others (marketplaces) the "policy" pages are the platform's.
OWN_SITE_PLATFORMS = frozenset({"generic", "shopify", "woocommerce"})

# The lines of a long page that carry what a claim is checked against: where it ships from and how long it takes,
# returns (days, costs, where to), who the company is and where, and what it says about itself.
_RELEVANT_RE = re.compile(
    r"\d|return|refund|exchange|restock|ship|deliver|dispatch|warehouse|address|located|based in|registered|company"
    r"|ltd|llc|inc\b|gmbh|b\.?v\.?\b|limited|family|founded|made in|handmade|our story|origin|china|hong kong"
    r"|retour|verzend|lever|bezorg|versand|liefer|rücksend|widerruf|livraison|retourn|envío|devoluci|spedizion|reso"
    r"|kvk|handelsregister|impressum|vat|btw|ust",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReadPage:
    """One of the shop's pages as read: its visible text and what it says about the company."""

    text: str
    # The page's legal mentions (register entries, withdrawal right) and whether it shows a VAT number.
    legal: str = ""
    vat: bool = False


@dataclass(frozen=True)
class ShopPage:
    """One of the shop's own pages, as the claim audit reads it."""

    kind: str
    text: str

    @property
    def label(self) -> str:
        """
        The page as the shopper is told of it.

        :return: "returns page", "about page", …
        """
        return LABELS.get(self.kind, f"{self.kind} page")


Source = Callable[[str], Awaitable[ReadPage | None]]
_source: Source | None = None


def use_source(source: Source | None) -> None:
    """
    Read the shop's pages from elsewhere (an evaluation's recorded pages) instead of fetching them, or fetch again.

    :param source: Gives a page's :class:`ReadPage` by URL (``None`` when it has none); ``None`` restores fetching.
    """
    global _source  # noqa: PLW0603 - one switch for the process, like the fact cache's store
    _source = source
    _READS.clear()


def enabled() -> bool:
    """
    Whether the shop's pages may be read: network lookups on (or an evaluation supplying them).

    :return: ``True`` when :func:`read` may answer.
    """
    return _source is not None or settings.network_lookups


def read_html(html: str, url: str) -> ReadPage:
    """
    A fetched page, read.

    :param html: The page.
    :param url: Its URL.
    :return: Its visible text, legal mentions and whether it shows a VAT number.
    """
    facts, text = parse_page(html, url)
    return ReadPage(text=text, legal=" ".join(facts.legal_mentions), vat=bool(facts.vat_ids))


async def _fetch(url: str) -> ReadPage | None:
    if _source is not None:
        return await _source(url)
    try:
        body = await fetch_public(url, timeout=FETCH_TIMEOUT_S, total_timeout=FETCH_TIMEOUT_S, max_bytes=MAX_BYTES)
    except Exception:  # noqa: BLE001 - a page we can't read says nothing about the shop
        return None
    from unfret.page_fetch import is_challenge_page  # noqa: PLC0415 - page_fetch imports the graph, which imports this

    html = body.decode("utf-8", "replace")
    if is_challenge_page(html):
        return None
    return await asyncio.to_thread(read_html, html, url)


# One fetch per page however many steps ask, for a couple of minutes: in memory only, keyed by the URL's hash.
_READS: OrderedDict[tuple[int, str], tuple[float, asyncio.Future[ReadPage | None]]] = OrderedDict()
_READS_KEPT = 32
_READ_TTL_S = 120.0


async def read(url: str) -> ReadPage | None:
    """
    One of the shop's pages, fetched once for every step that asks (concurrently or within a couple of minutes).

    :param url: The page's URL.
    :return: The page, or ``None`` when it couldn't be read (an error, a timeout, a bot check) or reads are off.
    """
    if not enabled():
        return None
    loop = asyncio.get_running_loop()
    key = (id(loop), hashlib.sha256(url.encode()).hexdigest())
    now = time.monotonic()
    entry = _READS.get(key)
    if entry is None or entry[0] < now or entry[1].get_loop() is not loop:  # a loop's id can be reused
        entry = (now + _READ_TTL_S, loop.create_task(_fetch(url)))
        _READS[key] = entry
        while len(_READS) > _READS_KEPT:
            _READS.popitem(last=False)
    _READS.move_to_end(key)
    return await asyncio.shield(entry[1])


def page_urls(facts: PageFacts, page_url: str, kinds: tuple[str, ...] = KINDS) -> list[tuple[str, str]]:
    """
    The shop's own pages worth reading: on its own site, not a PDF and not the analysed page itself.

    :param facts: The analysed page's facts (its ``policy_links``).
    :param page_url: The analysed page's URL.
    :param kinds: The kinds wanted, in order.
    :return: ``(kind, url)`` pairs, one per distinct page, in the order of ``kinds``.
    """
    host = hostname(page_url)
    if not host:
        return []
    shop = registrable_domain(host)
    here = urldefrag(page_url)[0]
    seen: set[str] = set()
    pages = []
    for kind in kinds:
        url = facts.policy_links.get(kind, "")
        target = hostname(url)
        page = urldefrag(url)[0]
        if not target or registrable_domain(target) != shop or page in seen or page == here:
            continue
        if url.lower().split("?")[0].endswith(".pdf") or not url.lower().startswith(("http://", "https://")):
            continue
        seen.add(page)
        pages.append((kind, url))
    return pages


def own_site(facts: PageFacts, page_type: str) -> bool:
    """
    Whether the analysed page is a web shop on its own site, whose other pages are the seller's own words.

    :param facts: The page facts.
    :param page_type: The classified page type.
    :return: ``True`` for a storefront that isn't on a marketplace.
    """
    return page_type == "storefront" and facts.platform in OWN_SITE_PLATFORMS


def excerpt(text: str, limit: int = PAGE_CHARS) -> str:
    """
    A page's text within ``limit``: all of it when it fits, else its relevant lines (:data:`_RELEVANT_RE`) in order.

    :param text: The page's visible text.
    :param limit: Characters at most.
    :return: The text, or the lines kept, joined by newlines.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    keep: list[str] = []
    used = 0
    for line in lines:
        if not _RELEVANT_RE.search(line):
            continue
        cut = line[:600]
        if used + len(cut) + 1 > limit:
            break
        keep.append(cut)
        used += len(cut) + 1
    return "\n".join(keep) if keep else text[:limit]


async def shop_pages(facts: PageFacts, page_url: str, page_type: str) -> list[ShopPage]:
    """
    The shop's own other pages (:data:`KINDS`, at most :data:`MAX_PAGES`), their text bounded.

    :param facts: The analysed page's facts.
    :param page_url: Its URL.
    :param page_type: Its classified page type.
    :return: The pages that could be read, in the order of :data:`KINDS`; ``[]`` for a marketplace or other page
        that isn't a web shop's own, or when reads are off.
    """
    if not (settings.claim_audit_shop_pages and enabled() and own_site(facts, page_type)):
        return []
    wanted = page_urls(facts, page_url)[:MAX_PAGES]
    read_pages = await asyncio.gather(*(read(url) for _, url in wanted))
    pages: list[ShopPage] = []
    budget = TOTAL_CHARS
    for (kind, _), page in zip(wanted, read_pages, strict=True):
        if page is None or budget < 200:
            continue
        text = excerpt(page.text, min(PAGE_CHARS, budget))
        if len(text) < 40:
            continue  # an empty shell its scripts fill in
        budget -= len(text)
        pages.append(ShopPage(kind, text))
    return pages
