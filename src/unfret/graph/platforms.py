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
# Trades as service companies name themselves (Dutch and US English); also the doorway-page skill's vocabulary.
TRADES = (
    r"loodgieters?(?:bedrijf)?|slotenmakers?|elektriciens?|installateurs?|cv[- ]monteur|ontstoppingsdienst|ontstopping"
    r"|rioolservice|riool ontstoppen|dakdekkers?|glaszetters?|schilders?|stukadoors?|timmerman|klusjesman|klusbedrijf"
    r"|verhuisbedrijf|verhuizers?|schoonmaakbedrijf|glazenwassers?|ongediertebestrijding|hoveniers?"
    r"|witgoed reparatie|wasmachine reparatie|plumbers?|plumbing|locksmiths?|electricians?|electrical|hvac"
    r"|ac repair|air conditioning repair|furnace repair|heating and cooling|roofers?|roofing|movers|moving company"
    r"|cleaning services?|house cleaning|maid service|handyman|garage door repair|pest control"
    r"|water damage(?: restoration)?|carpet cleaning|painters?|painting|towing|appliance repair|drain cleaning"
    r"|junk removal|tree service|landscaping|pressure washing|window cleaning"
)
# A service company's own words for its work, beyond the trades' names (Dutch and English); the evidence a page
# needs before it may be a service site. Words shops use for their goods ("electrical", "painting") aren't in it.
_SERVICE_WORDS = (
    r"dakwerken?|dakdekkersbedrijf|installatie(?:techniek|bedrijf)|installatiebedrijf|elektrotechniek|loodgieterswerk"
    r"|cv[- ]ketels?|warmtepompen|timmerwerken?|timmerbedrijf|interieurbetimmeringen|betimmeringen|aannemersbedrijf"
    r"|aannemer|bouwbedrijf|renovatiebedrijf|verbouwingen|schoonmaakdienst|schoonmaakwerk|schilderwerken?"
    r"|schildersbedrijf|tegelzetters?|stukadoorsbedrijf|rioolontstopping|tuinonderhoud|hoveniersbedrijf"
    r"|klussenbedrijf|onze diensten|onze dienstverlening|offerte aanvragen|vrijblijvende offerte"
    r"|general contractors?|roofing contractors?|remodel(?:l)?ing|home repairs?|free estimates?"
    r"|licensed (?:and|&) insured|our services|request a quote|service area"
)
_GENERIC_TRADES = frozenset({"electrical", "painting", "painters", "painter", "towing"})
_TRADE_RE = re.compile(rf"(?<![a-z])(?:{TRADES})(?![a-z])", re.IGNORECASE)
_SERVICE_RE = re.compile(rf"(?<![a-z])(?:{TRADES}|{_SERVICE_WORDS})(?![a-z])", re.IGNORECASE)
# A shop's buttons and basket: a page with these sells products, whatever else it offers ("afrekenen" is not one:
# "pinnen of contant afrekenen" is how a locksmith is paid).
_SHOP_RE = re.compile(
    r"add to (?:cart|basket|bag)|in (?:de )?winkel(?:wagen|mand)|winkelwagen|winkelmand|shopping (?:cart|bag)"
    r"|(?<![a-z])checkout(?![a-z])|buy now|koop nu|bestel nu|in den warenkorb|warenkorb|ajouter au panier"
    r"|free shipping|gratis verzending|gratis bezorging",
    re.IGNORECASE,
)
_SERVICE_TEXT_CHARS = 3_000
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


def service_evidence(facts: PageFacts | None, text: str) -> bool:
    """
    Whether a page not settled as a shop shows a trade service: needed before the LLM's "service site" counts.

    The page has to name a trade (plumber, loodgieter, locksmith, removals, …) in its title or opening text, or
    publish what service companies publish (a licence, call-out or quote terms, towns served, service structured
    data); a page about finance, travel or software with none of that stays out of the service class.

    :param facts: The page facts.
    :param text: The page text.
    :return: ``True`` when the page shows a trade service.
    """
    if service_site_signals(facts):
        return True
    title = facts.title if facts is not None else ""
    if any(
        m.group().lower() not in _GENERIC_TRADES for m in _SERVICE_RE.finditer(f"{title} {text[:_SERVICE_TEXT_CHARS]}")
    ):
        return True
    return facts is not None and bool(facts.licences or facts.pricing_terms or len(facts.cities_served) >= 3)


def shop_signals(facts: PageFacts | None, text: str) -> bool:
    """
    Whether the page shows a shop: a price, product structured data, or a basket and checkout.

    :param facts: The page facts.
    :param text: The page text.
    :return: ``True`` for a page that sells products.
    """
    if facts is not None and (facts.price is not None or facts.json_ld):
        return True
    return bool(_SHOP_RE.search(text))


def strong_service_evidence(facts: PageFacts | None, text: str) -> bool:
    """
    Whether a page the product classifier called a shop is plainly a service company's site instead.

    Only without any sign of a shop (:func:`shop_signals`) and with a trade service shown
    (:func:`service_evidence`); the second classification step still has to agree.

    :param facts: The page facts.
    :param text: The page text.
    :return: ``True`` when the page should be asked about as a service site.
    """
    if facts is None or shop_signals(facts, text):
        return False
    return service_evidence(facts, text)
