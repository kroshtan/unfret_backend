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
    | {"homeguide"}
)
_PLATFORM_NAMES = r"thumbtack|angi|homeadvisor|werkspot|bark|houzz|taskrabbit|checkatrade|myhammer|homeguide"
_MEMBER_SINCE_RE = re.compile(
    rf"(?:lid sinds|actief sinds|op werkspot sinds|member since|on (?:{_PLATFORM_NAMES}) since|joined(?: in)?)"
    r"\s*:?\s*(?:([a-zé]{3,10})\.?\s+)?((?:19|20)\d\d)(?![0-9])",
    re.IGNORECASE,
)
_JOBS_RES = (
    re.compile(
        r"(\d[\d.,]*)\s+(?:klussen|opdrachten)\s+(?:uitgevoerd|afgerond|voltooid|via werkspot|op werkspot)",
        re.IGNORECASE,
    ),
    re.compile(r"(\d[\d.,]*)\s+hires?(?![a-z])", re.IGNORECASE),
    re.compile(r"hired\s+(\d[\d.,]*)\s+times?", re.IGNORECASE),
    re.compile(r"(\d[\d.,]*)\s+(?:jobs|tasks|projects)\s+completed", re.IGNORECASE),
    re.compile(r"(\d[\d.,]*)\s+(?:completed|verified)\s+(?:jobs|tasks|projects)", re.IGNORECASE),
)
_YEARS_RES = (
    re.compile(r"(\d{1,3})\s+(?:years? in business|jaar (?:actief|ervaring|in bedrijf))", re.IGNORECASE),
    re.compile(r"years? in business\s*:?\s*(\d{1,3})(?![\d])", re.IGNORECASE),
)
# A rating and its count: "4.8 (127 reviews)", "4,5 ★ uit 160 beoordelingen", "5.0 · 23 Reviews". The rating is never
# the first digit of a longer number ("160 beoordelingen" is not a 1 from 60).
_RATING_RE = re.compile(
    r"(?<![\d.,])([0-5](?:[.,]\d{1,2})?)(?![\d])\s*(?:/\s*5|out of 5|uit 5|van 5)?(?![\d])"
    r"(?:\s*(?:stars?|sterren|[★☆]+))?\s*(?:[(·|-]|uit|from|based on|op basis van)?\s*"
    r"(\d[\d.,]*)\s*\)?\s*(?:verified\s+)?(?:reviews?|beoordelingen|ratings?|recensies)(?![a-z])"
    # Thumbtack and Angi write the count alone in brackets after the stars: "4.9 ★★★★★ (143)".
    r"|(?<![\d.,])([0-5][.,]\d{1,2})\s*[★☆]*\s*\((\d[\d.,]*)\)",
    re.IGNORECASE,
)
# A count of reviews without a rating next to it ("2 reviews"), and a profile that says it has none yet.
_REVIEW_COUNT_RE = re.compile(
    r"(?<![\d.,])(\d[\d.,]*)\s+(?:verified\s+)?(?:reviews?|beoordelingen|recensies)(?![a-z])", re.IGNORECASE
)
_NO_REVIEWS_RE = re.compile(
    r"(?<![a-z])(?:no reviews(?: yet)?|nog geen (?:beoordelingen|reviews|recensies)|geen beoordelingen)(?![a-z])",
    re.IGNORECASE,
)
_RESPONSE_RATE_RES = (
    re.compile(r"(\d{1,3})\s?%\s*(?:response rate|reactiepercentage|reageert)", re.IGNORECASE),
    re.compile(r"(?:response rate|reactiepercentage)\s*:?\s*(\d{1,3})\s?%", re.IGNORECASE),
)
_RESPONSE_TIME_RE = re.compile(
    r"(?:responds|reageert|reactietijd|response time)\s*:?\s*(?:usually|typically|meestal|gemiddeld)?\s*"
    r"((?:within|in about|about|binnen)(?:\s+about)?\s+(?:a|an|one|een|\d+)\s+"
    r"(?:minutes?|hours?|days?|minuten|uur|uren|dag|dagen))",
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
    "KvK-nummer gecontroleerd",
    "Best of Houzz",
    "Verified License",
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
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    return float(value) if isinstance(value, int | float) else parse_number(value)


def _rating(orgs: list[dict[str, Any]], text: str) -> tuple[float | None, int | None]:
    for org in orgs:
        node = org.get("aggregateRating")
        if isinstance(node, dict):
            value = _ld_number(node.get("ratingValue"))
            count = _ld_number(node.get("reviewCount") or node.get("ratingCount"))
            if value is not None and 0 <= value <= 5:
                return value, int(count) if count is not None and count >= 0 else None
    if m := _RATING_RE.search(text):
        value = parse_number(m.group(1) or m.group(3))
        count = parse_count(m.group(2) or m.group(4))
        # "0.0 (0 reviews)" is no rating at all.
        return (value if value is not None and value <= 5 and count != 0 else None), count
    if _NO_REVIEWS_RE.search(text):
        return None, 0
    if m := _REVIEW_COUNT_RE.search(text):
        return None, parse_count(m.group(1))
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
    years = next((m for pattern in _YEARS_RES if (m := pattern.search(text))), None)
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
