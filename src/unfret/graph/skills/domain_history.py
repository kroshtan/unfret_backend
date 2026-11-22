"""
What the shop's address showed before: earlier copies of its home page in Common Crawl or the Internet Archive.

An old address makes a shop look established (the "Established shop" positive, the scorer's domain age). An address
that expired and was bought again, or was taken over, carries that age to a new shop that hasn't earned it. So when
the address is old and the page already looks doubtful (the triage decides, :func:`unfret.graph.triage.plan`), the
deep check looks at the home page's copies from about 1, 3 and 12 months ago and compares them with the page now:

- a **parked page** (the address for sale, a registrar's parking page with ads) or an **empty placeholder** ("coming
  soon", a web server's default page) is a different site by itself;
- otherwise a copy is a **different site** only on two or more of: a different language, not a shop at all, and
  almost no words in common with the page. The shop's own name (its site or company name) in the copy makes it the
  same shop; with only the address's own name in it, all three must hold. A copy too thin to read (a page built by
  scripts) says nothing.

Cheap rules only, no LLM: the copy's title, language (a stop-word count), shop markers (a basket, prices, a shop
platform) and its most frequent words. No copies at all says nothing either: plenty of honest shops are never
archived.

Where the copies come from, in order:

1. **Common Crawl** (:mod:`unfret.graph.skills.common_crawl`): the crawls nearest each of those days, their index for
   the home page, and the page's WARC record by byte range, within Common Crawl's own pace.
2. **The Internet Archive**, only when Common Crawl has no usable copy (none, or only pages too thin to read, or it
   can't be reached): its CDX index and raw copies (``id_``, without the archive's toolbar), within a pace of a few
   requests a minute and a daily cap (``UNFRET_WAYBACK_PER_MINUTE``, ``UNFRET_WAYBACK_DAILY_CAP``), and none for a
   while after it refused us. It blocks clients that ask too often.

The findings name the archive the copies came from, with links to them. Copies are fetched like any outside page
(:func:`unfret.net.fetch_public`: public addresses only, a size limit, timeouts, no cookies), and what they show is
cached per address and per archive as a store-level fact (:mod:`unfret.fact_cache`), never their HTML.
"""

import asyncio
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any, NoReturn
from urllib.parse import quote

import httpx
import structlog
from bs4 import BeautifulSoup

from unfret import shopper
from unfret.config import settings
from unfret.fact_cache import cached_fact
from unfret.facts.schema import PageFacts
from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput, common_crawl, domain_age
from unfret.graph.skills.common_crawl import ArchiveBusyError, ArchiveUnreachableError
from unfret.graph.state import AnalysisState
from unfret.html_cleaner import strip_to_text
from unfret.net import fetch_public, hostname
from unfret.pace import Pace

# About 1, 3 and 12 months before today; a copy counts for a target within this many days of it.
TARGETS_DAYS: tuple[tuple[int, int], ...] = ((30, 45), (91, 60), (365, 90))
_CDX_TIMEOUT_S = 8.0
_SNAPSHOT_TIMEOUT_S = 10.0
_MAX_CDX_BYTES = 256 * 1024
_MAX_FIRST_CAPTURE_BYTES = 16 * 1024
_MAX_SNAPSHOT_BYTES = 3 * 1024 * 1024
_TEXT_CHARS = 60_000  # of a copy's text read for its words
_TOP_WORDS = 40
_MAX_TOKENS = 3_000
_TITLE_CHARS = 80
# Fewer words than this and a copy is too thin to say what it was (a page its scripts build).
_THIN_WORDS = 40
# A placeholder is a line or two ("Coming soon. Stay tuned!").
_PLACEHOLDER_WORDS = 25
# A parked page is short: the domain, a line that it is for sale, a few ads.
_SHORT_PAGE_WORDS = 300
# Share of the smaller top-word list the two pages have in common below which they have "almost nothing in common".
LOW_OVERLAP = 0.1

Kind = str  # "parked", "placeholder", "shop", "site" or "thin"

_PARKED_RE = re.compile(
    r"(?:this|the) domain(?: name)? (?:is|may be|might be) for sale|buy this domain|domain (?:is )?for sale"
    r"|make an offer on this domain|this domain has expired|domain has been registered|domein is te koop"
    r"|deze domeinnaam is te koop|domain steht zum verkauf|domain kaufen|ce (?:nom de )?domaine est à vendre"
    r"|dominio (?:está )?en venta|questo dominio è in vendita|afternic|hugedomains|dan\.com|undeveloped\.com"
    r"|domain parking|parked (?:free|domain)|this web page is parked",
    re.IGNORECASE,
)
# Parking services, in a page's code (their scripts and frames).
_PARKING_CODE_RE = re.compile(
    r"sedoparking|parkingcrew|bodis\.com|parklogic|above\.com/(?:marketplace|parking)", re.IGNORECASE
)
_PLACEHOLDER_RE = re.compile(
    r"coming soon|under construction|website (?:is )?(?:launching|opening) soon|future home of"
    r"|welcome to nginx|apache2? (?:\w+ )?default page|^\s*it works!|index of /|binnenkort online"
    r"|in aanbouw|demnächst online|en construction|account (?:has been )?suspended",
    re.IGNORECASE | re.MULTILINE,
)
_CART_RE = re.compile(
    r"add to (?:cart|bag|basket)|shopping (?:cart|bag|basket)|in (?:den|die) warenkorb|warenkorb|winkelwagen"
    r"|in winkelmand|ajouter au panier|panier|añadir al carrito|carrito|aggiungi al carrello|carrello|koszyk"
    r"|do koszyka|košík|varukorg|indkøbskurv|checkout|afrekenen|kasse",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"(?:[€$£]\s?\d{1,5}(?:[.,]\d{2})?|\d{1,5}[.,]\d{2}\s?(?:€|eur|usd|\$|kč|zł|kr))", re.IGNORECASE
)
_PLATFORM_RE = re.compile(
    r"cdn\.shopify\.com|woocommerce|mage/cookies|magento|prestashop|shopware|lightspeed|bigcommerce|ecwid"
    r"|wix-stores|squarespace-commerce|myshopify",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _set(words: str) -> frozenset[str]:
    return frozenset(words.split())


# The commonest words of the languages Unfret sees most, to tell which one a page is in.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": _set("the and for with you your our this that are from all have will not can more about"),
    "nl": _set("de het een van en voor met je jouw onze niet bij ook zijn wordt naar meer dit"),
    "de": _set("der die das und mit für sie ihr unsere nicht auf ist ein eine den dem zu auch"),
    "fr": _set("le la les et des pour avec vous votre nos est une du sur pas en au plus"),
    "es": _set("el la los las y para con su sus nuestro es una del por más en que"),
    "it": _set("il lo la gli le e per con tuo tuoi nostro è una del della che più"),
    "pt": _set("o os as e para com seu sua nosso é uma do da que mais em não"),
    "pl": _set("i w na z do jest się nie że dla oraz jak po od to"),
    "cs": _set("a v na se je do pro že jako od po s z k to"),
    "sv": _set("och att det är för med som på en av till inte om din"),
    "da": _set("og at det er for med som på en af til ikke om din"),
}
_ALL_STOPWORDS = frozenset().union(*_STOPWORDS.values())
_MIN_LANGUAGE_HITS = 8
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_LANGUAGES = {
    "en": "English",
    "nl": "Dutch",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "pl": "Polish",
    "cs": "Czech",
    "sv": "Swedish",
    "da": "Danish",
}


COMMON_CRAWL, WAYBACK = "commoncrawl", "wayback"
# How the findings name each archive ("<name> copy of …", "<owner> copy from …").
ARCHIVE_NAMES = {
    COMMON_CRAWL: ("Common Crawl", "Common Crawl's"),
    WAYBACK: ("Internet Archive", "the Internet Archive's"),
}
__all__ = ["ArchiveBusyError", "ArchiveUnreachableError"]
logger = structlog.get_logger("unfret")


def language(text: str) -> str:
    """
    The language a text is in, by counting the commonest words of each language.

    :param text: Plain text.
    :return: An ISO 639-1 code, or ``""`` when no language clearly leads.
    """
    words = Counter(w.lower() for w in _WORD_RE.findall(text[:_TEXT_CHARS]))
    hits = sorted(((sum(words[w] for w in stop), code) for code, stop in _STOPWORDS.items()), reverse=True)
    (best, code), (second, _) = hits[0], hits[1]
    return code if best >= _MIN_LANGUAGE_HITS and best >= 1.5 * second else ""


def top_words(text: str, n: int = _TOP_WORDS) -> list[str]:
    """
    A page's most frequent content words (four letters or more, no stop words), what it is about.

    :param text: Plain text.
    :param n: How many.
    :return: The words, lower case, most frequent first.
    """
    words = Counter(
        w for w in (m.lower() for m in _WORD_RE.findall(text[:_TEXT_CHARS])) if len(w) >= 4 and w not in _ALL_STOPWORDS
    )
    return [w for w, _ in words.most_common(n)]


def overlap(now: list[str], then: list[str]) -> float | None:
    """
    How much two pages' top words have in common: the share of the shorter list found in the other.

    :param now: One page's top words.
    :param then: The other's.
    :return: 0–1, or ``None`` when either has too few words to say.
    """
    if min(len(now), len(then)) < 10:
        return None
    return len(set(now) & set(then)) / min(len(now), len(then))


def kind_of(html: str, text: str, title: str = "") -> Kind:
    """
    What a page is: a parked page, an empty placeholder, a shop, another kind of site, or too thin to tell.

    A parked page or a placeholder says so in its title, or is little more than the words that say so: a shop's own
    page can mention "coming soon" (a product) or a domain seller (a link) without being either.

    :param html: The page's HTML (shop platforms and parking services show in its code).
    :param text: Its visible text.
    :param title: Its title.
    :return: ``parked``, ``placeholder``, ``shop``, ``site`` or ``thin``.
    """
    head = text[:5_000]
    words = len(_WORD_RE.findall(text[:_TEXT_CHARS]))
    short = words < _SHORT_PAGE_WORDS
    if _PARKED_RE.search(title) or (short and (_PARKED_RE.search(head) or _PARKING_CODE_RE.search(html[:20_000]))):
        return "parked"
    if _PLACEHOLDER_RE.search(title) or (words < _PLACEHOLDER_WORDS and _PLACEHOLDER_RE.search(head)):
        return "placeholder"
    carts = len(_CART_RE.findall(text[:_TEXT_CHARS]))
    prices = len(_PRICE_RE.findall(text[:_TEXT_CHARS]))
    if _PLATFORM_RE.search(html) or carts >= 2 or (carts and prices >= 2) or prices >= 5:
        return "shop"
    return "thin" if words < _THIN_WORDS else "site"


def summarise(html: str) -> dict[str, Any]:
    """
    What an archived copy shows, kept instead of its HTML: kind, language, title, top words and its word set.

    :param html: The copy's HTML.
    :return: ``kind``, ``lang``, ``title``, ``words`` (top words) and ``tokens`` (distinct words, for names).
    """
    soup = BeautifulSoup(html, "html.parser")
    title = " ".join((soup.title.get_text() if soup.title else "").split())[:_TITLE_CHARS]
    declared = str(soup.html.get("lang", "") if soup.html else "")[:2].lower()
    text = strip_to_text(soup)
    tokens = sorted({w.lower() for w in _WORD_RE.findall(f"{title} {text[:_TEXT_CHARS]}")})[:_MAX_TOKENS]
    return {
        "kind": kind_of(html, text, title),
        "lang": language(text) or (declared if declared in _STOPWORDS else ""),
        "title": title,
        "words": top_words(text),
        "tokens": tokens,
    }


def pick(rows: list[list[str]], today: date) -> list[list[str]]:
    """
    The copies closest to about 1, 3 and 12 months ago, each within its tolerance, each copy once.

    :param rows: CDX rows ``[timestamp, original]``.
    :param today: Today.
    :return: Up to three rows, newest first.
    """
    dated = [(r, _day(r[0])) for r in rows]
    chosen: list[list[str]] = []
    for days, tolerance in TARGETS_DAYS:
        target = today - timedelta(days=days)
        near = [(abs((d - target).days), r) for r, d in dated if d is not None and r not in chosen]
        best = min(near, key=lambda t: t[0], default=None)
        if best is not None and best[0] <= tolerance:
            chosen.append(best[1])
    return sorted(chosen, key=lambda r: r[0], reverse=True)


def _day(timestamp: str) -> date | None:
    try:
        return datetime.strptime(timestamp[:8], "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None


def archive_url(timestamp: str, original: str) -> str:
    """
    Where a person sees an Internet Archive copy (with the archive's toolbar, so it is clearly an archived page).

    :param timestamp: The copy's 14-digit timestamp.
    :param original: The archived address.
    :return: The web.archive.org link.
    """
    return f"https://web.archive.org/web/{timestamp}/{original}"


def copy_link(snapshot: dict[str, Any]) -> str:
    """
    The link to a copy in the archive it came from.

    :param snapshot: The copy's summary (``archive``, ``timestamp``, ``original``, and a Common Crawl copy's
        ``crawl``).
    :return: A web.archive.org copy, or the copy's record in Common Crawl's index.
    """
    if snapshot.get("archive") == COMMON_CRAWL:
        return common_crawl.copy_url(str(snapshot["crawl"]), snapshot["timestamp"], snapshot["original"])
    return archive_url(snapshot["timestamp"], snapshot["original"])


def month_of(timestamp: str) -> str:
    """
    A copy's month in words.

    :param timestamp: The copy's 14-digit timestamp.
    :return: "March 2026".
    """
    return f"{_MONTHS[int(timestamp[4:6]) - 1]} {timestamp[:4]}"


# The Internet Archive blocks a client that asks too often (it refuses connections for an hour, longer on repeat),
# which would cost every later deep check its look: a few requests a minute, a daily cap, a pause after a refusal.
def new_pace() -> Pace:
    """
    A fresh pace for the Internet Archive, by the settings.

    :return: The pace.
    """
    return Pace(
        per_minute=lambda: settings.wayback_per_minute,
        pause_s=lambda: settings.wayback_pause_s,
        daily_cap=lambda: settings.wayback_daily_cap,
    )


pace = new_pace()


def _refused(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {403, 429}
    return isinstance(exc, httpx.ConnectError | ConnectionRefusedError)


async def _get(url: str, *, timeout: float, max_bytes: int) -> bytes:
    """
    One request to the Internet Archive, within :data:`pace`, like any outside page (:func:`unfret.net.fetch_public`).

    :param url: The archive address.
    :param timeout: The whole request's time limit.
    :param max_bytes: The largest answer accepted.
    :return: The answer's body.
    :raises ArchiveBusyError: When the pace or the day's cap is used up, or the archive refused us lately.
    :raises Exception: What :func:`unfret.net.fetch_public` raises (a refusal pauses the pace first).
    """
    if not pace.take():
        raise ArchiveBusyError("Not asking the Internet Archive right now (Unfret's own pace).")
    try:
        return await fetch_public(url, timeout=timeout, total_timeout=timeout, max_bytes=max_bytes)
    except Exception as exc:
        if _refused(exc):
            pace.pause()
        raise


async def captures(host: str, today: date) -> list[list[str]]:
    """
    The home page's copies of the last 13 months in the Internet Archive, one a month (its CDX index).

    :param host: The shop's host.
    :param today: Today.
    :return: ``[timestamp, original]`` rows, oldest first; empty when the archive has none. An archive that can't be
        reached raises (:func:`unfret.net.fetch_public`), so it isn't cached and the next check asks again.
    """
    since = (today - timedelta(days=400)).strftime("%Y%m%d")
    url = (
        f"{settings.wayback_url}/cdx/search/cdx?url={quote(host)}/&from={since}&output=json"
        "&fl=timestamp,original&filter=statuscode:200&filter=mimetype:text/html&collapse=timestamp:6&limit=40"
    )
    body = await _get(url, timeout=_CDX_TIMEOUT_S, max_bytes=_MAX_CDX_BYTES)
    data = json.loads(body or b"[]")
    rows = data[1:] if isinstance(data, list) and data and data[0] == ["timestamp", "original"] else []
    return [[str(r[0]), str(r[1])] for r in rows if isinstance(r, list) and len(r) == 2]


async def first_capture(host: str) -> str | None:
    """
    The day the Internet Archive first copied the address's home page: the address is at least that old.

    One index request (the earliest capture that the archive answered with a page or a redirect), within
    :data:`pace` like the history look. A lower bound only: an address can be online for years before the archive
    first copies it, and an expired address bought again keeps its old copies (what :func:`history` checks).

    :param host: The shop's host.
    :return: ISO date of the first capture, or ``None`` when the archive has none. Paced out
        (:class:`ArchiveBusyError`) or an index that can't be reached raises, so it isn't cached.
    """
    url = (
        f"{settings.wayback_url}/cdx/search/cdx?url={quote(host)}/&output=json&fl=timestamp"
        f"&filter={quote('statuscode:[23]..')}&limit=1"
    )
    body = await _get(url, timeout=_CDX_TIMEOUT_S, max_bytes=_MAX_FIRST_CAPTURE_BYTES)
    data = json.loads(body or b"[]")
    rows = data[1:] if isinstance(data, list) and data and data[0] == ["timestamp"] else []
    first = rows[0] if rows and isinstance(rows[0], list) and rows[0] else None
    day = _day(str(first[0])) if first else None
    return day.isoformat() if day else None


async def _snapshot(timestamp: str, original: str) -> dict[str, Any]:
    raw = f"{settings.wayback_url}/web/{timestamp}id_/{original}"
    body = await _get(raw, timeout=_SNAPSHOT_TIMEOUT_S, max_bytes=_MAX_SNAPSHOT_BYTES)
    summary = await asyncio.to_thread(summarise, body.decode("utf-8", errors="replace"))
    return {"archive": WAYBACK, "timestamp": timestamp, "original": original} | summary


def _unreachable(picked: int, fetched: list[Any]) -> None:
    """
    Raise when copies were picked and none could be fetched.

    :param picked: How many were picked.
    :param fetched: What fetching each gave (a summary or an error).
    :raises ArchiveUnreachableError: When none could be fetched.
    """
    if picked and not any(isinstance(s, dict) for s in fetched):
        errors = sorted({type(e).__name__ for e in fetched if isinstance(e, BaseException)})
        raise ArchiveUnreachableError(f"No archived copy could be fetched ({', '.join(errors)}).")


async def wayback_history(host: str, today: date) -> dict[str, Any]:
    """
    The Internet Archive's copies of the home page from about 1, 3 and 12 months ago.

    :param host: The shop's host.
    :param today: Today.
    :return: ``archive``, ``captures`` (how many monthly copies the archive has) and ``snapshots`` (each picked
        copy's summary, newest first; a copy that couldn't be fetched is left out). When none of the picked copies
        could be fetched it raises (:class:`ArchiveUnreachableError`), and so does an index that can't be reached.
    """
    rows = await captures(host, today)
    picked = pick(rows, today)
    fetched = await asyncio.gather(*(_snapshot(t, o) for t, o in picked), return_exceptions=True)
    _unreachable(len(picked), fetched)
    return {"archive": WAYBACK, "captures": len(rows), "snapshots": [s for s in fetched if isinstance(s, dict)]}


async def _crawl_snapshot(capture: dict[str, Any]) -> dict[str, Any]:
    page = await common_crawl.fetch_page(capture)
    summary = await asyncio.to_thread(summarise, page.decode("utf-8", errors="replace"))
    meta = {"archive": COMMON_CRAWL, "crawl": capture["crawl"], "timestamp": str(capture["timestamp"])}
    return meta | {"original": str(capture.get("url", ""))} | summary


async def common_crawl_history(host: str, today: date) -> dict[str, Any]:
    """
    Common Crawl's copies of the home page from about 1, 3 and 12 months ago: one from each nearest crawl.

    :param host: The shop's host.
    :param today: Today.
    :return: Like :func:`wayback_history`; ``captures`` is how many of the picked crawls had the home page. A crawl
        list or indexes that can't be asked, or copies none of which could be fetched, raise
        (:func:`unfret.graph.skills.common_crawl.give_up`), so nothing is cached.
    """
    crawls = common_crawl.pick(await common_crawl.crawls(), today, TARGETS_DAYS)
    answers = await asyncio.gather(*(common_crawl.captures(c, host) for c in crawls), return_exceptions=True)
    errors = [a for a in answers if isinstance(a, BaseException)]
    if crawls and len(errors) == len(crawls):
        common_crawl.give_up(errors)
    picked = [c for a in answers if isinstance(a, list) and (c := common_crawl.html_capture(a))]
    fetched = await asyncio.gather(*(_crawl_snapshot(c) for c in picked), return_exceptions=True)
    _unreachable(len(picked), fetched)
    snapshots = sorted((s for s in fetched if isinstance(s, dict)), key=lambda s: s["timestamp"], reverse=True)
    if not snapshots:
        common_crawl.give_up(errors)  # an index that failed may have had the copy: ask again next time
    return {"archive": COMMON_CRAWL, "captures": len(picked), "snapshots": snapshots}


def usable(found: dict[str, Any] | None) -> bool:
    """
    Whether an archive's answer has a copy to compare with (not only pages too thin to read).

    :param found: :func:`common_crawl_history` or :func:`wayback_history`.
    :return: ``True`` when it does.
    """
    return any(s.get("kind") != "thin" for s in (found or {}).get("snapshots", []))


def cause(exc: BaseException) -> str:
    """
    An archive's failure in a few words, with its real cause: the HTTP status or the timeout behind it.

    Never the address asked (an archive's index URL names the shop).

    :param exc: The error, as raised (its ``__cause__`` and ``__context__`` are looked through).
    :return: "HTTP 504 (The archive couldn't be asked (HTTPStatusError))", "timed out (ReadTimeout)", ….
    """
    said = str(exc).rstrip(".") or type(exc).__name__
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if isinstance(link, httpx.HTTPStatusError):
            return f"HTTP {link.response.status_code}" + ("" if link is exc else f" ({said})")
        if isinstance(link, httpx.TimeoutException | TimeoutError):
            return f"timed out ({type(link).__name__})" + ("" if link is exc else f" ({said})")
        link = link.__cause__ or link.__context__
    return said


def both_failed(crawl_error: BaseException, wayback_error: BaseException) -> NoReturn:
    """
    Raise for neither archive answering: Common Crawl's real cause first, then the Internet Archive's.

    Of the kind the Internet Archive's error was, so the graph retries (or not) as it would on that error.

    :param crawl_error: Common Crawl's failure.
    :param wayback_error: The Internet Archive's (the raised error's cause).
    :raises ArchiveBusyError: When the Internet Archive was paced out.
    :raises ArchiveUnreachableError: When it couldn't be reached (a connection problem: the graph retries).
    :raises RuntimeError: When it failed otherwise.
    """
    said = f"Common Crawl failed: {cause(crawl_error)}. The Internet Archive failed too: {cause(wayback_error)}."
    if isinstance(wayback_error, ArchiveBusyError):
        raise ArchiveBusyError(said) from wayback_error
    if isinstance(wayback_error, httpx.TransportError | ConnectionError):
        raise ArchiveUnreachableError(said) from wayback_error
    raise RuntimeError(said) from wayback_error


async def history(host: str, today: date | None = None) -> dict[str, Any]:
    """
    What the shop's home page showed about 1, 3 and 12 months ago: Common Crawl first, the Internet Archive after.

    Each archive's answer is cached per address on its own; the Internet Archive is asked only when Common Crawl has
    no usable copy (:func:`usable`). Common Crawl failing is logged (without the address) when the archive steps in.

    :param host: The shop's host.
    :param today: Today (tests pass their own).
    :return: ``archive`` (whose copies: ``commoncrawl`` or ``wayback``), ``captures``, ``snapshots`` (newest first)
        and ``asked`` (the archives that answered).
    :raises Exception: When neither archive answered (paced out, refused or unreachable): not cached. It names both,
        Common Crawl's cause first (:func:`both_failed`).
    """
    today = today or datetime.now(tz=UTC).date()
    crawl: dict[str, Any] | None = None
    crawl_error: Exception | None = None
    try:
        crawl = await cached_fact(COMMON_CRAWL, host, lambda: common_crawl_history(host, today))
    except Exception as exc:  # noqa: BLE001 - unreachable or paced out: the Internet Archive instead
        crawl_error = exc
    if crawl is not None and usable(crawl):
        return crawl | {"asked": [COMMON_CRAWL]}
    try:
        wayback = await cached_fact(WAYBACK, host, lambda: wayback_history(host, today)) or {}
    except Exception as exc:
        if crawl is None:
            if crawl_error is None:
                raise
            both_failed(crawl_error, exc)
        return crawl | {"asked": [COMMON_CRAWL]}  # Common Crawl's answer stands; the archive is asked next time
    if crawl_error is not None:
        logger.warning("domain_history.common_crawl_failed", cause=cause(crawl_error), fallback=WAYBACK)
    wayback = {"archive": WAYBACK} | wayback  # answers cached before the archive was named
    asked = [COMMON_CRAWL, WAYBACK] if crawl is not None else [WAYBACK]
    best = wayback if crawl is None or wayback.get("snapshots") else crawl
    return best | {"asked": asked}


def _squash(name: str) -> str:
    return "".join(_WORD_RE.findall(name.lower()))


def shop_names(facts: PageFacts) -> list[str]:
    """
    The names the shop goes by now, by its own page: its site name and its company name.

    :param facts: The page facts.
    :return: Lower-case names without spaces or punctuation, three letters or more.
    """
    names = (_squash(n) for n in (facts.meta.get("og:site_name", ""), facts.business_name))
    return list(dict.fromkeys(n for n in names if len(n) >= 3))


def address_name(host: str) -> str:
    """
    The address's own name (``oudebakker`` for ``www.oudebakker.nl``).

    Weaker than the shop's own name: a shop that took an address over often goes by it too.

    :param host: The shop's host.
    :return: The name, or ``""``.
    """
    label = _squash(domain_age.candidates(host)[0].split(".")[0]) if host else ""
    return label if len(label) >= 3 else ""


def named(snapshot: dict[str, Any], names: list[str]) -> bool:
    """
    Whether an archived copy mentions any of the shop's names (a name of several words: all of them).

    :param snapshot: The copy's summary.
    :param names: :func:`shop_names`.
    :return: ``True`` when it does.
    """
    tokens = set(snapshot.get("tokens", []))
    squashed_title = "".join(_WORD_RE.findall(str(snapshot.get("title", "")).lower()))
    joined = "".join(snapshot.get("tokens", []))
    return any(n in tokens or n in squashed_title or (len(n) >= 6 and n in joined) for n in names)


def compare(
    snapshot: dict[str, Any], *, lang_now: str, words_now: list[str], names: list[str], label: str = ""
) -> str:
    """
    How an archived copy compares with the shop now.

    The shop's own name (its site or company name) in the copy makes it the same shop. The address's name alone
    doesn't: then all three differences must hold (another language, not a shop, almost no words in common); with
    no name at all, two.

    :param snapshot: The copy's summary.
    :param lang_now: The page's language now (:func:`language`).
    :param words_now: The page's top words now (:func:`top_words`).
    :param names: The shop's names (:func:`shop_names`).
    :param label: The address's own name (:func:`address_name`).
    :return: ``parked``, ``placeholder``, ``different``, ``same`` or ``unclear``.
    """
    kind = snapshot.get("kind")
    if kind in {"parked", "placeholder"}:
        return str(kind)
    if kind == "thin":
        return "unclear"
    lang_then = snapshot.get("lang", "")
    common = overlap(words_now, snapshot.get("words", []))
    differences = sum(
        (
            bool(lang_now and lang_then and lang_now != lang_then),
            kind == "site",
            common is not None and common < LOW_OVERLAP,
        )
    )
    if named(snapshot, names):
        return "same"
    if label and named(snapshot, [label]):
        return "different" if differences == 3 else "same"
    return "different" if differences >= 2 else "unclear"


def describe(snapshot: dict[str, Any], outcome: str) -> str:
    """
    A changed copy in words, to follow "it showed".

    :param snapshot: The copy's summary.
    :param outcome: :func:`compare`'s answer.
    :return: "a parked page, with the address for sale", "a different site (in German: “Bäckerei Jansen”)", ….
    """
    if outcome == "parked":
        return "a parked page, with the address for sale"
    if outcome == "placeholder":
        return "an empty placeholder page"
    details = []
    if snapshot.get("kind") == "site":
        details.append("not a shop")
    if lang := _LANGUAGES.get(snapshot.get("lang", "")):
        details.append(f"in {lang}")
    title = str(snapshot.get("title", "")).replace("“", "").replace("”", "").replace('"', "")
    if title:
        details.append(f"“{title}”")
    return f"a different site ({', '.join(details)})" if details else "a different site"


def _with_links(said: str, changed: list[tuple[dict[str, Any], str]]) -> str:
    """
    A finding's words, then the links to the changed copies, as many as fit in a finding (the first always does).

    :param said: The words.
    :param changed: The changed copies, newest first.
    :return: The evidence, at most 500 characters.
    """
    links = [copy_link(s) for s, _ in changed]
    text = said[: 499 - len(links[0])] + " " + links[0]
    for link in links[1:]:
        if len(text) + 1 + len(link) <= 500:
            text += " " + link
    return text


class DomainHistorySkill(Skill):
    """The shop's home page about 1, 3 and 12 months ago (Common Crawl, else the Internet Archive), compared."""

    name = "domain_history"
    description = "Compares the shop with earlier copies of its address in Common Crawl or the Internet Archive."
    page_types = STOREFRONTS
    tier = "expensive"
    always_run = False
    retry = True
    features = ("checked", "snapshots", "changed", "parked", "months_since_change")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Fetch the archived copies (cached per address) and compare them with the page.

        :param state: Current graph state; uses ``url``, ``facts``, ``cleaned_content`` and the domain age.
        :return: The skill's features and findings.
        """
        host = hostname(state.get("url", ""))
        if not host or not settings.network_lookups or not settings.domain_history:
            return self.output({"checked": False}, note="No address to look up.")
        found = await history(host)
        snapshots: list[dict[str, Any]] = found.get("snapshots", [])
        if not snapshots:
            asked = found.get("asked") or [found.get("archive", WAYBACK)]
            who = " and ".join(("the " if a == WAYBACK else "") + ARCHIVE_NAMES[a][0] for a in asked)
            have = "have" if len(asked) > 1 else "has"
            observation = Observation(
                type="no_archive_copies",
                evidence=f"{who[0].upper()}{who[1:]} {have} no copies of {host} from the past year to compare with.",
            )
            return self.output({"checked": True, "snapshots": 0}, [observation])

        text = state.get("cleaned_content", "")
        names, label = shop_names(state.get("facts") or PageFacts()), address_name(host)
        lang_now, words_now = language(text), top_words(text)
        observations: list[Observation] = []
        changed: list[tuple[dict[str, Any], str]] = []
        for snapshot in snapshots:
            outcome = compare(snapshot, lang_now=lang_now, words_now=words_now, names=names, label=label)
            what = {"same": "the same shop", "unclear": "a page too thin to compare"}.get(outcome) or describe(
                snapshot, outcome
            )
            archive = ARCHIVE_NAMES[snapshot.get("archive", WAYBACK)][0]
            month = month_of(snapshot["timestamp"])
            observations.append(
                Observation(
                    type="archive_copy",
                    evidence=f"{archive} copy of {host} from {month}: {what}. {copy_link(snapshot)}",
                )
            )
            if outcome in {"parked", "placeholder", "different"}:
                changed.append((snapshot, outcome))

        features: dict[str, float | bool] = {"checked": True, "snapshots": len(snapshots)}
        if changed:
            latest, outcome = changed[0]  # newest first
            known = state.get("features", {})
            years = int(known.get("domain_age.age_years", 0.0))
            since = int(known.get("domain_age.archive_first_year", 0.0))
            if known.get("domain_age.age_from_archive", 0.0) >= 1 and since:
                age = f"{host} has been online since at least {since} (per {shopper.seen_by(known)}), but"
            else:
                age = f"{host} is {years} years old, but" if years >= 1 else f"{host}:"
            when = _day(latest["timestamp"])
            today = datetime.now(tz=UTC).date()
            features |= {
                "changed": True,
                "parked": any(o == "parked" for _, o in changed),
                "months_since_change": round(((today - when).days if when else 0) / 30.4, 1),
            }
            owner = ARCHIVE_NAMES[latest.get("archive", WAYBACK)][1]
            said = f"{age} {owner} copy from {month_of(latest['timestamp'])} shows {describe(latest, outcome)}."
            observations.append(Observation(type="domain_repurposed", evidence=_with_links(said, changed)))
        return self.output(features, observations)
