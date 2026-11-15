"""
Synthetic peer-to-peer ads (``p2p_listing``), built from documented scam and normal patterns.

The real platforms can't be collected: eBay's robots.txt forbids automated access outright, Vinted's disallows
every page, and the terms of Marktplaats, 2dehands, Craigslist and Facebook forbid scraping (Facebook also needs a
login). So, as with the Amazon pages, the ads are rendered here (``source="synthetic"``, ``notes.synthetic`` true):
platform-like pages for Marktplaats, 2dehands, eBay (private sellers), Vinted, Craigslist and Facebook Marketplace,
in Dutch or English as the platform's users write them.

- **scam** ads combine two to four scam patterns that public warnings document (:mod:`evals.sources.patterns`:
  Fraudehelpdesk, the Marktplaats safety centre, FTC consumer advice), with the sources cited in the record. A
  quarter come from an older account (hijacked accounts are common), and some have a normal price, so no single
  cue gives them away.
- **legit** ads follow the documented normal patterns: pickup or the platform's payment, a seller with a history,
  a plausible used price, specific details including flaws. Some are from new sellers.

Ads never carry a seller's name (the PageFacts privacy rule for private sellers): the seller box shows only what a
platform shows about an account. Phone numbers in scam ads are made up. Labels are rule labels (``label_source=
"rule"``) until the second opinion agrees (:mod:`evals.second_opinion`); ``rule_features`` lists the patterns used.

Usage: ``uv run python -m evals.sources.p2p_ads [--per-label 150] [--seed 11]``.
"""

import argparse
import html
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.sources.patterns import confirmed_patterns

logger = structlog.get_logger("unfret.evals")

DATASET = "p2p-synthetic-2026"


@dataclass(frozen=True)
class PlatformStyle:
    """How one platform's ad pages look."""

    key: str
    name: str
    lang: str
    currency: str
    host: str
    region: str


PLATFORMS: tuple[PlatformStyle, ...] = (
    PlatformStyle("marktplaats", "Marktplaats", "nl", "EUR", "www.marktplaats.nl", "nl"),
    PlatformStyle("2dehands", "2dehands", "nl", "EUR", "www.2dehands.be", "be"),
    PlatformStyle("vinted", "Vinted", "nl", "EUR", "www.vinted.nl", "nl"),
    PlatformStyle("ebay", "eBay", "en", "USD", "www.ebay.com", "us"),
    PlatformStyle("craigslist", "Craigslist", "en", "USD", "craigslist.org", "us"),
    PlatformStyle("facebook", "Facebook Marketplace", "en", "USD", "www.facebook.com", "us"),
)
# How often each platform is picked (Dutch-language platforms first: Unfret's audience).
_PLATFORM_WEIGHTS = (30, 12, 12, 16, 15, 15)


@dataclass(frozen=True)
class Item:
    """Something people sell second hand, with its price new."""

    key: str
    nl: str
    en: str
    category_nl: str
    category_en: str
    new_price: float
    desirable: bool = True
    rental: bool = False
    clothing: bool = False
    details_nl: tuple[str, ...] = ()
    details_en: tuple[str, ...] = ()


ITEMS: tuple[Item, ...] = (
    Item(
        "iphone",
        "iPhone 14 Pro 128GB spacegrijs",
        "iPhone 14 Pro 128GB Space Black",
        "Telefoons",
        "Cell phones",
        900,
        details_nl=("Batterijconditie 88%.", "Altijd in hoesje gezeten met screenprotector."),
        details_en=("Battery health 88%.", "Always had a case and screen protector on it."),
    ),
    Item(
        "ps5",
        "PlayStation 5 disc edition met 2 controllers",
        "PlayStation 5 Disc Edition with 2 controllers",
        "Spelcomputers",
        "Video gaming",
        520,
        details_nl=("Inclusief alle kabels.", "Firmware is up-to-date."),
        details_en=("All cables included.", "Firmware is up to date."),
    ),
    Item(
        "switch",
        "Nintendo Switch OLED wit + 3 games",
        "Nintendo Switch OLED white + 3 games",
        "Spelcomputers",
        "Video gaming",
        380,
        details_nl=("Games: Mario Kart 8, Zelda BOTW, Animal Crossing.",),
        details_en=("Games: Mario Kart 8, Zelda BOTW, Animal Crossing.",),
    ),
    Item(
        "macbook",
        "MacBook Air M2 13 inch 256GB",
        "MacBook Air M2 13-inch 256GB",
        "Laptops",
        "Computers",
        1200,
        details_nl=("Laadcycli: 142.", "Originele doos en lader aanwezig."),
        details_en=("Battery cycle count: 142.", "Original box and charger included."),
    ),
    Item(
        "camera",
        "Canon EOS R6 body",
        "Canon EOS R6 body",
        "Fotografie",
        "Cameras",
        1900,
        details_nl=("Ongeveer 18.000 clicks.", "Twee accu's en lader erbij."),
        details_en=("About 18,000 shutter count.", "Comes with two batteries and the charger."),
    ),
    Item(
        "dyson",
        "Dyson V15 Detect steelstofzuiger",
        "Dyson V15 Detect cordless vacuum",
        "Huishouden",
        "Appliances",
        650,
        details_nl=("Alle opzetstukken aanwezig.",),
        details_en=("All attachments included.",),
    ),
    Item(
        "ebike",
        "Gazelle Ultimate C380 e-bike 2022",
        "Trek Verve+ 2 e-bike 2022",
        "Fietsen",
        "Bicycles",
        3200,
        details_nl=("Accu 500Wh, ca. 3.100 km gereden.", "Onderhoudsboekje aanwezig."),
        details_en=("500Wh battery, about 1,900 miles.", "Serviced last spring."),
    ),
    Item(
        "stroller",
        "Bugaboo Fox 3 kinderwagen compleet",
        "Bugaboo Fox 3 stroller complete",
        "Kinderwagens",
        "Baby & kids",
        1100,
        details_nl=("Wieg en zitje, regenhoes en voetenzak.",),
        details_en=("Bassinet and seat, rain cover and footmuff.",),
    ),
    Item(
        "headphones",
        "Sony WH-1000XM5 koptelefoon",
        "Sony WH-1000XM5 headphones",
        "Audio",
        "Electronics",
        380,
        details_nl=("Met etui en kabel.",),
        details_en=("With carry case and cable.",),
    ),
    Item(
        "tv",
        "Samsung 55 inch QLED tv",
        'Samsung 55" QLED TV',
        "Televisies",
        "Electronics",
        800,
        details_nl=("Afstandsbediening en voet aanwezig.",),
        details_en=("Remote and stand included.",),
    ),
    Item(
        "lego",
        "LEGO Star Wars Millennium Falcon 75257",
        "LEGO Star Wars Millennium Falcon 75257",
        "Speelgoed",
        "Toys",
        170,
        details_nl=("Compleet met boekjes, één keer gebouwd.",),
        details_en=("Complete with instructions, built once.",),
    ),
    Item(
        "sofa",
        "Hoekbank grijs stof 3-zits",
        "Gray fabric sectional sofa",
        "Meubels",
        "Furniture",
        1300,
        False,
        details_nl=("Afmeting 260 x 180 cm.", "Rookvrij huis, geen huisdieren."),
        details_en=("Size 102 x 71 in.", "Smoke-free home, no pets."),
    ),
    Item(
        "kidsbike",
        "Kinderfiets 20 inch Batavus",
        "Kids bike 20 inch Schwinn",
        "Fietsen",
        "Bicycles",
        280,
        False,
        details_nl=("Banden vorig jaar vervangen.",),
        details_en=("Tires replaced last year.",),
    ),
    Item(
        "table",
        "Eikenhouten eettafel 200 cm",
        'Solid oak dining table 78"',
        "Meubels",
        "Furniture",
        900,
        False,
        details_nl=("Wat gebruikssporen op het blad.",),
        details_en=("Some wear marks on the top.",),
    ),
    Item(
        "puppy",
        "Franse bulldog pups, ingeënt",
        "French Bulldog puppies, vaccinated",
        "Dieren",
        "Pets",
        2500,
        details_nl=("Ingeënt en ontwormd, met chip.",),
        details_en=("Vaccinated and dewormed, microchipped.",),
    ),
    Item(
        "firewood",
        "Haardhout eiken, per m3",
        "Seasoned oak firewood, per cord",
        "Tuin",
        "Garden",
        120,
        False,
        details_nl=("Gedroogd, klaar om te stoken.",),
        details_en=("Seasoned and ready to burn.",),
    ),
    Item(
        "jacket",
        "Canada Goose winterjas maat M",
        "Canada Goose parka size M",
        "Kleding",
        "Clothing",
        1100,
        clothing=True,
        details_nl=("Twee winters gedragen.",),
        details_en=("Worn two winters.",),
    ),
    Item(
        "sneakers",
        "Nike Dunk Low Panda maat 42",
        "Nike Dunk Low Panda size 9",
        "Schoenen",
        "Shoes",
        120,
        clothing=True,
        details_nl=("Paar keer gedragen.",),
        details_en=("Worn a few times.",),
    ),
    Item(
        "bag",
        "Longchamp Le Pliage tas groot",
        "Longchamp Le Pliage large tote",
        "Tassen",
        "Bags",
        140,
        clothing=True,
        details_nl=("Kleine vlek aan de binnenkant.",),
        details_en=("Small mark inside.",),
    ),
    Item(
        "dress",
        "Zara jurk maat S, nieuw met label",
        "Reformation midi dress size 4",
        "Kleding",
        "Clothing",
        60,
        False,
        clothing=True,
        details_nl=("Nooit gedragen.",),
        details_en=("Never worn.",),
    ),
    Item(
        "room",
        "Kamer 18 m2 in gedeeld appartement",
        "Room in shared 3BR apartment",
        "Huizen en kamers",
        "Rooms & shares",
        750,
        rental=True,
        details_nl=("Gedeelde keuken en badkamer.",),
        details_en=("Shared kitchen and bath.",),
    ),
    Item(
        "apartment",
        "2-kamer appartement, gemeubileerd",
        "2BR apartment, furnished",
        "Huizen en kamers",
        "Apartments / housing for rent",
        1600,
        rental=True,
        details_nl=("Balkon op het zuiden.",),
        details_en=("South-facing balcony.",),
    ),
)

CITIES: dict[str, tuple[str, ...]] = {
    "nl": (
        "Amsterdam",
        "Utrecht",
        "Rotterdam",
        "Den Haag",
        "Eindhoven",
        "Groningen",
        "Nijmegen",
        "Haarlem",
        "Amersfoort",
        "Zwolle",
        "Leiden",
        "Tilburg",
        "Breda",
        "Arnhem",
        "Enschede",
        "Delft",
    ),
    "be": ("Antwerpen", "Gent", "Brugge", "Leuven", "Mechelen", "Hasselt", "Kortrijk", "Aalst"),
    "us": (
        "Seattle",
        "Portland",
        "Austin",
        "Denver",
        "Chicago",
        "Brooklyn",
        "Phoenix",
        "San Diego",
        "Atlanta",
        "Minneapolis",
        "Columbus",
        "Raleigh",
        "Sacramento",
        "Pittsburgh",
    ),
}
# Short names Craigslist uses in its subdomains.
_CL_SITES = {
    "Seattle": "seattle",
    "Portland": "portland",
    "Austin": "austin",
    "Denver": "denver",
    "Chicago": "chicago",
    "Brooklyn": "newyork",
    "Phoenix": "phoenix",
    "San Diego": "sandiego",
    "Atlanta": "atlanta",
    "Minneapolis": "minneapolis",
    "Columbus": "columbus",
    "Raleigh": "raleigh",
    "Sacramento": "sacramento",
    "Pittsburgh": "pittsburgh",
}

# ── ad text ─────────────────────────────────────────────────────────────────

GOOD_TEXT: dict[str, dict[str, tuple[str, ...]]] = {
    "nl": {
        "intro": (
            "Te koop: {item}.",
            "Ik verkoop mijn {item}.",
            "{item} in goede staat.",
            "Wegens verhuizing te koop: {item}.",
        ),
        "history": (
            "Gekocht in 2022 bij Coolblue, bon is er nog.",
            "Ongeveer twee jaar oud.",
            "Gekocht in 2021, altijd goed onderhouden.",
            "Cadeau gekregen, weinig gebruikt.",
        ),
        "flaw": (
            "Kleine kras aan de zijkant, zie foto 3.",
            "Wat gebruikssporen, werkt perfect.",
            "Doos is wat beschadigd.",
            "Eén klein deukje, verder netjes.",
        ),
        "reason": (
            "Verkoop omdat ik een nieuwe heb.",
            "Wordt te weinig gebruikt.",
            "Past niet meer in ons huis.",
            "Overgestapt naar iets anders.",
        ),
        "logistics": (
            "Ophalen in {city}, of verzenden via {platform} (koper betaalt verzendkosten).",
            "Ophalen in {city}. Bekijken mag altijd, even een bericht sturen.",
            "Kan bekeken en getest worden bij ophalen in {city}.",
        ),
        "payment": (
            "Betalen contant bij ophalen of veilig via {platform}.",
            "Betalen en verzenden via {platform} kan ook, dan ben je beschermd.",
            "Contant bij ophalen of via de betaalknop van {platform}.",
        ),
        "closing": (
            "Vragen? Stuur gerust een bericht.",
            "Bieden vanaf {floor}.",
            "Prijs is iets bespreekbaar.",
            "Geen ruilen.",
        ),
    },
    "en": {
        "intro": (
            "Selling my {item}.",
            "For sale: {item}.",
            "{item} in good condition.",
            "Moving, so selling my {item}.",
        ),
        "history": (
            "Bought in 2022 at Best Buy, have the receipt.",
            "About two years old.",
            "Bought new in 2021, well looked after.",
            "Was a gift, barely used.",
        ),
        "flaw": (
            "Small scratch on the side, see photo 3.",
            "Some signs of use, works perfectly.",
            "Box is a bit beat up.",
            "One small dent, otherwise clean.",
        ),
        "reason": (
            "Upgraded to a newer one.",
            "Don't use it enough.",
            "Doesn't fit our new place.",
            "Switched to something else.",
        ),
        "logistics": (
            "Pickup in {city}, happy to meet at a public place.",
            "Local pickup in {city}. You're welcome to look at it and test it first.",
            "Can meet in person in {city} so you can check it out.",
        ),
        "payment": (
            "Cash on pickup.",
            "Cash in person, or pay through {platform} checkout.",
            "Cash or card through {platform}, in person only.",
        ),
        "closing": ("Message me with questions.", "Firm on price.", "Reasonable offers considered.", "No trades."),
    },
}

SCAM_TEXT: dict[str, dict[str, tuple[str, ...]]] = {
    "nl": {
        "intro": (
            "Nieuw in doos, ongeopend: {item}.",
            "{item}, zo goed als nieuw!",
            "Te koop {item}, top staat.",
            "{item} — snel weg.",
        ),
        "off_platform_contact": (
            "Reageer liever niet via de berichten, stuur een WhatsApp naar {phone}, daar ben ik sneller.",
            "Ik ben niet vaak op {platform}, app me op {phone}.",
            "Contact alleen via WhatsApp: {phone}.",
        ),
        "seller_payment_link": (
            "Na akkoord stuur ik je een betaalverzoek via mijn eigen link, dan gaat het pakket dezelfde dag weg.",
            "Je ontvangt van mij een betaallink, na betaling verzend ik direct.",
            "Betalen via een betaalverzoek dat ik je stuur (Tikkie of link).",
        ),
        "untraceable_prepayment": (
            "Betaling vooraf via overboeking, daarna verzend ik.",
            "Ik accepteer alleen betaling vooraf of met PaysafeCard.",
            "Eerst overmaken, dan verzenden. Geen ophalen.",
        ),
        "too_good_price": (
            "Moet snel weg, daarom deze prijs.",
            "Spotprijs, ik heb het geld nodig.",
            "Prijs is laag omdat ik snel wil verkopen.",
        ),
        "pressure": (
            "Veel interesse, wie het eerst betaalt krijgt hem.",
            "Vandaag nog beslissen, morgen is hij weg.",
            "Reserveren kan niet, eerst betalen.",
        ),
        "courier_only": (
            "Ik werk tijdelijk in het buitenland, dus alleen verzending via mijn koerier.",
            "Ophalen is niet mogelijk, ik laat het bezorgen door een koeriersdienst.",
            "Alleen verzenden met koerier, ik zit momenteel in Duitsland.",
        ),
        "rental_no_viewing": (
            (
                "Ik woon momenteel in Spanje voor mijn werk, dus bezichtigen kan helaas niet. Na betaling van de "
                "borg (twee maanden) en de eerste maand huur stuur ik de sleutels per post."
            ),
            (
                "Bezichtiging is niet mogelijk omdat ik in het buitenland zit. Maak de borg over, dan krijg je het "
                "contract en de sleutels binnen een paar dagen."
            ),
        ),
        "copied_listing": (
            "Deze prachtige woning wordt aangeboden in nieuwstaat. Foto's zijn ter illustratie.",
            "Luxe afgewerkt, zie foto's. Neem voor meer informatie direct contact met mij op, niet via de makelaar.",
        ),
    },
    "en": {
        "intro": (
            "Brand new in sealed box: {item}.",
            "{item}, like new!",
            "{item} for sale, great condition.",
            "{item} — must go.",
        ),
        "off_platform_contact": (
            "Please text me at {phone}, I don't check messages here.",
            "Email only, I'm rarely on {platform}: {email}.",
            "Contact me on WhatsApp {phone}.",
        ),
        "seller_payment_link": (
            "I'll send you a payment link, item ships same day after payment.",
            "Pay through the invoice link I'll email you.",
            "I'll send a PayPal friends & family request.",
        ),
        "untraceable_prepayment": (
            "Payment upfront via Zelle, Cash App or gift cards only.",
            "Pay by wire transfer first, then I ship.",
            "Prepayment only, crypto or gift cards accepted.",
        ),
        "too_good_price": (
            "Priced to sell fast.",
            "Need the money, that's why it's cheap.",
            "Low price because I'm moving overseas.",
        ),
        "pressure": (
            "Lots of interest, first to pay gets it.",
            "Need to sell today.",
            "No holds, payment secures it.",
        ),
        "courier_only": (
            "I'm currently overseas for work so shipping only through my courier.",
            "No local pickup, my shipping agent will deliver.",
            "Shipping only, I'm deployed right now.",
        ),
        "rental_no_viewing": (
            (
                "I'm out of the country for work so I can't show the place. Send the deposit and first month's "
                "rent and I'll mail you the keys."
            ),
            "Viewing isn't possible right now. Once the deposit is wired the keys will be sent to you right away.",
        ),
        "copied_listing": (
            "Stunning fully renovated home, see photos. Do not contact the agency, deal directly with me.",
            "Photos are for illustration. Contact the owner directly by email.",
        ),
    },
}
_NOT_GOODS = frozenset({"puppy", "firewood"})
_RENTAL_ONLY = frozenset({"rental_no_viewing", "copied_listing"})
_SCAM_PATTERNS = (
    "off_platform_contact",
    "seller_payment_link",
    "untraceable_prepayment",
    "too_good_price",
    "pressure",
    "courier_only",
    "rental_no_viewing",
    "copied_listing",
)


@dataclass
class Ad:
    """One generated ad and the facts it was built from."""

    platform: PlatformStyle
    item: Item
    label: str
    title: str
    price: float
    city: str
    text: list[str]
    patterns: list[str]
    member_since: str
    reviews: int
    rating: float
    verified: list[str]
    other_listings: int
    posted: str
    listing_id: str
    url: str
    attributes: dict[str, str] = field(default_factory=dict)


def _phone(rng: random.Random, region: str) -> str:
    if region == "us":
        return f"({rng.randint(200, 989)}) 555-01{rng.randint(10, 99)}"
    return f"06-{rng.randint(10_000_000, 99_999_999)}"


def _slug(text: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in text)
    return "-".join(part for part in keep.split("-") if part)[:60]


def _url(p: PlatformStyle, item: Item, city: str, listing_id: str, title: str) -> str:
    slug = _slug(title)
    if p.key in {"marktplaats", "2dehands"}:
        return f"https://{p.host}/v/{_slug(item.category_nl)}/{item.key}/m{listing_id}-{slug}"
    if p.key == "vinted":
        return f"https://{p.host}/items/{listing_id}-{slug}"
    if p.key == "ebay":
        return f"https://{p.host}/itm/{listing_id}"
    if p.key == "craigslist":
        section = "apa" if item.rental else "sss"
        return f"https://{_CL_SITES.get(city, 'sfbay')}.craigslist.org/{section}/d/{slug}/{listing_id}.html"
    return f"https://{p.host}/marketplace/item/{listing_id}/"


def _price(rng: random.Random, item: Item, label: str, patterns: list[str]) -> float:
    if item.rental:
        share = rng.uniform(0.45, 0.65) if "too_good_price" in patterns else rng.uniform(0.9, 1.15)
    elif "too_good_price" in patterns:
        share = rng.uniform(0.12, 0.3)
    elif label == "scam":
        share = rng.uniform(0.4, 0.7)
    else:
        share = rng.uniform(0.5, 0.8) if item.desirable else rng.uniform(0.3, 0.6)
    value = item.new_price * share
    step = 5 if value < 200 else 25 if value < 1000 else 50
    return float(max(step, round(value / step) * step))


def _seller(rng: random.Random, label: str, lang: str) -> tuple[str, int, float, list[str], int]:
    established = rng.random() < (0.25 if label == "scam" else 0.8)
    if established:
        since = str(rng.randint(2009, 2022))
        reviews = rng.randint(8, 650)
        rating = round(rng.uniform(4.5, 5.0), 1)
        verified = rng.sample(["phone", "email", "id", "bank"], rng.randint(1, 3))
        others = rng.randint(0, 25)
    else:
        since = ("deze maand" if lang == "nl" else "this month") if rng.random() < 0.6 else "2026"
        reviews = 0 if label == "scam" or rng.random() < 0.5 else rng.randint(1, 4)
        rating = round(rng.uniform(4.0, 5.0), 1) if reviews else 0.0
        verified = [] if label == "scam" else rng.sample(["phone", "email"], 1)
        others = rng.randint(0, 3) if label == "legit" else rng.choice((0, 0, 6, 11))
    return since, reviews, rating, verified, others


def _pick_item(rng: random.Random, platform: PlatformStyle, label: str) -> Item:
    if platform.key == "vinted":
        items = [i for i in ITEMS if i.clothing]
    elif platform.key == "ebay":
        items = [i for i in ITEMS if not i.rental and i.key not in {"puppy", "firewood", "sofa", "table"}]
    else:
        items = [i for i in ITEMS if not (i.clothing and platform.key == "craigslist")]
    item = rng.choice(items)
    if label == "scam" and not item.desirable and rng.random() < 0.5:
        item = rng.choice([i for i in items if i.desirable] or items)
    return item


def _scam_patterns(rng: random.Random, platform: PlatformStyle, item: Item, scam_keys: list[str]) -> list[str]:
    allowed = [k for k in _SCAM_PATTERNS if k in scam_keys and (item.rental or k not in _RENTAL_ONLY)]
    patterns = ["rental_no_viewing"] if item.rental and "rental_no_viewing" in allowed else []
    if platform.key in {"vinted", "ebay"}:
        allowed = [k for k in allowed if k != "courier_only"] or allowed
    rest = [k for k in allowed if k not in patterns]
    return patterns + rng.sample(rest, min(len(rest), rng.randint(2, 3)))


RENTAL_TEXT: dict[str, dict[str, tuple[str, ...]]] = {
    "nl": {
        "intro": ("Te huur: {item} in {city}.", "Per direct te huur in {city}: {item}.", "{item} te huur, {city}."),
        "scam_intro": ("Te huur: {item} in {city}, direct beschikbaar!", "Mooie {item} in {city}, snel reageren!"),
        "terms": (
            "Huurcontract voor onbepaalde tijd, borg één maand, te betalen bij het tekenen na de bezichtiging.",
            "Huur inclusief servicekosten, exclusief energie. Borg één maand bij het tekenen van het contract.",
        ),
        "viewing": (
            "Bezichtiging op afspraak, stuur een bericht met wat over jezelf.",
            "Kom gerust kijken, ik woon in hetzelfde gebouw. Bezichtigingen op zaterdag.",
        ),
        "closing": ("Huurtoeslag is mogelijk.", "Geen huisdieren, niet roken.", "Liefst een werkende huurder."),
    },
    "en": {
        "intro": ("For rent: {item} in {city}.", "{item} available in {city}.", "{item} for rent, {city}."),
        "scam_intro": ("For rent: {item} in {city}, available now!", "Beautiful {item} in {city}, won't last!"),
        "terms": (
            "12-month lease, deposit equal to one month's rent, due at lease signing after a viewing.",
            "Rent includes water and trash; tenant pays electric. Application through the property manager.",
        ),
        "viewing": (
            "Showings by appointment, message me to set up a time.",
            "Open house Saturday 11-1, or message for a private showing.",
        ),
        "closing": ("No smoking.", "Small pets considered.", "Credit and background check required."),
    },
}


def _rental_text(rng: random.Random, lang: str, item: Item, label: str, patterns: list[str]) -> list[str]:
    pool = RENTAL_TEXT[lang]
    details = item.details_nl if lang == "nl" else item.details_en
    if label == "scam":
        return [
            rng.choice(pool["scam_intro"]),
            rng.choice(details),
            *(rng.choice(SCAM_TEXT[lang][k]) for k in patterns),
        ]
    text = [rng.choice(pool["intro"]), rng.choice(details), rng.choice(pool["terms"]), rng.choice(pool["viewing"])]
    if rng.random() < 0.6:
        text.append(rng.choice(pool["closing"]))
    return text


def _ad_text(rng: random.Random, lang: str, item: Item, label: str, patterns: list[str]) -> list[str]:
    if item.rental:
        return _rental_text(rng, lang, item, label, patterns)
    details = item.details_nl if lang == "nl" else item.details_en
    if label == "scam":
        scam = SCAM_TEXT[lang]
        text = [rng.choice(scam["intro"])]
        if rng.random() < 0.6:
            text.append(rng.choice(details))
        return text + [rng.choice(scam[k]) for k in patterns]
    good = GOOD_TEXT[lang]
    text = [rng.choice(good["intro"]), rng.choice(details)]
    if item.key not in _NOT_GOODS:  # "bought at a shop, small scratch" makes no sense for puppies or firewood
        text += [rng.choice(good[part]) for part in ("history", "flaw", "reason") if rng.random() < 0.75]
    text += [rng.choice(good["logistics"]), rng.choice(good["payment"])]
    if rng.random() < 0.5:
        text.append(rng.choice(good["closing"]))
    return text


def _listing_id(rng: random.Random, platform: PlatformStyle) -> str:
    if platform.key == "facebook":
        return str(rng.randint(10**14, 10**15))
    if platform.key == "ebay":
        return str(rng.randint(10**11, 10**12 - 1))
    return str(rng.randint(10**9, 10**10 - 1))


def make_ad(rng: random.Random, label: str, scam_keys: list[str]) -> Ad:
    """
    Generate one ad.

    :param rng: The random source (seeded by the caller).
    :param label: ``scam`` or ``legit``.
    :param scam_keys: The scam patterns that a fetched source confirmed (the only ones a scam ad may use).
    :return: The ad.
    """
    platform = rng.choices(PLATFORMS, weights=_PLATFORM_WEIGHTS)[0]
    lang = platform.lang
    item = _pick_item(rng, platform, label)
    city = rng.choice(CITIES[platform.region])
    name = item.nl if lang == "nl" else item.en
    patterns = _scam_patterns(rng, platform, item, scam_keys) if label == "scam" else []
    price = _price(rng, item, label, patterns)
    fill = {
        "item": name,
        "city": city,
        "platform": platform.name,
        "phone": _phone(rng, platform.region),
        "email": f"{rng.choice(['deals', 'sale', 'shop', 'owner'])}{rng.randint(10, 999)}@gmail.com",
        "floor": f"{'€' if platform.currency == 'EUR' else '$'}{int(price * 0.85)}",
    }
    text = [t.format(**fill) for t in _ad_text(rng, lang, item, label, patterns)]
    if label != "scam":
        patterns = ["meet_in_person", "platform_payment", "seller_history"]
    since, reviews, rating, verified, others = _seller(rng, label, lang)
    listing_id = _listing_id(rng, platform)
    title = name if rng.random() < 0.7 else f"{name} - {city}"
    condition_nl = ("Zo goed als nieuw", "Gebruikt", "Nieuw") if label == "scam" else ("Gebruikt", "Zo goed als nieuw")
    condition_en = ("Like new", "Used", "New") if label == "scam" else ("Used", "Like new")
    attributes = {
        ("Conditie" if lang == "nl" else "Condition"): rng.choice(condition_nl if lang == "nl" else condition_en),
        ("Categorie" if lang == "nl" else "Category"): item.category_nl if lang == "nl" else item.category_en,
    }
    return Ad(
        platform=platform,
        item=item,
        label=label,
        title=title,
        price=price,
        city=city,
        text=text,
        patterns=patterns,
        member_since=since,
        reviews=reviews,
        rating=rating,
        verified=verified,
        other_listings=others,
        posted=f"{rng.randint(1, 28):02d}-{rng.randint(1, 11):02d}-2026",
        listing_id=listing_id,
        url=_url(platform, item, city, listing_id, title),
        attributes=attributes,
    )


# ── rendering ───────────────────────────────────────────────────────────────

_LABELS = {
    "nl": {
        "since": "Op {platform} sinds {since}",
        "since_new": "Nieuw op {platform} ({since})",
        "reviews": "{rating} ★ ({n} beoordelingen)",
        "no_reviews": "Nog geen beoordelingen",
        "verified": {
            "phone": "Telefoonnummer geverifieerd",
            "email": "E-mailadres geverifieerd",
            "id": "Identiteit geverifieerd",
            "bank": "Bankrekening geverifieerd",
        },
        "others": "{n} andere advertenties",
        "posted": "Geplaatst op {date}",
        "seller": "Verkoper",
        "description": "Beschrijving",
        "delivery": "Levering",
        "delivery_values": ("Ophalen of verzenden", "Ophalen", "Verzenden"),
        "safety": "Veilig handelen: blijf in Berichten en betaal via {platform}. {platform} vraagt nooit om je "
        "bankgegevens via WhatsApp of e-mail.",
        "message": "Bericht sturen",
        "bid": "Bieden",
    },
    "en": {
        "since": "Joined {platform} in {since}",
        "since_new": "Joined {platform} {since}",
        "reviews": "{rating} ★ ({n} ratings)",
        "no_reviews": "No ratings yet",
        "verified": {
            "phone": "Phone verified",
            "email": "Email verified",
            "id": "Identity verified",
            "bank": "Payment method verified",
        },
        "others": "{n} other listings",
        "posted": "Listed {date}",
        "seller": "Seller information",
        "description": "Description",
        "delivery": "Delivery",
        "delivery_values": ("Local pickup or shipping", "Local pickup", "Shipping"),
        "safety": "Stay safe: meet in a public place, inspect the item before paying, and never pay with gift "
        "cards or wire transfers.",
        "message": "Message seller",
        "bid": "Make offer",
    },
}


def _delivery(ad: Ad, labels: Mapping[str, object]) -> str:
    values: tuple[str, str, str] = labels["delivery_values"]  # type: ignore[assignment]
    if ad.label == "scam" and {"courier_only", "untraceable_prepayment"} & set(ad.patterns):
        return values[2]
    if ad.item.rental:
        return "—"
    return values[0] if ad.platform.key not in {"craigslist", "facebook"} else values[1]


def render(ad: Ad) -> str:
    """
    Render an ad as a platform-like HTML page (no seller name; canonical URL on the platform's host).

    :param ad: The ad.
    :return: The HTML.
    """
    p = ad.platform
    lang = p.lang
    labels = _LABELS[lang]
    symbol = "€" if p.currency == "EUR" else "$"
    price_text = f"{symbol} {ad.price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if p.currency == "USD":
        price_text = f"${ad.price:,.0f}" + ("/mo" if ad.item.rental else "")
    elif ad.item.rental:
        price_text += " p/m"
    verified: dict[str, str] = labels["verified"]  # type: ignore[assignment]
    reviews = (
        str(labels["reviews"]).format(rating=ad.rating, n=ad.reviews) if ad.reviews else str(labels["no_reviews"])
    )
    seller_lines = [
        str(labels["since"] if ad.member_since.isdigit() else labels["since_new"]).format(
            platform=p.name, since=ad.member_since
        ),
        reviews,
        *(verified[v] for v in ad.verified),
        str(labels["others"]).format(n=ad.other_listings),
    ]
    if p.key == "craigslist":  # Craigslist shows no seller profile at all
        seller_lines = []
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": ad.title,
        "description": " ".join(ad.text)[:300],
        "offers": {
            "@type": "Offer",
            "price": f"{ad.price:.2f}",
            "priceCurrency": p.currency,
            "itemCondition": "https://schema.org/UsedCondition",
        },
    }
    e = html.escape
    attrs = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in ad.attributes.items())
    attrs += f"<tr><th>{e(str(labels['delivery']))}</th><td>{e(_delivery(ad, labels))}</td></tr>"
    seller = "".join(f"<li>{e(line)}</li>" for line in seller_lines)
    seller_box = (
        f'<section class="seller"><h2>{e(str(labels["seller"]))}</h2><ul>{seller}</ul>'
        f"<button>{e(str(labels['message']))}</button></section>"
        if seller_lines
        else ""
    )
    photos = "".join(
        f'<img src="https://images.{p.host.removeprefix("www.")}/{ad.listing_id}/{n}.jpg" alt="{e(ad.title)} {n}">'
        for n in range(1, 4)
    )
    paragraphs = "".join(f"<p>{e(t)}</p>" for t in ad.text)
    return f"""<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<title>{e(ad.title)} | {e(p.name)}</title>
<link rel="canonical" href="{e(ad.url)}">
<meta property="og:site_name" content="{e(p.name)}">
<meta property="og:title" content="{e(ad.title)}">
<meta property="og:price:amount" content="{ad.price:.2f}">
<meta property="og:price:currency" content="{p.currency}">
<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>
</head><body>
<header><a href="https://{p.host}/">{e(p.name)}</a></header>
<nav class="breadcrumbs">{e(ad.attributes.get("Categorie") or ad.attributes.get("Category", ""))} › {e(ad.city)}</nav>
<main>
<h1>{e(ad.title)}</h1>
<div class="price">{e(price_text)}</div>
<div class="meta">{e(ad.city)} · {e(str(labels["posted"]).format(date=ad.posted))}</div>
<div class="gallery">{photos}</div>
<section class="description"><h2>{e(str(labels["description"]))}</h2>{paragraphs}</section>
<table class="attributes">{attrs}</table>
{seller_box}
<button>{e(str(labels["bid"]))}</button>
</main>
<footer><p>{e(str(labels["safety"]).format(platform=p.name))}</p></footer>
</body></html>
"""


def record_for(ad: Ad, sources: dict[str, list[str]]) -> LabelRecord:
    """
    The corpus record of a generated ad.

    :param ad: The ad.
    :param sources: Confirmed pattern sources (:func:`evals.sources.patterns.confirmed_patterns`).
    :return: The record (a rule label, until the second opinion agrees).
    """
    cited = sorted({u for k in ad.patterns for u in sources.get(k, [])})
    notes = {
        "synthetic": True,
        "generator": "p2p_ads",
        "platform": ad.platform.key,
        "item": ad.item.key,
        "patterns": ad.patterns,
        "sources": cited,
        "seller_established": ad.reviews >= 8,
    }
    return LabelRecord(
        id=page_id("p2p", ad.url),
        label=ad.label,  # type: ignore[arg-type]
        label_source="rule",
        prior_label=ad.label,  # type: ignore[arg-type]
        confidence=0.7,
        platform=ad.platform.name,
        page_type="p2p_listing",
        source="synthetic",
        origin_dataset=DATASET,
        origin_url=ad.url,
        origin_id=ad.listing_id,
        collected_at="2026",
        notes=json.dumps(notes, sort_keys=True),
        rule_features=list(ad.patterns),
    )


def build(corpus: Corpus, per_label: int, seed: int) -> int:
    """
    Generate ``per_label`` scam and legit ads and add them to the corpus.

    :param corpus: The corpus.
    :param per_label: Ads per label.
    :param seed: Random seed (the same seed gives the same ads).
    :return: The number of ads stored.
    :raises SystemExit: When no scam pattern has a confirmed source (run ``evals.sources.patterns`` first).
    """
    sources = confirmed_patterns(corpus, "p2p")
    scam_keys = [k for k in _SCAM_PATTERNS if k in sources]
    if len(scam_keys) < 3:
        raise SystemExit("Too few confirmed scam patterns; run `python -m evals.sources.patterns` first.")
    rng = random.Random(seed)
    items: list[tuple[LabelRecord, str]] = []
    seen: set[str] = set()
    for label in ("scam", "legit"):
        while sum(1 for r, _ in items if r.label == label) < per_label:
            ad = make_ad(rng, label, scam_keys)
            record = record_for(ad, sources)
            if record.id not in seen:
                seen.add(record.id)
                items.append((record, render(ad)))
    stored = corpus.add_many(items)
    logger.info("p2p_ads_done", stored=stored, scam_patterns=scam_keys)
    return stored


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--per-label", type=int, default=150)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()
    build(Corpus(), args.per_label, args.seed)


if __name__ == "__main__":
    main()
