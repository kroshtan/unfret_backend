"""
What the shop's address showed before: earlier copies of its home page in the Internet Archive.

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
archived. The archived copies are fetched like any outside page (:func:`unfret.net.fetch_public`: public addresses
only, a size limit, timeouts, no cookies), raw (``id_``, without the archive's own toolbar), and what they show is
cached per address as a store-level fact (:mod:`unfret.fact_cache`), never their HTML.
"""

import asyncio
import json
import re
import time
from collections import Counter, deque
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from unfret.config import settings
from unfret.fact_cache import cached_fact
from unfret.facts.schema import PageFacts
from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.skills.domain_age import candidates
from unfret.graph.state import AnalysisState
from unfret.html_cleaner import strip_to_text
from unfret.net import fetch_public, hostname

# About 1, 3 and 12 months before today; a copy counts for a target within this many days of it.
TARGETS_DAYS: tuple[tuple[int, int], ...] = ((30, 45), (91, 60), (365, 90))
_CDX_TIMEOUT_S = 8.0
_SNAPSHOT_TIMEOUT_S = 10.0
_TOTAL_TIMEOUT_S = 15.0
_MAX_CDX_BYTES = 256 * 1024
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


class ArchiveUnreachableError(ConnectionError):
    """None of the archived copies could be fetched: a connection problem, so the graph tries the skill once more."""


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
    Where a person sees an archived copy (with the archive's toolbar, so it is clearly an archived page).

    :param timestamp: The copy's 14-digit timestamp.
    :param original: The archived address.
    :return: The web.archive.org link.
    """
    return f"https://web.archive.org/web/{timestamp}/{original}"


def month_of(timestamp: str) -> str:
    """
    A copy's month in words.

    :param timestamp: The copy's 14-digit timestamp.
    :return: "March 2026".
    """
    return f"{_MONTHS[int(timestamp[4:6]) - 1]} {timestamp[:4]}"


class ArchiveBusyError(RuntimeError):
    """Unfret's own pace for the archive is used up, or the archive refused us lately: not asked, not retried."""


class Pace:
    """
    How often this process asks the Internet Archive: a few requests a minute, and none for a while after a refusal.

    The archive blocks a client that asks too often (it refuses connections for an hour, longer on repeat), which
    would cost every later deep check its look: so the skill asks within a pace of its own
    (``UNFRET_WAYBACK_PER_MINUTE``), and stops asking for ``UNFRET_WAYBACK_PAUSE_S`` after a refused connection or a
    "too many requests".
    """

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: deque[float] = deque()
        self.paused_until = 0.0

    def take(self) -> bool:
        """
        Take one request from the pace.

        :return: ``True`` when the request may be sent now.
        """
        now = time.monotonic()
        while self.sent and self.sent[0] <= now - 60:
            self.sent.popleft()
        if now < self.paused_until or len(self.sent) >= settings.wayback_per_minute:
            return False
        self.sent.append(now)
        return True

    def pause(self) -> None:
        """Stop asking for ``UNFRET_WAYBACK_PAUSE_S``."""
        self.paused_until = time.monotonic() + settings.wayback_pause_s


pace = Pace()


def _refused(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return isinstance(exc, httpx.ConnectError | ConnectionRefusedError)


async def _get(url: str, *, timeout: float, max_bytes: int) -> bytes:
    """
    One request to the archive, within :data:`pace`, like any outside page (:func:`unfret.net.fetch_public`).

    :param url: The archive address.
    :param timeout: The whole request's time limit.
    :param max_bytes: The largest answer accepted.
    :return: The answer's body.
    :raises ArchiveBusyError: When the pace is used up or the archive refused us lately.
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
    The home page's copies of the last 13 months, one a month (the archive's CDX index).

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


async def _snapshot(timestamp: str, original: str) -> dict[str, Any]:
    raw = f"{settings.wayback_url}/web/{timestamp}id_/{original}"
    body = await _get(raw, timeout=_SNAPSHOT_TIMEOUT_S, max_bytes=_MAX_SNAPSHOT_BYTES)
    summary = await asyncio.to_thread(summarise, body.decode("utf-8", errors="replace"))
    return {"timestamp": timestamp, "original": original} | summary


async def history(host: str, today: date | None = None) -> dict[str, Any]:
    """
    What the shop's home page showed about 1, 3 and 12 months ago.

    :param host: The shop's host.
    :param today: Today (tests pass their own).
    :return: ``captures`` (how many monthly copies the archive has) and ``snapshots`` (each picked copy's summary,
        newest first; a copy that couldn't be fetched is left out).
    :raises ArchiveUnreachableError: When none of the picked copies could be fetched (an index that can't be reached
        raises too, from :func:`captures`).
    """
    today = today or datetime.now(tz=UTC).date()
    rows = await captures(host, today)
    picked = pick(rows, today)
    fetched = await asyncio.gather(*(_snapshot(t, o) for t, o in picked), return_exceptions=True)
    snapshots = [s for s in fetched if isinstance(s, dict)]
    if picked and not snapshots:
        errors = sorted({type(e).__name__ for e in fetched if isinstance(e, BaseException)})
        raise ArchiveUnreachableError(f"No archived copy could be fetched ({', '.join(errors)}).")
    return {"captures": len(rows), "snapshots": snapshots}


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
    label = _squash(candidates(host)[0].split(".")[0]) if host else ""
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


class DomainHistorySkill(Skill):
    """The shop's home page about 1, 3 and 12 months ago (the Internet Archive), compared with the page now."""

    name = "domain_history"
    description = "Compares the shop with earlier copies of its address in the Internet Archive."
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
        found = await cached_fact("wayback", host, lambda: history(host)) or {}
        snapshots: list[dict[str, Any]] = found.get("snapshots", [])
        if not snapshots:
            observation = Observation(
                type="no_archive_copies",
                evidence=f"The Internet Archive has no copies of {host} from the past year to compare with.",
            )
            return self.output({"checked": True, "snapshots": 0}, [observation])

        text = state.get("cleaned_content", "")
        names, label = shop_names(state.get("facts") or PageFacts()), address_name(host)
        lang_now, words_now = language(text), top_words(text)
        observations: list[Observation] = []
        changed: list[tuple[dict[str, Any], str]] = []
        for snapshot in snapshots:
            outcome = compare(snapshot, lang_now=lang_now, words_now=words_now, names=names, label=label)
            link = archive_url(snapshot["timestamp"], snapshot["original"])
            what = {"same": "the same shop", "unclear": "a page too thin to compare"}.get(outcome) or describe(
                snapshot, outcome
            )
            observations.append(
                Observation(
                    type="archive_copy",
                    evidence=f"Internet Archive copy of {host} from {month_of(snapshot['timestamp'])}: {what}. {link}",
                )
            )
            if outcome in {"parked", "placeholder", "different"}:
                changed.append((snapshot, outcome))

        features: dict[str, float | bool] = {"checked": True, "snapshots": len(snapshots)}
        if changed:
            latest, outcome = changed[0]  # newest first
            years = int(state.get("features", {}).get("domain_age.age_years", 0.0))
            age = f"{host} is {years} years old, but" if years >= 1 else f"{host}:"
            when = _day(latest["timestamp"])
            today = datetime.now(tz=UTC).date()
            features |= {
                "changed": True,
                "parked": any(o == "parked" for _, o in changed),
                "months_since_change": round(((today - when).days if when else 0) / 30.4, 1),
            }
            links = " ".join(archive_url(s["timestamp"], s["original"]) for s, _ in changed)
            observations.append(
                Observation(
                    type="domain_repurposed",
                    evidence=f"{age} the Internet Archive's copy from {month_of(latest['timestamp'])} shows "
                    f"{describe(latest, outcome)}. {links}"[:500],
                )
            )
        return self.output(features, observations)
