"""
Synthetic service-provider pages (``service_site`` and ``service_listing``), Netherlands and US.

Real rogue-trader sites come from ACM decisions (:mod:`evals.sources.acm_traders`) and real established firms from
OpenStreetMap (:mod:`evals.sources.osm_trades`), but both are company websites of one era and mostly Dutch. These
pages fill the gaps, so that neither label is all real or all synthetic:

- **service_site**: a company's own site. ``scam``: the documented fake-local-business pattern (ACM's KeyService,
  Allfree and Leadle decisions; Fraudehelpdesk, Kassa, Consumentenbond, FTC): a site named after a town, one
  national number, "24/7, there in 15 minutes", a call-out fee as the only price, a footer of city pages (doorway
  pages), no company address or registration, templated reviews, payment on the spot. ``legit``: a family firm
  with an address, KvK or licence number, published rates or written quotes, a plausible service area.
- **service_listing**: a provider's profile on Werkspot (NL) or Thumbtack (US), with the platform's own facts
  (member since, jobs, reviews, verified badges). The rogue side uses the same documented patterns.

Business and reviewer names are made up (common surnames, first names only for reviewers); phone numbers are made
up too (US numbers in the 555-01xx range). Every page is ``source="synthetic"`` with ``notes.synthetic`` true, a
rule label until the second opinion agrees, and the patterns used in ``rule_features``.

Usage: ``uv run python -m evals.sources.service_pages [--sites-per-label 30] [--listings-per-label 150]``.
"""

import argparse
import html
import json
import random
from dataclasses import dataclass, field

import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.sources.patterns import confirmed_patterns

logger = structlog.get_logger("unfret.evals")

DATASET = "service-synthetic-2026"


@dataclass(frozen=True)
class Trade:
    """A trade, in both languages, with its schema.org type and a typical price."""

    key: str
    nl: str
    nl_plural: str
    en: str
    ld_type: str
    nl_jobs: tuple[str, ...]
    en_jobs: tuple[str, ...]
    hourly_eur: int
    hourly_usd: int


TRADES: tuple[Trade, ...] = (
    Trade(
        "locksmith",
        "Slotenmaker",
        "slotenmakers",
        "Locksmith",
        "Locksmith",
        ("deur openen", "cilinder vervangen", "inbraakschade herstellen", "sloten met politiekeurmerk"),
        ("lockouts", "rekeying", "lock replacement", "smart lock installation"),
        65,
        95,
    ),
    Trade(
        "plumber",
        "Loodgieter",
        "loodgieters",
        "Plumber",
        "Plumber",
        ("lekkage verhelpen", "kraan vervangen", "toilet repareren", "leidingwerk"),
        ("leak repair", "water heater replacement", "toilet repair", "repiping"),
        60,
        110,
    ),
    Trade(
        "drain",
        "Ontstoppingsdienst",
        "ontstoppers",
        "Drain cleaning",
        "Plumber",
        ("riool ontstoppen", "camera-inspectie", "afvoer reinigen", "wc ontstoppen"),
        ("drain clearing", "sewer camera inspection", "hydro jetting", "clogged toilets"),
        70,
        120,
    ),
    Trade(
        "electrician",
        "Elektricien",
        "elektriciens",
        "Electrician",
        "Electrician",
        ("groepenkast vervangen", "storing verhelpen", "laadpaal installeren", "stopcontacten aanleggen"),
        ("panel upgrades", "troubleshooting", "EV charger installation", "outlet and wiring"),
        58,
        105,
    ),
    Trade(
        "pest",
        "Ongediertebestrijding",
        "ongediertebestrijders",
        "Pest control",
        "HomeAndConstructionBusiness",
        ("wespennest verwijderen", "muizen bestrijden", "bedwantsen behandelen", "preventief advies"),
        ("wasp nest removal", "rodent control", "bed bug treatment", "inspections"),
        75,
        90,
    ),
    Trade(
        "roofer",
        "Dakdekker",
        "dakdekkers",
        "Roofer",
        "RoofingContractor",
        ("daklekkage", "bitumen dak vervangen", "dakgoot reinigen", "dakpannen herstellen"),
        ("roof leak repair", "shingle replacement", "gutter cleaning", "flat roofs"),
        55,
        85,
    ),
)

NL_TOWNS = (
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
    "Apeldoorn",
    "Deventer",
    "Alkmaar",
    "Zaandam",
    "Hilversum",
    "Almere",
    "Leeuwarden",
    "Maastricht",
    "Venlo",
    "Helmond",
    "Oss",
    "Ede",
    "Gouda",
    "Dordrecht",
    "Zoetermeer",
    "Hoorn",
    "Assen",
    "Emmen",
    "Roosendaal",
    "Heerlen",
)
US_TOWNS = (
    ("Austin", "TX"),
    ("Dallas", "TX"),
    ("Houston", "TX"),
    ("Phoenix", "AZ"),
    ("Mesa", "AZ"),
    ("Denver", "CO"),
    ("Aurora", "CO"),
    ("Seattle", "WA"),
    ("Tacoma", "WA"),
    ("Portland", "OR"),
    ("Sacramento", "CA"),
    ("San Jose", "CA"),
    ("San Diego", "CA"),
    ("Atlanta", "GA"),
    ("Orlando", "FL"),
    ("Tampa", "FL"),
    ("Charlotte", "NC"),
    ("Raleigh", "NC"),
    ("Columbus", "OH"),
    ("Chicago", "IL"),
    ("Minneapolis", "MN"),
    ("Nashville", "TN"),
    ("Las Vegas", "NV"),
    ("Salt Lake City", "UT"),
)
NL_SURNAMES = (
    "de Vries",
    "Jansen",
    "Bakker",
    "Visser",
    "Smit",
    "Meijer",
    "de Boer",
    "Mulder",
    "Bos",
    "Vos",
    "Peters",
    "Hendriks",
    "Dekker",
    "Brouwer",
    "de Wit",
    "Dijkstra",
    "Kok",
    "Vermeulen",
    "Postma",
)
US_SURNAMES = (
    "Miller",
    "Johnson",
    "Anderson",
    "Thompson",
    "Garcia",
    "Martinez",
    "Wilson",
    "Nelson",
    "Carlson",
    "Peterson",
    "Hughes",
    "Russell",
    "Bennett",
    "Sullivan",
    "Foster",
    "Murphy",
    "Hayes",
    "Coleman",
)
FIRST_NAMES = {
    "nl": ("Sanne", "Mark", "Fleur", "Joost", "Ilse", "Bram", "Anouk", "Ruud", "Femke", "Daan"),
    "us": ("Jennifer", "Mike", "Sarah", "Dave", "Lisa", "Chris", "Amanda", "Tom", "Karen", "Steve"),
}
# US licence formats as states print them (numbers made up).
US_LICENSES = {
    "TX": "TDLR License #B{n}",
    "AZ": "ROC #{n}",
    "CO": "License #{n}",
    "WA": "L&I Reg. #{n}",
    "OR": "CCB #{n}",
    "CA": "CSLB License #{n}",
    "GA": "License #MP{n}",
    "FL": "License #CFC{n}",
    "NC": "License #{n}",
    "OH": "OCILB #{n}",
    "IL": "License #058-{n}",
    "MN": "License #BC{n}",
    "TN": "License #{n}",
    "NV": "NSCB #{n}",
    "UT": "DOPL #{n}",
}
_NL_AREA_CODES = {
    "Amsterdam": "020",
    "Utrecht": "030",
    "Rotterdam": "010",
    "Den Haag": "070",
    "Eindhoven": "040",
    "Groningen": "050",
    "Nijmegen": "024",
    "Haarlem": "023",
    "Amersfoort": "033",
    "Zwolle": "038",
    "Leiden": "071",
    "Tilburg": "013",
    "Breda": "076",
    "Arnhem": "026",
    "Enschede": "053",
}

_SCAM_PATTERNS = (
    "fake_local",
    "emergency_247",
    "vague_prices",
    "no_identity",
    "fake_reviews",
    "pay_on_the_spot",
    "fake_quality_claims",
)
_GOOD_PATTERNS = ("trade_registration", "written_quote", "real_address")


@dataclass
class ServicePage:
    """One generated service page and the facts it was built from."""

    kind: str  # service_site or service_listing
    market: str  # nl or us
    trade: Trade
    label: str
    name: str
    town: str
    state: str
    url: str
    patterns: list[str]
    body: str
    ld: dict[str, object] = field(default_factory=dict)
    platform: str = ""


def _nl_phone(rng: random.Random, town: str, scam: bool) -> str:
    if scam:
        return rng.choice(("085", "088", "0900")) + f"-{rng.randint(100, 999)} {rng.randint(1000, 9999)}"
    area = _NL_AREA_CODES.get(town, "0" + str(rng.randint(30, 79)))
    return f"{area}-{rng.randint(100, 999)} {rng.randint(1000, 9999)}"


def _us_phone(rng: random.Random, scam: bool) -> str:
    if scam:
        return f"1-8{rng.choice('0456')}{rng.choice('0478')}-555-01{rng.randint(10, 99)}"
    return f"({rng.randint(201, 989)}) 555-01{rng.randint(10, 99)}"


def _a(noun: str) -> str:
    return f"{'an' if noun[:1].lower() in 'aeiou' else 'a'} {noun.lower()}"


def _slug(text: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in text)
    return "-".join(part for part in keep.split("-") if part)


def _reviews(rng: random.Random, market: str, trade: Trade, scam: bool, town: str) -> list[tuple[str, str]]:
    names = FIRST_NAMES[market]
    if scam:
        pool = (
            (
                "Super snelle service, aanrader!",
                "Zeer tevreden, top bedrijf!",
                "Snel geholpen, 5 sterren!",
                "Uitstekend, heel vriendelijk!",
                "Prima geholpen, zeker aan te raden!",
            )
            if market == "nl"
            else (
                "Great service, highly recommend!",
                "Fast and professional!",
                "Five stars, very happy!",
                "Excellent work, thank you!",
                "Best in town, would use again!",
            )
        )
        return [(rng.choice(names), rng.choice(pool)) for _ in range(rng.randint(3, 5))]
    jobs = trade.nl_jobs if market == "nl" else trade.en_jobs
    if market == "nl":
        templates = (
            "Kwam op dinsdagochtend voor {job}. Prijs was vooraf afgesproken en klopte met de factuur.",
            (
                "Netjes gewerkt bij {job}, alles uitgelegd. Iets duurder dan verwacht door extra materiaal, "
                "maar dat werd eerst overlegd."
            ),
            "Al jaren onze vaste {trade} in {town}. Ook nu weer goed geholpen met {job}.",
            "Offerte binnen twee dagen, werk binnen een week gepland. Kleine vertraging werd netjes gemeld.",
        )
    else:
        templates = (
            "Came out Tuesday morning for {job}. Quote matched the final bill.",
            (
                "Did a clean job on {job} and explained everything. A bit more than I hoped because of parts, "
                "but they called first."
            ),
            "Our go-to {trade} in {town} for years. Handled {job} quickly again.",
            "Got a written estimate in two days, scheduled within the week. Communicated a small delay.",
        )
    return [
        (
            rng.choice(names),
            rng.choice(templates).format(
                job=rng.choice(jobs), trade=trade.nl.lower() if market == "nl" else trade.en.lower(), town=town
            ),
        )
        for _ in range(rng.randint(2, 4))
    ]


COPY: dict[str, dict[str, str]] = {
    "nl": {
        "hero_scam": "{trade} nodig in {town}? Wij zijn uw lokale {trade_l}!",
        "emergency_247": "24/7 bereikbaar, ook in het weekend en op feestdagen. Binnen 15 minuten ter plaatse!",
        "call": "Bel direct",
        "vague_prices": "De prijs hangt af van de situatie ter plaatse; onze monteur bespreekt dit met u.",
        "fake_quality_claims": "Al onze monteurs zijn gecertificeerd, gescreend en erkend vakman.",
        "pay_on_the_spot": "Betaling direct na afloop, contant of met pin bij de monteur.",
        "reviews_scam": "Klantbeoordelingen ★★★★★ 9,8",
        "areas": "Ook actief in",
        "hero_good": "Uw {trade_l} in {town} en omgeving, sinds {since}.",
        "prices_good": (
            "<h2>Tarieven</h2><table><tr><td>Voorrijkosten</td><td>€ {callout},-</td></tr>"
            "<tr><td>Uurtarief</td><td>€ {hourly},- per uur incl. btw</td></tr>"
            "<tr><td>Spoedtoeslag (avond/weekend)</td><td>50%</td></tr></table>"
            "<p>Voor grotere klussen maken we vooraf een vrijblijvende offerte op papier. "
            "U betaalt na afloop op factuur.</p>"
        ),
        "reviews_good": "Wat klanten zeggen",
        "service_area": "Werkgebied",
        # platform profile
        "joined": "Lid sinds {since}",
        "new_month": "oktober 2026|november 2026",
        "reviews_n": "{n} beoordelingen",
        "no_reviews": "Nog geen beoordelingen",
        "jobs_n": "{n} klussen via Werkspot",
        "rated": "{rating} ★ uit {n} beoordelingen",
        "checked": "KvK-nummer gecontroleerd",
        "responds": "Reageert meestal binnen 2 uur",
        "about": "Over ons",
        "reviews": "Beoordelingen",
        "quote": "Offerte aanvragen",
        "anywhere": "{trade} in heel Nederland.",
        "l_emergency_247": "24/7 spoedservice, binnen 30 minuten bij u.",
        "l_vague_prices": "Voorrijkosten € 0. Prijs in overleg ter plaatse.",
        "l_pay_on_the_spot": "Betaling contant of pin direct na de klus.",
        "l_fake_local": "Bel ons landelijke nummer {phone} voor een monteur in uw buurt.",
        "l_no_identity": "Bedrijfsgegevens op aanvraag.",
        "l_fake_quality_claims": "Gecertificeerde vakmensen, 100% garantie.",
        "l_intro": "Wij zijn {name}, gevestigd in {town} en actief in de regio.",
        "l_specialties": "Specialiteiten: {jobs}.",
        "l_quote": "Vooraf een duidelijke prijsopgave, betalen na afloop op factuur.",
        "l_rate": "Uurtarief vanaf € {hourly}.",
    },
    "us": {
        "hero_scam": "Need {a_trade} in {town}? Your local {trade_l} is here!",
        "emergency_247": "Open 24/7, nights, weekends and holidays. On site in 15 minutes!",
        "call": "Call now",
        "vague_prices": "Final price depends on the job; the technician will discuss it on site.",
        "fake_quality_claims": "All technicians are certified, background checked and fully vetted.",
        "pay_on_the_spot": "Payment due on completion, cash preferred.",
        "reviews_scam": "Reviews ★★★★★ 4.9",
        "areas": "Service areas",
        "hero_good": "Serving {town} and nearby since {since}. Family owned and operated.",
        "prices_good": (
            "<h2>Pricing</h2><p>Service call ${callout}, applied to the repair. Hourly rate ${hourly}. "
            "Free written estimates for larger jobs; you pay when the work is done.</p>"
        ),
        "reviews_good": "What customers say",
        "service_area": "Service area",
        "joined": "Joined {since}",
        "new_month": "October 2026|November 2026",
        "reviews_n": "{n} reviews",
        "no_reviews": "No reviews yet",
        "jobs_n": "Hired {n} times",
        "rated": "{rating} ★ ({n} reviews)",
        "checked": "Background checked",
        "responds": "Responds within about 2 hours",
        "about": "About",
        "reviews": "Reviews",
        "quote": "Request a quote",
        "anywhere": "{trade} anywhere in your area.",
        "l_emergency_247": "24/7 emergency service, there in 30 minutes.",
        "l_vague_prices": "No trip fee. Price discussed on site.",
        "l_pay_on_the_spot": "Cash or card on completion, deposit required to book.",
        "l_fake_local": "Call {phone} to reach a local technician.",
        "l_no_identity": "Company details available on request.",
        "l_fake_quality_claims": "Certified pros, 100% guaranteed.",
        "l_intro": "We're {name}, a {trade_l} business based in {town}, serving the area.",
        "l_specialties": "Specialties: {jobs}.",
        "l_quote": "Upfront written quotes; pay when the job is done.",
        "l_rate": "Rates from ${hourly}/hour.",
    },
}
_BAIT = {
    "nl": ("Voorrijkosten vanaf € 0,-", "Gratis voorrijden in heel Nederland"),
    "us": ("$19 service call", "No trip charge!"),
}
_BAIT_TRADE = {
    ("nl", "drain"): "Ontstoppen vanaf € 9,95 per meter",
    ("us", "drain"): "Drain clearing from $9.95 per foot",
    ("us", "locksmith"): "Lockouts from $15",
}


@dataclass(frozen=True)
class _Who:
    """Where a generated business is and what it is called."""

    market: str
    trade: Trade
    tname: str
    town: str
    state: str
    name: str
    phone: str

    @property
    def nl(self) -> bool:
        return self.market == "nl"

    def say(self, key: str, **extra: object) -> str:
        """The market's copy for ``key``, filled in."""
        return COPY[self.market][key].format(
            trade=self.tname,
            trade_l=self.tname.lower(),
            a_trade=_a(self.tname),
            town=self.town,
            name=self.name,
            **extra,
        )


def _where(rng: random.Random, market: str) -> tuple[str, str]:
    return (rng.choice(NL_TOWNS), "") if market == "nl" else rng.choice(US_TOWNS)


def _towns(market: str) -> list[str]:
    return list(NL_TOWNS) if market == "nl" else [t for t, _ in US_TOWNS]


def _reviews_html(rng: random.Random, who: _Who, heading: str, scam: bool) -> str:
    quotes = "".join(
        f"<blockquote>{html.escape(t)} — {html.escape(n)}</blockquote>"
        for n, t in _reviews(rng, who.market, who.trade, scam, who.town)
    )
    return f"<section class='reviews'><h2>{html.escape(heading)}</h2>{quotes}</section>"


def _scam_site_parts(rng: random.Random, who: _Who, patterns: list[str]) -> tuple[list[str], str]:
    e = html.escape
    parts = [f"<h1>{e(who.name)}</h1>", f"<p class='hero'>{e(who.say('hero_scam'))}</p>"]
    if "emergency_247" in patterns:
        parts.append(f"<p class='usp'>{e(who.say('emergency_247'))}</p>")
    parts.append(f"<p class='call'>{e(who.say('call'))}: <a href='tel:{e(who.phone)}'>{e(who.phone)}</a></p>")
    if "vague_prices" in patterns:
        bait = [*_BAIT[who.market], *([_BAIT_TRADE[k]] if (k := (who.market, who.trade.key)) in _BAIT_TRADE else [])]
        parts += [f"<p>{e(rng.choice(bait))}</p>", f"<p>{e(who.say('vague_prices'))}</p>"]
    parts += [f"<p>{e(who.say(k))}</p>" for k in ("fake_quality_claims", "pay_on_the_spot") if k in patterns]
    jobs = who.trade.nl_jobs if who.nl else who.trade.en_jobs
    parts.append("<ul>" + "".join(f"<li>{e(j.capitalize())}</li>" for j in jobs) + "</ul>")
    if "fake_reviews" in patterns:
        parts.append(_reviews_html(rng, who, who.say("reviews_scam"), scam=True))
    towns = [t for t in _towns(who.market) if t != who.town]
    links = " ".join(
        f"<a href='/{_slug(who.tname)}-{_slug(t)}/'>{e(who.tname)} {e(t)}</a>"
        for t in rng.sample(towns, min(len(towns), rng.randint(18, 30)))
    )
    parts.append(f"<section class='areas'><h2>{e(who.say('areas'))}</h2>{links}</section>")
    contact = f"© {e(who.name)}" if "no_identity" in patterns else f"© {e(who.name)} · {e(who.town)}"
    return parts, contact


def _legit_site_parts(rng: random.Random, who: _Who) -> tuple[list[str], str, str]:
    e = html.escape
    street = rng.choice(
        ("Industrieweg", "Ambachtsweg", "Kerkstraat", "Dorpsstraat", "Nijverheidsweg")
        if who.nl
        else ("Main St", "Commerce Dr", "Industrial Blvd", "Oak Ave", "Market St")
    )
    number = rng.randint(2, 180)
    jobs = who.trade.nl_jobs if who.nl else who.trade.en_jobs
    hourly = who.trade.hourly_eur if who.nl else who.trade.hourly_usd
    callout = rng.choice((35, 39, 45, 49) if who.nl else (79, 89, 95, 99))
    nearby_pool = [t for t in (NL_TOWNS if who.nl else [t for t, s in US_TOWNS if s == who.state]) if t != who.town]
    nearby = rng.sample(nearby_pool, k=min(4, len(nearby_pool)))
    parts = [
        f"<h1>{e(who.name)}</h1>",
        f"<p class='hero'>{e(who.say('hero_good', since=rng.randint(1962, 2008)))}</p>",
        "<ul>" + "".join(f"<li>{e(j.capitalize())}</li>" for j in rng.sample(jobs, 3)) + "</ul>",
        who.say("prices_good", callout=callout, hourly=hourly),
        _reviews_html(rng, who, who.say("reviews_good"), scam=False),
        f"<p>{e(who.say('service_area'))}: {e(', '.join([who.town, *nearby]))}</p>",
    ]
    if who.nl:
        member = rng.choice(
            (
                "Lid van Techniek Nederland",
                "Aangesloten bij het NSSG",
                "Erkend leerbedrijf",
                "Politiekeurmerk Veilig Wonen",
            )
        )
        postcode = f"{rng.randint(1000, 9999)} {rng.choice('ABCDEFGHJK')}{rng.choice('ABCDEFGHJK')}"
        contact = (
            f"{e(who.name)} · {e(street)} {number}, {postcode} {e(who.town)} · Tel. {e(who.phone)} · "
            f"KvK {rng.randint(10_000_000, 99_999_999)} · btw NL{rng.randint(100_000_000, 999_999_999)}B01 · "
            f"{e(member)}"
        )
        address = f"{street} {number}"
    else:
        licence = US_LICENSES.get(who.state, "License #{n}").format(n=rng.randint(100_000, 999_999))
        contact = (
            f"{e(who.name)} · {number} {e(street)}, {e(who.town)}, {who.state} {rng.randint(10_000, 99_999)} · "
            f"{e(who.phone)} · Licensed, bonded &amp; insured · {e(licence)}"
        )
        address = f"{number} {street}"
    return parts, contact, address


def _site(rng: random.Random, market: str, trade: Trade, label: str, scam_keys: list[str]) -> ServicePage:
    scam = label == "scam"
    nl = market == "nl"
    town, state = _where(rng, market)
    tname = trade.nl if nl else trade.en.title()
    if scam:
        name = f"{tname} {town}" + (" 24/7" if rng.random() < 0.4 else "")
        domain = (
            f"{_slug(tname)}-{_slug(town)}.nl"
            if nl
            else f"{_slug(town)}{_slug(tname).replace('-', '')}{rng.choice(['247', 'pros', 'now'])}.com"
        )
        others = [k for k in _SCAM_PATTERNS if k in scam_keys and k != "fake_local"]
        patterns = ["fake_local", *rng.sample(others, min(len(others), rng.randint(2, 4)))]
    else:
        surname = rng.choice(NL_SURNAMES if nl else US_SURNAMES)
        suffixes = (
            ["Installatietechniek", "Vakwerk", tname, f"{tname} & Zn."]
            if nl
            else [tname, f"{tname} & Sons", f"{tname} Co."]
        )
        name = f"{surname} {rng.choice(suffixes)}"
        name = name[0].upper() + name[1:]
        domain = f"{_slug(name).replace('-', '')}.{'nl' if nl else 'com'}"
        patterns = list(_GOOD_PATTERNS)
    phone = _nl_phone(rng, town, scam) if nl else _us_phone(rng, scam)
    who = _Who(market, trade, tname, town, state, name, phone)
    url = f"https://www.{domain}/"
    address: dict[str, object] = {"@type": "PostalAddress", "addressLocality": town}
    ld: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": trade.ld_type,
        "name": name,
        "telephone": phone,
        "url": url,
        "address": address,
    }
    if scam:
        parts, contact = _scam_site_parts(rng, who, patterns)
        ld["areaServed"] = [town, *rng.sample(_towns(market), 6)]
    else:
        parts, contact, street = _legit_site_parts(rng, who)
        address["streetAddress"] = street
    body = "\n".join(parts) + f"\n<section class='contact'><p>{contact}</p></section>"
    body += f"\n<footer><p>© {html.escape(name)}</p></footer>"
    return ServicePage("service_site", market, trade, label, name, town, state, url, patterns, body, ld)


def _profile_facts(rng: random.Random, who: _Who, scam: bool) -> list[str]:
    if scam:
        n = rng.choice((0, 0, 2, 3))
        return [
            who.say("joined", since=rng.choice(COPY[who.market]["new_month"].split("|"))),
            who.say("reviews_n", n=n) if n else who.say("no_reviews"),
            who.say("jobs_n", n=n),
        ]
    jobs_done = rng.randint(40, 900)
    facts = [
        who.say("joined", since=rng.randint(2012, 2021)),
        who.say("rated", rating=round(rng.uniform(4.4, 5.0), 1), n=rng.randint(20, max(21, jobs_done // 2))),
        who.say("jobs_n", n=jobs_done),
        who.say("checked"),
        who.say("responds"),
    ]
    if not who.nl:
        facts += [f"{rng.randint(6, 30)} years in business", "License verified"]
    return facts


def _profile_about(rng: random.Random, who: _Who, scam: bool, patterns: list[str]) -> list[str]:
    if scam:
        phone = _nl_phone(rng, who.town, True) if who.nl else _us_phone(rng, True)
        keys = ("emergency_247", "vague_prices", "pay_on_the_spot", "fake_local", "no_identity", "fake_quality_claims")
        return [who.say("anywhere"), *(who.say(f"l_{k}", phone=phone) for k in keys if k in patterns)]
    jobs = who.trade.nl_jobs if who.nl else who.trade.en_jobs
    hourly = who.trade.hourly_eur if who.nl else who.trade.hourly_usd
    return [
        who.say("l_intro"),
        who.say("l_specialties", jobs=", ".join(rng.sample(jobs, 3))),
        who.say("l_quote"),
        who.say("l_rate", hourly=hourly),
    ]


def _listing(rng: random.Random, market: str, trade: Trade, label: str, scam_keys: list[str]) -> ServicePage:
    scam = label == "scam"
    nl = market == "nl"
    e = html.escape
    town, state = _where(rng, market)
    tname = trade.nl if nl else trade.en.title()
    if scam:
        name = rng.choice(
            (f"{tname} Service {town}", f"Spoed {tname} {town}" if nl else f"{town} {tname} 24/7", f"{tname} Direct")
        )
        patterns = rng.sample([k for k in _SCAM_PATTERNS if k in scam_keys], rng.randint(3, 4))
    else:
        surname = rng.choice(NL_SURNAMES if nl else US_SURNAMES)
        name = f"{surname} {tname}" if rng.random() < 0.6 else f"{tname} {surname}"
        name = name[0].upper() + name[1:]
        patterns = list(_GOOD_PATTERNS)
    who = _Who(market, trade, tname, town, state, name, "")
    pid = rng.randint(100_000, 9_999_999)
    platform = "Werkspot" if nl else "Thumbtack"
    url = (
        f"https://www.werkspot.nl/profiel/{_slug(name)}-{pid}"
        if nl
        else f"https://www.thumbtack.com/{state.lower()}/{_slug(town)}/{_slug(tname)}/{_slug(name)}/service/{pid}"
    )
    facts = "".join(f"<li>{e(x)}</li>" for x in _profile_facts(rng, who, scam))
    about = "".join(f"<p>{e(a)}</p>" for a in _profile_about(rng, who, scam, patterns))
    reviews = _reviews_html(rng, who, who.say("reviews"), scam) if not scam or "fake_reviews" in patterns else ""
    where = f"{town}, {state}" if state else town
    body = (
        f"<h1>{e(name)}</h1><p class='where'>{e(where)}</p><ul class='facts'>{facts}</ul>"
        f"<section class='about'><h2>{e(who.say('about'))}</h2>{about}</section>{reviews}"
        f"<button>{e(who.say('quote'))}</button>"
    )
    return ServicePage(
        "service_listing", market, trade, label, name, town, state, url, patterns, body, platform=platform
    )


def render(page: ServicePage) -> str:
    """
    Render a service page as HTML.

    :param page: The page.
    :return: The HTML.
    """
    e = html.escape
    lang = "nl" if page.market == "nl" else "en"
    header = (
        "<header><a href='https://www.werkspot.nl/'>Werkspot</a></header>"
        if page.platform == "Werkspot"
        else (
            "<header><a href='https://www.thumbtack.com/'>Thumbtack</a></header>"
            if page.platform == "Thumbtack"
            else f"<header><a href='/'>{e(page.name)}</a> <nav><a href='/contact/'>Contact</a> <a href='/over-ons/'>"
            f"{'Over ons' if lang == 'nl' else 'About'}</a></nav></header>"
        )
    )
    ld = f"<script type='application/ld+json'>{json.dumps(page.ld, ensure_ascii=False)}</script>" if page.ld else ""
    title = f"{page.name} | {page.platform}" if page.platform else page.name
    return (
        f"<!doctype html>\n<html lang='{lang}'><head><meta charset='utf-8'><title>{e(title)}</title>"
        f"<link rel='canonical' href='{e(page.url)}'>{ld}</head><body>{header}<main>{page.body}</main>"
        "</body></html>\n"
    )


def record_for(page: ServicePage, sources: dict[str, list[str]]) -> LabelRecord:
    """
    The corpus record of a generated page.

    :param page: The page.
    :param sources: Confirmed pattern sources.
    :return: The record (a rule label, until the second opinion agrees).
    """
    notes = {
        "synthetic": True,
        "generator": "service_pages",
        "market": page.market,
        "trade": page.trade.key,
        "patterns": page.patterns,
        "sources": sorted({u for k in page.patterns for u in sources.get(k, [])}),
    }
    return LabelRecord(
        id=page_id("svc", page.url),
        label=page.label,  # type: ignore[arg-type]
        label_source="rule",
        prior_label=page.label,  # type: ignore[arg-type]
        confidence=0.7,
        platform=page.platform,
        page_type=page.kind,
        source="synthetic",
        origin_dataset=DATASET,
        origin_url=page.url,
        collected_at="2026",
        notes=json.dumps(notes, sort_keys=True),
        rule_features=list(page.patterns),
    )


def build(corpus: Corpus, sites_per_label: int, listings_per_label: int, seed: int) -> int:
    """
    Generate service sites and platform listings for both labels and both markets and add them to the corpus.

    :param corpus: The corpus.
    :param sites_per_label: ``service_site`` pages per label.
    :param listings_per_label: ``service_listing`` pages per label.
    :param seed: Random seed.
    :return: The number of pages stored.
    :raises SystemExit: When too few patterns have confirmed sources.
    """
    sources = confirmed_patterns(corpus, "service")
    scam_keys = [k for k in _SCAM_PATTERNS if k in sources]
    if "fake_local" not in scam_keys or len(scam_keys) < 4 or not set(_GOOD_PATTERNS) <= set(sources):
        raise SystemExit("Too few confirmed service patterns; run `python -m evals.sources.patterns` first.")
    rng = random.Random(seed)
    items: dict[str, tuple[LabelRecord, str]] = {}
    for _kind, per_label, make in (
        ("service_site", sites_per_label, _site),
        ("service_listing", listings_per_label, _listing),
    ):
        for label in ("scam", "legit"):
            made = 0
            while made < per_label:
                market = "nl" if rng.random() < 0.55 else "us"
                page = make(rng, market, rng.choice(TRADES), label, scam_keys)
                record = record_for(page, sources)
                if record.id in items:
                    continue
                items[record.id] = (record, render(page))
                made += 1
    stored = corpus.add_many(items.values())
    logger.info("service_pages_done", stored=stored)
    return stored


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--sites-per-label", type=int, default=30)
    parser.add_argument("--listings-per-label", type=int, default=150)
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()
    build(Corpus(), args.sites_per_label, args.listings_per_label, args.seed)


if __name__ == "__main__":
    main()
