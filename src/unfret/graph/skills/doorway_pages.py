"""
Doorway pages: a landing page per town ("Loodgieter Utrecht", "Plumber Austin") and no real local address behind them.

The classic pattern of a call centre posing as many local businesses: dozens of near-identical city pages, one
central phone number on all of them, and no street address in any of those towns. Read from the site's own links
to its other pages and its structured data's ``areaServed``. A real local business often has a few such pages too
(SEO for the towns around it), so the pattern needs both: many city pages and no street address.
"""

import re

from unfret.graph.skills import SERVICE_SITES, Observation, Skill, SkillOutput
from unfret.graph.skills.service_identity import has_street_address
from unfret.graph.state import AnalysisState
from unfret.market import service_market

MIN_CITY_PAGES = 5
_SHOWN = 3
# Trades as their landing pages name them (Dutch and US English).
_TRADES = (
    r"loodgieters?(?:bedrijf)?|slotenmakers?|elektriciens?|installateurs?|cv[- ]monteur|ontstoppingsdienst|ontstopping"
    r"|rioolservice|riool ontstoppen|dakdekkers?|glaszetters?|schilders?|stukadoors?|timmerman|klusjesman|klusbedrijf"
    r"|verhuisbedrijf|verhuizers?|schoonmaakbedrijf|glazenwassers?|ongediertebestrijding|hoveniers?"
    r"|witgoed reparatie|wasmachine reparatie|plumbers?|plumbing|locksmiths?|electricians?|electrical|hvac"
    r"|ac repair|air conditioning repair|furnace repair|heating and cooling|roofers?|roofing|movers|moving company"
    r"|cleaning services?|house cleaning|maid service|handyman|garage door repair|pest control"
    r"|water damage(?: restoration)?|carpet cleaning|painters?|painting|towing|appliance repair|drain cleaning"
    r"|junk removal|tree service|landscaping|pressure washing|window cleaning"
)
_CITY_LINK_RE = re.compile(
    rf"^(?:emergency |24/7 |24 hour |spoed |24 uurs? )?(?:{_TRADES})"
    r"(?: (?:service|services|company|bedrijf|dienst|repair))?(?: (?:in|near|te|nabij|voor|regio))? "
    r"(?P<place>[a-zà-ÿ][a-zà-ÿ ,.'-]{1,39})$",
    re.IGNORECASE,
)
# What follows a trade on a page that isn't about a place.
_NOT_PLACES = frozenset(
    {"near me", "prijzen", "prijs", "tarieven", "kosten", "spoed", "service", "services", "diensten", "offerte"}
    | {
        "me",
        "you",
        "contact",
        "reviews",
        "faq",
        "blog",
        "cost",
        "costs",
        "prices",
        "pricing",
        "residential",
        "commercial",
    }
    | {"emergency", "nodig", "inhuren", "nood", "noodgeval", "24/7", "vacatures", "jobs", "careers", "tips"}
    | {"installatie", "installation", "reparatie", "repair", "onderhoud", "maintenance", "aan huis", "at home"}
)
_STATE_SUFFIX_RE = re.compile(r",?\s+[a-z]{2}$")
_ORIGIN_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/?#]*", re.IGNORECASE)
_SLUG_RE = re.compile(r"/([^/?#]+)/?(?:[?#].*)?$")


def _place(candidate: str) -> str:
    m = _CITY_LINK_RE.match(" ".join(candidate.split()))
    if not m:
        return ""
    place = _STATE_SUFFIX_RE.sub("", m.group("place").lower().strip(" .-'"))
    return "" if place in _NOT_PLACES or len(place) < 2 else place


def city_pages(links: list[tuple[str, str]]) -> list[str]:
    """
    The towns the site has a landing page for: links named "<trade> <town>" (by their text or their address).

    :param links: The site's links to its own pages, as ``(text, url)``.
    :return: The towns, distinct, in the order linked.
    """
    places: list[str] = []
    for text, url in links:
        slug = _SLUG_RE.search(_ORIGIN_RE.sub("", url))
        place = _place(text) or (_place(re.sub(r"[-_]+", " ", slug.group(1))) if slug else "")
        if place and place not in places:
            places.append(place)
    return places


class DoorwayPagesSkill(Skill):
    """Many near-identical town landing pages without a local street address."""

    name = "doorway_pages"
    description = "Finds many '<trade> <town>' landing pages on a service site without a local street address."
    page_types = SERVICE_SITES
    features = ("city_pages", "areas_served", "street_address", "central_number_only", "doorway")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Count the town landing pages the site links to, and look for a street address and a local number.

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        places = city_pages([(link.text, link.url) for link in facts.internal_links])
        address = has_street_address(facts, service_market(facts, state.get("url", "")))
        central = bool(facts.phones) and not any(p.kind == "local" for p in facts.phones)
        doorway = len(places) >= MIN_CITY_PAGES and not address
        observations: list[Observation] = []
        if places:
            shown = ", ".join(p.title() for p in places[:_SHOWN]) + (" and others" if len(places) > _SHOWN else "")
            observations.append(
                Observation(
                    type="city_pages",
                    evidence=f"Links to landing pages for {len(places)} town{'s' if len(places) != 1 else ''}: "
                    f"{shown}."[:500],
                )
            )
        if doorway:
            number = " and only a central phone number" if central else ""
            observations.append(
                Observation(
                    type="doorway",
                    evidence=f"{len(places)} town landing pages, but no street address in any of them{number}.",
                )
            )
        return self.output(
            {
                "city_pages": len(places),
                "areas_served": len(facts.cities_served),
                "street_address": address,
                "central_number_only": central,
                "doorway": doorway,
            },
            observations,
        )
