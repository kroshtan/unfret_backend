"""
Doorway pages and the other marks of a call centre posing as a local business.

The classic pattern: dozens of near-identical city pages ("Loodgieter Utrecht", "Plumber Austin"), one central phone
number on all of them, and no street address in any of those towns. Read from the site's own links to its other
pages and its structured data's ``areaServed``. A real local business often has a few such pages too (SEO for the
towns around it), so the pattern needs both: many city pages and no street address.

The same sites show it on a single page too, which is what a visitor usually lands on:

- a site named after nothing but a trade and a town (``zwolle-slotenmakers.nl``, ``loodgieters.nl``) rather than a
  company, the town being one the page itself says it works in;
- the page saying the tradesperson you reach "isn't necessarily based in" the town;
- only national phone numbers (NL 085/088, 0800, 0900; US toll-free), no local or mobile number, on a page about
  a town or with a Dutch number that can be rented anywhere (085, 088, …);
- a page about one town whose every address is in another town.

Each is only something to note on its own (a real firm may own a keyword domain or use an 088 number); the verdict
weighs them together with the company's identity.
"""

import re

from unfret.graph.skills import SERVICE_SITES, Observation, Skill, SkillOutput
from unfret.graph.skills.service_identity import has_street_address
from unfret.graph.state import AnalysisState
from unfret.market import service_market
from unfret.net import hostname

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


# The trades as one word, as domain names write them ("slotenmakers", "loodgietersbedrijf", "garagedoorrepair"),
# longest first so "slotenmakerij" isn't read as "slotenmaker" and a town "ij…".
_TRADE_WORDS = sorted(
    {t.replace(" ", "") for t in _TRADES.split("|")}
    | {"slotenmakerij", "slotenservice", "loodgietersbedrijf", "loodgietersservice", "installatiebedrijf"},
    key=lambda t: (len(re.sub(r"[?()\[\]:]", "", t)), t),
    reverse=True,
)
_TRADE_WORD_RE = re.compile(
    r"(?:" + "|".join(_TRADE_WORDS) + r")(?:service|services|dienst|bedrijf|company|pros?|direct|nu|now)*",
    re.IGNORECASE,
)
# The page saying the tradesperson isn't local: "De slotenmaker die u via deze website bereikt is niet perse
# gevestigd in Nieuwegein", "our technicians are not necessarily located in your town".
_NOT_LOCAL_RE = re.compile(
    r"niet\s+(?:per\s?se|persé|perse|noodzakelijk(?:erwijs)?|altijd|zelf)\s+(?:\w+\s+){0,4}?gevestigd"
    r"|not\s+necessarily\s+(?:be\s+)?(?:located|based)\s+in",
    re.IGNORECASE,
)
# Words around the trade in a domain name that name neither a company nor a place ("spoed-slotenmaker-24uur").
_MODIFIERS = frozenset(
    {"spoed", "24uur", "24uurs", "247", "24", "emergency", "nood", "uw", "de", "het", "the", "best"}
)
_NATIONAL_KINDS = frozenset({"national", "toll_free", "premium"})
_SPACES_RE = re.compile(r"[\s'.-]+")
_TAG_RE = re.compile(r"<(script|style)\b.*?</\1\s*>|<[^>]+>", re.IGNORECASE | re.DOTALL)
_MAX_SOURCE = 500_000


def _flat(text: str) -> str:
    return _SPACES_RE.sub("", text.lower())


def _uses_place(place: str, text: str) -> bool:
    """
    Whether the page names ``place`` as a town it works in ("in Den Bosch", "regio Utrecht", "serving Austin").

    :param place: The candidate town, as one word ("denbosch").
    :param text: The page text (title included).
    :return: ``True`` when it does.
    """
    words = r"[\s'.-]*".join(re.escape(c) for c in place)
    pattern = rf"\b(?:in|te|regio|omgeving van|near|serving|around)\s+{words}(?![a-zà-ÿ])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def trade_town_name(url: str, text: str) -> tuple[bool, str] | None:
    """
    Whether the site is named after a trade (and a town) rather than a company: ``zwolle-slotenmakers.nl``.

    :param url: The page URL.
    :param text: The page text (title included), which must name the town as one it works in.
    :return: ``None`` when the domain names something else (a company, a person); else ``(True, town)``, the town
        empty for a domain that is the trade alone (``loodgieters.nl``).
    """
    host = hostname(url).removeprefix("www.")
    if "." not in host:
        return None
    label = host.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    rest: list[str] = []
    trade = False
    for token in filter(None, re.split(r"[-_]", label)):
        if token.isdigit():
            continue
        m = _TRADE_WORD_RE.search(token)
        if m and not trade:
            trade = True
            rest += [part for part in (token[: m.start()], token[m.end() :]) if part]
        else:
            rest.append(token)
    rest = [part for part in rest if part.lower() not in _MODIFIERS]
    if not trade:
        return None
    place = "".join(rest)
    if not place:
        return True, ""
    return (True, place) if len(place) >= 3 and _uses_place(place, text) else None


def page_town(facts_title: str, url: str, text: str) -> str:
    """
    The town a page is about: the one its domain is named after, else the "<trade> <town>" in its title.

    :param facts_title: The page title.
    :param url: The page URL.
    :param text: The page text.
    :return: The town in lower case, or empty.
    """
    named = trade_town_name(url, text)
    if named and named[1]:
        return named[1]
    town = _place(facts_title.split("|", maxsplit=1)[0].split(" - ", maxsplit=1)[0].strip())
    return town if town and _uses_place(_flat(town), text) else ""


def address_elsewhere(town: str, addresses: list[str]) -> bool:
    """
    Whether every address the site gives is in a town other than ``town`` (addresses naming no town don't count).

    :param town: The town the page is about.
    :param addresses: The site's addresses.
    :return: ``True`` when there are addresses naming a town and none of them is ``town``.
    """
    flat = _flat(town)
    named = [a for a in addresses if re.search(r"[1-9]\d{3}\s?[A-Z]{2}\s*,?\s*[A-Za-zÀ-ÿ]{3,}|,\s*[A-Za-z]{3,}", a)]
    return bool(flat) and bool(named) and not any(flat in _flat(a) for a in named)


class DoorwayPagesSkill(Skill):
    """Many near-identical town landing pages without a local street address."""

    name = "doorway_pages"
    description = "Finds many '<trade> <town>' landing pages on a service site without a local street address."
    page_types = SERVICE_SITES
    features = (
        "city_pages",
        "areas_served",
        "street_address",
        "central_number_only",
        "doorway",
        "trade_town_name",
        "not_local",
        "national_number_only",
        "local_page",
        "address_elsewhere",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Count the town landing pages the site links to, and look for a street address and a local number.

        :param state: Current graph state; uses ``facts``, ``url`` and ``cleaned_content``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        url = state.get("url", "") or facts.url
        text = f"{facts.title} {state.get('cleaned_content', '')}"
        places = city_pages([(link.text, link.url) for link in facts.internal_links])
        address = has_street_address(facts, service_market(facts, state.get("url", "")))
        central = bool(facts.phones) and not any(p.kind == "local" for p in facts.phones)
        doorway = len(places) >= MIN_CITY_PAGES and not address
        named = trade_town_name(url, text)
        town = page_town(facts.title, url, text)
        # The statement often sits in the footer, which the cleaned text leaves out: then read the page's own text.
        not_local = _NOT_LOCAL_RE.search(text)
        if not_local is None:
            text = " ".join(_TAG_RE.sub(" ", state.get("page_content", "")[:_MAX_SOURCE]).split())
            not_local = _NOT_LOCAL_RE.search(text)
        local_page = bool(named) or bool(town) or bool(places)
        # Only non-geographic numbers: on a local page, or when one is a Dutch 084/085/087/088/091 number (rented
        # anywhere, unlike a freephone 0800 number on its own).
        national = (
            bool(facts.phones)
            and all(p.kind in _NATIONAL_KINDS for p in facts.phones)
            and (local_page or any(p.kind == "national" for p in facts.phones))
        )
        elsewhere = address_elsewhere(town, facts.addresses)
        observations: list[Observation] = []
        if named:
            observations.append(
                Observation(
                    type="trade_town_name",
                    evidence=f"Named after a trade{' and a town' if named[1] else ''}: "
                    f"{hostname(url).removeprefix('www.')}.",
                )
            )
        if not_local:
            lo = max(text.rfind(".", 0, not_local.start()) + 1, not_local.start() - 100)
            hi = text.find(".", not_local.end())
            quote = " ".join(text[lo : hi if hi >= 0 else not_local.end() + 60].split())[:200]
            observations.append(Observation(type="not_local", evidence=f'"{quote}"'))
        if national:
            observations.append(
                Observation(
                    type="national_number_only", evidence="Only national phone numbers, no local or mobile one."
                )
            )
        if elsewhere:
            observations.append(
                Observation(
                    type="address_elsewhere",
                    evidence=f"About {town.title()}, but its address is elsewhere: {facts.addresses[0][:120]}.",
                )
            )
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
                "trade_town_name": named is not None,
                "not_local": not_local is not None,
                "national_number_only": national,
                "local_page": local_page,
                "address_elsewhere": elsewhere,
            },
            observations,
        )
