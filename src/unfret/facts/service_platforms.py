"""
A service provider's profile on a platform (Werkspot, Thumbtack, Angi, …): what the platform shows about them.

Fills :class:`~unfret.facts.schema.SellerProfile`: member since, jobs completed, years in business, rating and its
count, badges and response rate and time, from the profile's structured data (its LocalBusiness block) and the
platform's own wording in the page text, read outside the visitor's areas (a logged-in visitor's account menu
says "member since" too). The platform's reviews go into ``reviews``, without the reviewers' names. Used during the
check only. The browser extension reads the same (``src/extract/services.ts``, kept in parity).
"""

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from unfret.facts import testimonials
from unfret.facts._parse import parse_count, parse_date, parse_number
from unfret.facts.business import public_text
from unfret.facts.schema import PageFacts, Review, SellerProfile

SERVICE_PLATFORMS: frozenset[str] = frozenset(
    {"werkspot", "thumbtack", "angi", "homeadvisor", "checkatrade", "taskrabbit", "bark", "houzz", "myhammer"}
)
_PLATFORM_NAMES = r"thumbtack|angi|homeadvisor|werkspot|bark|houzz|taskrabbit|checkatrade|myhammer"
_MEMBER_SINCE_RE = re.compile(
    rf"(?:lid sinds|actief sinds|op werkspot sinds|member since|on (?:{_PLATFORM_NAMES}) since|joined(?: in)?)"
    r"\s*:?\s*(?:([a-zé]{3,10})\.?\s+)?((?:19|20)\d\d)(?![0-9])",
    re.IGNORECASE,
)
_JOBS_RES = (
    re.compile(r"(\d[\d.,]*)\s+(?:klussen|opdrachten)\s+(?:uitgevoerd|afgerond|voltooid)", re.IGNORECASE),
    re.compile(r"(\d[\d.,]*)\s+hires?(?![a-z])", re.IGNORECASE),
    re.compile(r"hired\s+(\d[\d.,]*)\s+times?", re.IGNORECASE),
    re.compile(r"(\d[\d.,]*)\s+(?:jobs|tasks|projects)\s+completed", re.IGNORECASE),
    re.compile(r"(\d[\d.,]*)\s+(?:completed|verified)\s+(?:jobs|tasks|projects)", re.IGNORECASE),
)
_YEARS_RE = re.compile(r"(\d{1,3})\s+(?:years? in business|jaar (?:actief|ervaring|in bedrijf))", re.IGNORECASE)
_RATING_RE = re.compile(
    r"(?<![\d.,])([0-5](?:[.,]\d)?)\s*(?:/\s*5|out of 5|uit 5|van 5)?(?:\s*(?:stars?|sterren))?\s*[(·|-]?\s*"
    r"(\d[\d.,]*)\s*(?:reviews?|beoordelingen|ratings?|recensies)(?![a-z])",
    re.IGNORECASE,
)
_RESPONSE_RATE_RES = (
    re.compile(r"(\d{1,3})\s?%\s*(?:response rate|reactiepercentage|reageert)", re.IGNORECASE),
    re.compile(r"(?:response rate|reactiepercentage)\s*:?\s*(\d{1,3})\s?%", re.IGNORECASE),
)
_RESPONSE_TIME_RE = re.compile(
    r"(?:responds|reageert|reactietijd|response time)\s*:?\s*((?:within|in about|about|binnen|gemiddeld binnen)\s+"
    r"(?:a|an|one|een|\d+)\s+(?:minutes?|hours?|days?|minuten|uur|uren|dag|dagen))",
    re.IGNORECASE,
)
# Badges the platforms award, as they write them (matched case-insensitively, recorded as written here).
BADGES = (
    "Top Pro",
    "Background checked",
    "License verified",
    "Angi Certified",
    "Super Service Award",
    "Screened & Approved",
    "Elite Service",
    "Top Rated",
    "Topvakman",
    "Geverifieerd vakman",
    "KvK geverifieerd",
    "Identiteit geverifieerd",
    "Erkend vakman",
)
_BADGE_RES = tuple((badge, re.compile(rf"(?<![a-z]){re.escape(badge)}(?![a-z])", re.IGNORECASE)) for badge in BADGES)


def _member_since(text: str) -> date | None:
    m = _MEMBER_SINCE_RE.search(text)
    if not m:
        return None
    year = int(m.group(2))
    month = parse_date(f"1 {m.group(1)} {year}") if m.group(1) else None
    return month or date(year, 1, 1)


def _jobs(text: str) -> int | None:
    for pattern in _JOBS_RES:
        if m := pattern.search(text):
            return parse_count(m.group(1))
    return None


def _ld_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    return float(value) if isinstance(value, int | float) else parse_number(str(value))


def _rating(orgs: list[dict[str, Any]], text: str) -> tuple[float | None, int | None]:
    for org in orgs:
        node = org.get("aggregateRating")
        if isinstance(node, dict):
            value = _ld_number(node.get("ratingValue"))
            count = _ld_number(node.get("reviewCount") or node.get("ratingCount"))
            if value is not None and 0 <= value <= 5:
                return value, int(count) if count is not None and count >= 0 else None
    if m := _RATING_RE.search(text):
        value = parse_number(m.group(1))
        return (value if value is not None and value <= 5 else None), parse_count(m.group(2))
    return None, None


def _response_rate(text: str) -> float | None:
    for pattern in _RESPONSE_RATE_RES:
        if (m := pattern.search(text)) and int(m.group(1)) <= 100:
            return float(m.group(1))
    return None


def profile(text: str, orgs: list[dict[str, Any]]) -> SellerProfile | None:
    """
    The provider as the platform shows them.

    :param text: The page text outside the visitor's areas.
    :param orgs: The page's Organization/LocalBusiness nodes.
    :return: The profile, or ``None`` when the page shows none of it.
    """
    rating, count = _rating(orgs, text)
    years = _YEARS_RE.search(text)
    rate = _response_rate(text)
    response_time = _RESPONSE_TIME_RE.search(text)
    found = SellerProfile(
        member_since=_member_since(text),
        rating=rating,
        rating_count=count,
        badges=[badge for badge, pattern in _BADGE_RES if pattern.search(text)],
        response_time=response_time.group(1)[:100] if response_time else "",
        response_rate=rate,
        jobs_completed=_jobs(text),
        years_in_business=int(years.group(1)) if years and int(years.group(1)) <= 200 else None,
    )
    return found if found != SellerProfile() else None


def extract(soup: BeautifulSoup, facts: PageFacts, orgs: list[dict[str, Any]]) -> None:
    """
    Fill ``seller_profile`` and, when the page has none yet, ``reviews`` from a service platform's profile page.

    :param soup: The parsed page (not modified).
    :param facts: The facts to fill.
    :param orgs: The page's Organization/LocalBusiness nodes, untrimmed.
    """
    facts.seller_profile = profile(public_text(soup), orgs)
    if not facts.reviews:
        facts.reviews = [
            Review(rating=t.rating, posted=t.posted, body=t.text) for t in testimonials.extract(soup, orgs)
        ][:100]
