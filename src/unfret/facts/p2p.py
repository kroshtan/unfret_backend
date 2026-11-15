"""
Peer-to-peer ads: the item, the deal and the seller as the platform shows them, never the seller's name.

Marktplaats, 2dehands, eBay, Vinted, Craigslist and Facebook Marketplace. Each platform gets a few areas of its
page (the ad text, the seller box, the item details, the price), found by CSS selectors or, where the markup has no
stable names (Facebook), by the heading above them. What is read from those areas is matched by wording, in Dutch,
English, German and French, rather than by markup, so a redesign that keeps the words keeps the facts. Anything not
found stays empty: a missing seller box is no profile, not a bad one.

The seller box is read for what it says about the account (member since, ratings, badges, other ads, response
time), never for who it is. The ad text (:attr:`ListingDetails.description`) is kept for the check only; contact
methods and payment requests are the *kinds* it mentions (``whatsapp``, ``bank_transfer``), not the details.

The browser extension runs the same rules on the live page (``src/extract/p2p.ts``, kept in parity).
"""

import re
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, Tag

from unfret.facts._parse import parse_count, parse_date, parse_number, parse_price, parse_since
from unfret.facts.schema import Delivery, ListingDetails, PageFacts, SellerProfile, SellerType


@dataclass(frozen=True)
class Areas:
    """Where a platform shows each part of an ad: CSS selectors, and heading texts as a fallback."""

    title: str = ""
    description: str = ""
    seller: str = ""
    details: str = ""
    price: str = ""
    location: str = ""
    listed: str = ""
    deal: str = ""
    description_headings: tuple[str, ...] = ()
    seller_headings: tuple[str, ...] = ()
    details_headings: tuple[str, ...] = ()
    protection: bool | None = None  # the platform always (True) or never (False) offers payment protection


_ADEVINTA = Areas(  # Marktplaats and 2dehands share their markup
    description='#vip-ad-description, [data-testid="vip-ad-description"], .Description-description',
    seller='[data-testid="seller-info"], .SellerInfo, .seller-info',
    details='[data-testid="attributes"], .Attributes, .Listing-attributes',
    price='[data-testid="price"], .Listing-price, .Price-price',
    location='[data-testid="location"], .SellerInfo-location, .Location-name',
    listed='[data-testid="listing-stats"], .Stats',
    deal='[data-testid="buy-box"], .BuyBlock, .Listing-payment',
    description_headings=("beschrijving", "omschrijving", "description"),
)
AREAS: dict[str, Areas] = {
    "marktplaats": _ADEVINTA,
    "2dehands": _ADEVINTA,
    "ebay": Areas(
        description='[data-testid="x-item-description-child"], #ds_div',
        seller='[data-testid="x-sellercard-atf"], [data-testid="str-about-seller"], .x-sellercard-atf',
        details='[data-testid="x-item-condition"], [data-testid="ux-labels-values--shipping"], '
        '[data-testid="ux-labels-values--itemLocation"], [data-testid="x-about-this-item"]',
        price='[data-testid="x-price-primary"], .x-price-primary',
        location='[data-testid="ux-labels-values--itemLocation"] .ux-labels-values__values',
        deal='[data-testid="x-buybox"], [data-testid="x-ebay-money-back"]',
        description_headings=("item description from the seller", "artikelbeschrijving", "artikelbeschreibung"),
    ),
    "vinted": Areas(
        description='[itemprop="description"], [data-testid="item-description"]',
        seller='[data-testid="profile-section"], [data-testid="item-seller"]',
        details='[data-testid="item-details"], .details-list',
        price='[data-testid="item-price"]',
        location='[data-testid="item-location"]',
        listed='[data-testid="item-upload-date"]',
        deal='[data-testid="item-buyer-protection"], [data-testid="item-sidebar"]',
        protection=True,
    ),
    "craigslist": Areas(
        title="#titletextonly",
        description="#postingbody",
        details=".attrgroup",
        price=".postingtitletext .price",
        location=".postingtitletext small",
        listed=".postinginfos",
        protection=False,
    ),
    "facebook": Areas(
        description_headings=("seller's description", "beschrijving van de verkoper", "description", "beschrijving"),
        seller_headings=("seller information", "seller details", "informatie over verkoper", "verkopergegevens"),
        details_headings=("details",),
        price='[data-testid="marketplace-price"]',
    ),
}
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "strong", "span", "div", "dt")

# ── The seller box ──────────────────────────────────────────────────────────────────────────────────────────

_MEMBER_SINCE_RE = re.compile(
    r"(?<![a-z])(?:member since|lid sinds|actief sinds|op (?:marktplaats|2dehands) sinds"
    r"|joined(?: (?:facebook|ebay|vinted|marketplace))?(?: in| on)?|membre depuis|mitglied seit|sinds|since|seit"
    r"|depuis)\s*:?\s*([^·|,()]{0,30}?(?:19|20)\d\d)(?!\d)",
    re.IGNORECASE,
)
_RATING_RE = re.compile(
    r"(?<![\d.,])(\d(?:[.,]\d{1,2})?)\s*(?:/\s*5|out of 5|uit 5|van 5|van de 5|sur 5|von 5|sterren|stars?|★)",
    re.IGNORECASE,
)
_RATING_COUNT_RE = re.compile(
    r"(?<![\d.,])(\d[\d.,]*)\s*(?:reviews?|beoordelingen|recensies|ratings?|evaluaties|évaluations?|bewertungen"
    r"|feedback|waarderingen)(?![a-z])",
    re.IGNORECASE,
)
_NO_RATINGS_RE = re.compile(
    r"no (?:reviews|ratings|feedback)(?: yet)?|nog geen (?:reviews|beoordelingen)|geen (?:reviews|beoordelingen)"
    r"|pas encore d'évaluation|noch keine bewertungen",
    re.IGNORECASE,
)
_POSITIVE_SHARE_RE = re.compile(
    r"(\d{2,3}(?:[.,]\d)?)\s*%\s*(?:positive|positieve|positiv)", re.IGNORECASE
)  # eBay: "99.8% positive feedback"
_FEEDBACK_SCORE_RE = re.compile(r"\((\d[\d.,]*)\)")  # eBay: the feedback score next to the name, "(1,234)"
_BADGES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("Verified ID", r"(?:id|identiteit|identity)\s*(?:geverifieerd|verified)|verified id|identité vérifiée"),
        ("Verified phone", r"(?:telefoon(?:nummer)?|phone(?: number)?)\s*(?:geverifieerd|verified)"),
        ("Verified bank account", r"(?:bank(?:rekening)?|bank account)\s*(?:geverifieerd|verified)"),
        ("Verified e-mail", r"e-?mail(?:adres)?\s*(?:geverifieerd|verified)"),
        ("Top seller", r"top[- ]rated(?: seller| plus)?|top seller|topverkoper|top-verkoper"),
        ("Highly rated", r"highly rated|hoog gewaardeerd"),
    )
)
_VERIFIED_RE = re.compile(r"(?<![a-z])(?<!not )(?<!niet )(?:geverifieerd|verified|vérifié)(?![a-z])", re.IGNORECASE)
_OTHER_LISTINGS_RE = re.compile(
    r"(?<![\d.,])(\d[\d.,]*)\s*(?:andere |other |overige )?(?:advertenties|ads|listings|items(?: for sale)?|artikelen"
    r"|annonces|anzeigen|articles)(?![a-z])",
    re.IGNORECASE,
)
_TIME_UNIT = (
    r"(?:minutes?|minuten|uur|uren|hours?|dagen|dag|days?|weken|week|weeks?|stunden?|tagen?|heures?|jours?)(?![a-z])"
)
_RESPONSE_TIME_RE = re.compile(
    r"(?:(?:typically|usually|meestal)\s+)?(?:responds|replies|reageert|antwoordt|antwortet|répond)"
    r"(?:\s+(?:meestal|usually|typically|normaal))?\s+(?:within|binnen|innerhalb|en|in)\s+"
    rf"(?:(?:an?|een|één|a few|enkele|einer?|\d+)\s+)?{_TIME_UNIT}"
    rf"|(?:reactietijd|response time|antwoordtijd)\s*:?\s*(?:(?:\d+|een|an?)\s+)?{_TIME_UNIT}",
    re.IGNORECASE,
)
_RESPONSE_RATE_RE = re.compile(
    r"(\d{1,3})\s*%\s*(?:response rate|reactiepercentage|antwoordpercentage)"
    r"|(?:response rate|reactiepercentage|antwoordpercentage)\s*:?\s*(\d{1,3})\s*%",
    re.IGNORECASE,
)
_BUSINESS_RE = re.compile(
    r"(?<![a-z])(?:zakelijke verkoper|zakelijk|bedrijf|business seller|registered as a business|professional seller"
    r"|vendeur professionnel|gewerblicher verkäufer|händler)(?![a-z])",
    re.IGNORECASE,
)
_PRIVATE_RE = re.compile(
    r"(?<![a-z])(?:private seller|particuliere verkoper|particulier|vendeur particulier|privater verkäufer"
    r"|privatverkäufer)(?![a-z])",
    re.IGNORECASE,
)

# ── The deal ────────────────────────────────────────────────────────────────────────────────────────────────

_FIXED_RE = re.compile(
    r"vaste prijs|fixed price|prijs is vast|no offers|geen bod|non-negotiable|niet onderhandelbaar|festpreis"
    r"|prix ferme",
    re.IGNORECASE,
)
_NEGOTIABLE_RE = re.compile(
    r"n\.?o\.?t\.?k(?![a-z])|nader overeen te komen|onderhandelbaar|bespreekbaar|or best offer|best offer"
    r"|(?<![a-z])obo(?![a-z])|negotiable|verhandelbar|à négocier|à débattre|make (?:an )?offer|doe een bod"
    r"|faire une offre"
    r"|preisvorschlag",
    re.IGNORECASE,
)
_BIDDING_RE = re.compile(
    r"(?<![a-z])(?:bieden|bod uitbrengen|biedingen|place bid|bids?|enchères?|gebote?|auction|veiling)(?![a-z])",
    re.IGNORECASE,
)
_CONDITION_RE = re.compile(
    r"(?<![a-z])(?:conditie|staat|condition|item condition|zustand|état|toestand)\s*:?\s*"
    r"(nieuw met prijskaartje|nieuw zonder prijskaartje|nieuw met kaartje|nieuw zonder kaartje|zo goed als nieuw"
    r"|nieuw in doos|nieuw|zeer goed|heel goed|gebruikt|goed|redelijk|matig|defect|voor onderdelen"
    r"|new with tags|new without tags|new with box|new other|brand new|used - like new|used - good|used - fair"
    r"|like new|new|open box|excellent|very good|good|fair|poor|used|pre-owned|refurbished"
    r"|for parts or not working|for parts|salvage|neuf|très bon état|bon état|wie neu|neu|gebraucht)(?![a-z])",
    re.IGNORECASE,
)
_LD_CONDITIONS = {
    "newcondition": "New",
    "usedcondition": "Used",
    "refurbishedcondition": "Refurbished",
    "damagedcondition": "Damaged",
}
_LOCATION_LABEL_RE = re.compile(
    r"^(?:located in|item location|locatie|location|standort|ophalen in|plaats|lieu)\s*:?\s*", re.IGNORECASE
)
_LISTED_LINE_RE = re.compile(r"^(?:listed|geplaatst|posted|placed)(?![a-z]).{0,100}$", re.IGNORECASE)
_LISTED_IN_RE = re.compile(r"\s(?:in|te)\s+(.{2,80})$", re.IGNORECASE)
_PICKUP_RE = re.compile(
    r"ophalen|afhalen|pick ?-?up|collection|abholung|abholen|retrait|remise en main propre|meet ?up", re.IGNORECASE
)
_SHIPPING_RE = re.compile(
    r"verzenden|verzending|verzonden|shipping|postage|delivery|bezorg|versand|livraison|envoi"
    r"|(?<![a-z])ships?(?![a-z])",
    re.IGNORECASE,
)
_NO_SHIPPING_RE = re.compile(
    r"local pickup only|pickup only|pick-up only|collection only|alleen ophalen|enkel ophalen|uitsluitend ophalen"
    r"|nur abholung|does not ship|no shipping|geen verzending|niet verzenden|wordt niet verzonden",
    re.IGNORECASE,
)
_NO_PICKUP_RE = re.compile(
    r"shipping only|alleen verzenden|enkel verzenden|uitsluitend verzenden|geen ophalen|niet ophalen|no pickup"
    r"|no local pickup|nur versand|kan niet opgehaald",
    re.IGNORECASE,
)
_PROTECTION_RE = re.compile(
    r"gelijk oversteken|betaal veilig|veilig betalen|kopersbescherming|koperbescherming|buyer protection"
    r"|purchase protection|money back guarantee|geld-terug-garantie|käuferschutz|protection acheteurs?"
    r"|betalen via (?:marktplaats|2dehands)|checkout with facebook|facebook checkout",
    re.IGNORECASE,
)

# ── What the ad text asks for ───────────────────────────────────────────────────────────────────────────────

# A mobile or international number as sellers write them ("06-11223344", "+31 6 1122 3344", "(415) 555-0134").
PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9+])(?:(?:\+|00)\d{2}[\s.-]?(?:\(0\)[\s.-]?)?\d(?:[\s.-]?\d){7,10}"
    r"|0(?:[\s.-]?\d){9}"
    r"|\(?[2-9]\d{2}\)?[\s.-]\d{3}[\s.-]\d{4})(?![0-9])"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_BANK_BEFORE_RE = re.compile(r"iban|rekening|bank|account", re.IGNORECASE)
CONTACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "whatsapp",
            (
                r"whats ?app|(?<![a-z])wa\.me|(?<![a-z])app (?:me|mij|even)(?![a-z])"
                r"|stuur (?:me |mij )?(?:een )?app(?:je)?(?![a-z])|(?<![a-z])appen(?![a-z])"
            ),
        ),
        ("telegram", r"telegram|(?<![a-z])t\.me/"),
        ("phone", r"(?<![a-z])(?:bel (?:me|mij)|call me|text me|sms (?:me|mij)|bellen|ruf (?:mich )?an)(?![a-z])"),
        ("email", r"(?<![a-z])(?:e-?mail (?:me|mij)|mail (?:me|mij)|stuur (?:me |mij )?een (?:e-?)?mail)(?![a-z])"),
    )
)
PAYMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "bank_transfer",
            (
                r"overmaken|overboeken|overboeking|overschrijving|bank ?transfer|wire transfer|(?<![a-z])iban(?![a-z])"
                r"|rekeningnummer|virement|überweisung"
            ),
        ),
        ("payment_link", r"tikkie|betaalverzoek|payment link|betaal ?link"),
        (
            "deposit",
            (
                r"aanbetaling|vooruitbetal|vooraf (?:betalen|overmaken|maken)|(?:betaling|betalen) vooraf"
                r"|eerst (?:betalen|overmaken|betaling)"
                r"|borg (?:vooraf|eerst|overmaken)|deposit|advance payment|pay(?:ment)? (?:up ?front|in advance|first)"
                r"|upfront payment|acompte|anzahlung|vorkasse|reserveringskosten|reservation fee|holding fee"
            ),
        ),
        (
            "gift_card",
            (
                r"gift ?cards?|cadeaukaart|cadeaubon|itunes|google play (?:card|kaart)|steam ?(?:card|kaart)"
                r"|paysafe ?card|amazon (?:card|voucher|gift)"
            ),
        ),
        ("crypto", r"bitcoin|(?<![a-z])(?:btc|usdt|tether)(?![a-z])|crypto|ethereum"),
        ("money_transfer", r"western union|moneygram|(?<![a-z])ria money|worldremit"),
        (
            "payment_app",
            (
                r"(?<![a-z])(?:zelle|venmo|cash ?app)(?![a-z])|paypal (?:vrienden|friends)|friends (?:and|&) family"
                r"|vrienden (?:en|&) familie|(?<![a-z])f&f(?![a-z])"
            ),
        ),
    )
)
# "Never pay in advance", "no bank transfer": a payment the ad rules out isn't one it asks for.
NEGATED_RE = re.compile(
    r"(?:^|[^a-z])(?:nooit|never|niet|not|geen|no|don't|dont|do not|kein|keine|pas de)(?:\s+[^\s.!?]+){0,4}\s*$",
    re.IGNORECASE,
)
_NEGATION_WINDOW = 40
_QR_LABEL_RE = re.compile(r"^QR Code Link to This Post\s*", re.IGNORECASE)  # Craigslist's print label

_MAX_DESCRIPTION = 5_000
_WS_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _text(tag: object) -> str:
    return _squash(tag.get_text(" ")) if isinstance(tag, Tag) else ""


def _labels(tag: Tag) -> list[str]:
    # The rating stars and badges some platforms only name in an aria-label.
    return [str(el.get("aria-label", "")) for el in tag.select("[aria-label]") if el.get("aria-label")]


def _area(soup: BeautifulSoup, selector: str) -> str:
    """Every element the selector matches, its text and aria-labels, joined."""
    if not selector:
        return ""
    parts: list[str] = []
    for tag in soup.select(selector):
        parts.append(_text(tag))
        parts.extend(_labels(tag))
    return _squash(" ".join(parts))


def _heading(soup: BeautifulSoup, headings: tuple[str, ...]) -> Tag | None:
    if not headings:
        return None
    for tag in soup.find_all(_HEADING_TAGS):
        if _text(tag).rstrip(":").strip().lower() in headings:
            return tag
    return None


def _section(soup: BeautifulSoup, headings: tuple[str, ...]) -> str:
    """The text of the section a heading opens (its parent element), or ``""``."""
    tag = _heading(soup, headings)
    if tag is None or not isinstance(tag.parent, Tag):
        return ""
    return _squash(" ".join([_text(tag.parent), *_labels(tag.parent)]))


def _after_heading(soup: BeautifulSoup, headings: tuple[str, ...]) -> str:
    """The text of the element right after a heading, or ``""``."""
    tag = _heading(soup, headings)
    sibling = tag.find_next_sibling() if tag is not None else None
    return _text(sibling)


def negated(text: str, start: int) -> bool:
    """
    Whether the words just before a match rule it out ("never pay in advance", "geen aanbetaling").

    :param text: The text.
    :param start: Where the match starts.
    :return: ``True`` when a negation precedes it.
    """
    return bool(NEGATED_RE.search(text[max(start - _NEGATION_WINDOW, 0) : start]))


def phones_in(text: str) -> list[str]:
    """
    The phone numbers written in an ad, as written (not a bank account number).

    :param text: The ad text.
    :return: The numbers.
    """
    return [
        m.group()
        for m in PHONE_RE.finditer(text)
        if not _BANK_BEFORE_RE.search(text[max(m.start() - 30, 0) : m.start()])
    ]


def ad_signals(text: str) -> tuple[list[str], list[str]]:
    """
    The contact methods and payment requests an ad's text mentions.

    :param text: The ad text.
    :return: ``(contact_methods, payment_requests)``, in a fixed order.
    """
    contacts = [name for name, pattern in CONTACT_PATTERNS if pattern.search(text)]
    if phones_in(text) and "phone" not in contacts:
        contacts.append("phone")
    if EMAIL_RE.search(text) and "email" not in contacts:
        contacts.append("email")
    order = [name for name, _ in CONTACT_PATTERNS]
    contacts.sort(key=order.index)
    payments = [
        name for name, pattern in PAYMENT_PATTERNS if any(not negated(text, m.start()) for m in pattern.finditer(text))
    ]
    return contacts, payments


# ── Extraction ──────────────────────────────────────────────────────────────────────────────────────────────


def extract(soup: BeautifulSoup, facts: PageFacts) -> None:
    """
    Fill ``facts.listing`` and ``facts.seller_profile`` for an ad on a peer-to-peer platform (or eBay).

    Runs after the generic extraction (JSON-LD has given the price, the title and the photos where the platform
    has it). Never reads the seller's name.

    :param soup: The parsed page.
    :param facts: The facts to complete; updated in place.
    """
    areas = AREAS.get(facts.platform)
    if areas is None:
        return
    if areas.title and (title := _text(soup.select_one(areas.title))):
        facts.title = title[:2_000]
    description = _text(soup.select_one(areas.description)) if areas.description else ""
    description = description or _after_heading(soup, areas.description_headings)
    description = _QR_LABEL_RE.sub("", description)[:_MAX_DESCRIPTION]
    seller = _squash(f"{_area(soup, areas.seller)} {_section(soup, areas.seller_headings)}")
    details = _squash(f"{_area(soup, areas.details)} {_section(soup, areas.details_headings)}")
    price_text = _text(soup.select_one(areas.price)) if areas.price else ""
    deal = _squash(f"{price_text} {details} {_area(soup, areas.deal)}")

    if facts.price is None and price_text:
        price, currency = parse_price(price_text)
        facts.price, facts.currency = price, facts.currency or currency
    listed_line = _listed_line(soup)
    contacts, payments = ad_signals(description)
    facts.listing = ListingDetails(
        negotiable=_negotiable(deal),
        bidding=bool(_BIDDING_RE.search(deal)) if deal else None,
        condition=_condition(details, facts),
        category=_category(soup, facts.title),
        listed=_listed(soup, areas.listed) or parse_date(listed_line),
        location=_location(soup, areas.location, listed_line),
        delivery=_delivery(deal),
        payment_protection=True if _PROTECTION_RE.search(deal) else areas.protection,
        contact_methods=contacts,
        payment_requests=payments,
        description=description,
    )
    if seller:
        facts.seller_profile = _seller_profile(seller, facts)
    if facts.platform == "ebay" and (m := _POSITIVE_SHARE_RE.search(seller)):
        share = parse_number(m.group(1))
        facts.seller_rating = share if share is not None and share <= 100 else facts.seller_rating


def _negotiable(text: str) -> bool | None:
    if _FIXED_RE.search(text):
        return False
    return True if _NEGOTIABLE_RE.search(text) else None


def _condition(details: str, facts: PageFacts) -> str:
    if m := _CONDITION_RE.search(details):
        return m.group(1)[:100]
    offers = facts.json_ld[0].get("offers") if facts.json_ld else None
    offer = offers[0] if isinstance(offers, list) and offers else offers
    condition = offer.get("itemCondition") if isinstance(offer, dict) else None
    if isinstance(condition, str):
        return _LD_CONDITIONS.get(condition.rsplit("/", 1)[-1].lower(), "")
    return ""


def _category(soup: BeautifulSoup, title: str) -> str:
    crumbs = [_text(a) for a in soup.select('[class*="readcrumb"] a, nav[aria-label*="readcrumb"] a')]
    kept: list[str] = []
    for crumb in crumbs:
        if crumb and crumb not in kept and crumb.lower() != title.lower():
            kept.append(crumb)
    return " > ".join(kept)[:300]


def _listed(soup: BeautifulSoup, selector: str) -> date | None:
    area = soup.select_one(selector) if selector else None
    if area is None:
        return None
    time = area.find("time")
    if isinstance(time, Tag) and (found := parse_date(str(time.get("datetime", "")))):
        return found
    return parse_date(_text(area))


def _listed_line(soup: BeautifulSoup) -> str:
    # Facebook's "Listed 3 days ago in Utrecht, NL": a short line of its own.
    for tag in soup.find_all(["span", "div", "p"]):
        line = _text(tag)
        if _LISTED_LINE_RE.match(line):
            return line
    return ""


def _location(soup: BeautifulSoup, selector: str, listed_line: str) -> str:
    text = _text(soup.select_one(selector)) if selector else ""
    if not text and (m := _LISTED_IN_RE.search(listed_line)):
        text = m.group(1)
    return _LOCATION_LABEL_RE.sub("", text).strip("()").strip()[:300]


def _delivery(text: str) -> list[Delivery]:
    shipping = bool(_SHIPPING_RE.search(text)) and not _NO_SHIPPING_RE.search(text)
    pickup = bool(_PICKUP_RE.search(text)) and not _NO_PICKUP_RE.search(text)
    kinds: list[Delivery] = ["shipping", "pickup"]
    return [kind for kind, offered in zip(kinds, (shipping, pickup), strict=True) if offered]


def _seller_type(text: str) -> SellerType:
    business, private = bool(_BUSINESS_RE.search(text)), bool(_PRIVATE_RE.search(text))
    if business != private:
        return "business" if business else "private"
    return ""


def _seller_profile(text: str, facts: PageFacts) -> SellerProfile:
    since = _MEMBER_SINCE_RE.search(text)
    rating = _RATING_RE.search(text)
    stars = parse_number(rating.group(1)) if rating else None
    count = _RATING_COUNT_RE.search(text)
    ratings = parse_count(count.group(1)) if count else None
    if ratings is None and _NO_RATINGS_RE.search(text):
        ratings = 0
    if ratings is None and facts.platform == "ebay" and (score := _FEEDBACK_SCORE_RE.search(text)):
        ratings = parse_count(score.group(1))
    badges = [label for label, pattern in _BADGES if pattern.search(text)]
    if not any(b.startswith("Verified") for b in badges) and _VERIFIED_RE.search(text):
        badges.append("Verified")
    listings = _OTHER_LISTINGS_RE.search(text)
    response = _RESPONSE_TIME_RE.search(text)
    rate = _RESPONSE_RATE_RE.search(text)
    rate_value = parse_number(rate.group(1) or rate.group(2)) if rate else None
    return SellerProfile(
        seller_type=_seller_type(text),
        member_since=parse_since(since.group(1)) if since else None,
        rating=stars if stars is not None and stars <= 5 else None,
        rating_count=ratings,
        badges=badges,
        other_listings=parse_count(listings.group(1)) if listings else None,
        response_time=_squash(response.group())[:100] if response else "",
        response_rate=rate_value if rate_value is not None and rate_value <= 100 else None,
    )
