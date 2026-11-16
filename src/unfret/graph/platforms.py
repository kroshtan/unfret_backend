"""
Known peer-to-peer marketplaces and service platforms: which page class their listing pages are, without an LLM.

Used only while ``settings.expansion_classes`` is on. The platform comes from the page facts (the extractor reads
it from the host, or the canonical link); the path tells a listing or profile from a search page or the home page,
which go to the LLM instead. eBay hosts both: a business seller's listing is a marketplace listing like Amazon's, a
private seller's an ad (and the default when the page doesn't say).
"""

import re
from dataclasses import dataclass

from unfret.facts.schema import PageFacts, Platform, SellerType
from unfret.graph.state import PageType


@dataclass(frozen=True)
class KnownPlatform:
    """A platform whose listing pages are one page class."""

    name: str
    page_type: PageType
    listing_path: re.Pattern[str]


PLATFORMS: dict[Platform, KnownPlatform] = {
    "ebay": KnownPlatform("eBay", "p2p_listing", re.compile(r"/itm/")),
    "marktplaats": KnownPlatform("Marktplaats", "p2p_listing", re.compile(r"/[va]/.+/m\d{6,}")),
    "2dehands": KnownPlatform("2dehands", "p2p_listing", re.compile(r"/[va]/.+/m\d{6,}")),
    "vinted": KnownPlatform("Vinted", "p2p_listing", re.compile(r"/items/\d+")),
    "craigslist": KnownPlatform("Craigslist", "p2p_listing", re.compile(r"/\d{8,}\.html")),
    "facebook": KnownPlatform("Facebook Marketplace", "p2p_listing", re.compile(r"^/marketplace/item/\d+")),
    "werkspot": KnownPlatform("Werkspot", "service_listing", re.compile(r"/profiel/")),
    "thumbtack": KnownPlatform("Thumbtack", "service_listing", re.compile(r"/service/\d+")),
    "angi": KnownPlatform("Angi", "service_listing", re.compile(r"/companylist/.+\.htm")),
    "homeadvisor": KnownPlatform("HomeAdvisor", "service_listing", re.compile(r"/rated\.")),
    "checkatrade": KnownPlatform("Checkatrade", "service_listing", re.compile(r"/trades/[^/]+")),
    "taskrabbit": KnownPlatform("TaskRabbit", "service_listing", re.compile(r"/profile/")),
    "bark": KnownPlatform("Bark", "service_listing", re.compile(r"/company/")),
    "houzz": KnownPlatform("Houzz", "service_listing", re.compile(r"/(?:pro|professionals/[^/]+)/[^/]+")),
    "myhammer": KnownPlatform("MyHammer", "service_listing", re.compile(r"/(?:auftragnehmer|handwerker)/")),
    # A pro's profile: /<state>/<city>/<trade>/<pro>; shorter paths are city and trade listings.
    "homeguide": KnownPlatform("HomeGuide", "service_listing", re.compile(r"^/[a-z]{2}/[^/]+/[^/]+/[^/?#]+")),
}

# How eBay labels its sellers, in the languages of its sites.
_BUSINESS_SELLER_RE = re.compile(
    r"business seller|registered as a business|gewerblicher verkäufer|vendeur professionnel|zakelijke verkoper"
    r"|venditore professionale|vendedor profesional",
    re.IGNORECASE,
)
_PRIVATE_SELLER_RE = re.compile(
    r"private seller|privater verkäufer|privatverkäufer|vendeur particulier|particuliere verkoper"
    r"|venditore privato|vendedor particular",
    re.IGNORECASE,
)
# Organization types a service company's own site uses (schema.org's trades and services).
SERVICE_LD_TYPES = frozenset(
    {
        "homeandconstructionbusiness",
        "plumber",
        "electrician",
        "locksmith",
        "hvacbusiness",
        "roofingcontractor",
        "generalcontractor",
        "housepainter",
        "movingcompany",
        "emergencyservice",
        "professionalservice",
        "autorepair",
        "drycleaningorlaundry",
        "childcare",
    }
)
_PATH_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^/?#]*([^?#]*)")


def _path(url: str) -> str | None:
    m = _PATH_RE.match(url.strip())
    return (m.group(1) or "/") if m else None


def seller_type(facts: PageFacts, text: str) -> SellerType:
    """
    Whether the platform shows the seller as a business or a private person.

    :param facts: The page facts (a platform extractor may have read it already).
    :param text: The page text.
    :return: ``business``, ``private`` or ``""`` when the page doesn't say (or says both).
    """
    if facts.seller_profile is not None and facts.seller_profile.seller_type:
        return facts.seller_profile.seller_type
    business, private = bool(_BUSINESS_SELLER_RE.search(text)), bool(_PRIVATE_SELLER_RE.search(text))
    if business != private:
        return "business" if business else "private"
    return ""


def platform_rule(facts: PageFacts, url: str, text: str) -> tuple[PageType, str] | None:
    """
    The page class of a listing or profile page on a known platform.

    :param facts: The page facts.
    :param url: The page URL (the facts' own URL when empty).
    :param text: The page text (eBay's seller label).
    :return: The page type and the platform's name, or ``None`` when the platform is unknown or the page is not one
        of its listings (a search page, the home page): the LLM decides those. Without a URL, a titled page with a
        price counts as an ad on a peer-to-peer platform.
    """
    known = PLATFORMS.get(facts.platform)
    if known is None:
        return None
    path = _path(url or facts.url)
    if path is None:
        listing = known.page_type == "p2p_listing" and bool(facts.title) and facts.price is not None
    else:
        listing = bool(known.listing_path.search(path))
    if not listing:
        return None
    if facts.platform == "ebay" and seller_type(facts, text) == "business":
        return "aggregate_listing", known.name
    return known.page_type, known.name


def is_known_platform(facts: PageFacts | None) -> bool:
    """
    Whether the page is on a peer-to-peer or service platform (its other pages aren't shops either).

    :param facts: The page facts.
    :return: ``True`` for the platforms in :data:`PLATFORMS`.
    """
    return facts is not None and facts.platform in PLATFORMS


def service_site_signals(facts: PageFacts | None) -> bool:
    """
    Whether the page's structured data describes a service company (a plumber, a locksmith, a removals firm, …).

    :param facts: The page facts.
    :return: ``True`` when an Organization block has one of :data:`SERVICE_LD_TYPES`.
    """
    if facts is None:
        return False
    for org in facts.organization_ld:
        kind = org.get("@type", "")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(isinstance(k, str) and k.lower() in SERVICE_LD_TYPES for k in kinds):
            return True
    return False
