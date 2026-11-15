"""
What a web shop says about itself against what the page shows: claims that the facts contradict.

Rules only, and only clear contradictions:

- **Origin**: "a Dutch company", "family business in Amsterdam", "based in Germany", while every address the shop
  publishes is in another country (an address whose country can't be told counts as no evidence either way).
- **Physical store**: "visit our store" or "come by our showroom", while the shop publishes no address at all.
- **Founding year** ("since 1998", "founded 20 years ago") against the domain's age: that comparison needs the
  registration lookup, so :class:`~unfret.graph.skills.domain_age.DomainAgeSkill` makes it with
  :func:`claimed_founding`.

The evidence quotes the claim and states the fact. Countries are told from the address text (country names and
codes, Dutch postcodes, US state and ZIP), never with a geocoding service.
"""

import re
from dataclasses import dataclass

from unfret.facts import PageFacts
from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

COUNTRY_NAMES = {
    "NL": "the Netherlands",
    "BE": "Belgium",
    "DE": "Germany",
    "FR": "France",
    "GB": "the United Kingdom",
    "US": "the United States",
    "CN": "China",
    "HK": "Hong Kong",
}

# ── Claims ──────────────────────────────────────────────────────────────────────────────────────────────────

_NOUNS = (
    r"(?:company|business|brand|shop|webshop|web shop|online shop|online store|store|retailer|family business"
    r"|bedrijf|merk|winkel|webwinkel|familiebedrijf|onderneming|unternehmen|firma|familienunternehmen|marke"
    r"|entreprise|marque|boutique)"
)
_PLACES: dict[str, str] = {
    "NL": r"(?:the netherlands|netherlands|nederland|holland|amsterdam|rotterdam|utrecht|den haag|the hague"
    r"|eindhoven|groningen|haarlem|leiden|tilburg|breda|nijmegen|arnhem|zwolle|delft|amersfoort|apeldoorn"
    r"|maastricht|enschede|almere|den bosch|'s-hertogenbosch)",
    "BE": r"(?:belgium|belgi[ëe]|belgique|brussels|brussel|bruxelles|antwerp|antwerpen|ghent|gent|bruges|brugge"
    r"|leuven|li[èe]ge)",
    "DE": r"(?:germany|deutschland|duitsland|berlin|munich|m[üu]nchen|hamburg|cologne|k[öo]ln|frankfurt"
    r"|stuttgart|d[üu]sseldorf)",
    "FR": r"(?:france|frankrijk|frankreich|paris|parijs|lyon|marseille|bordeaux|toulouse)",
    "GB": r"(?:the uk|the united kingdom|united kingdom|great britain|england|london|manchester|birmingham"
    r"|scotland|edinburgh|glasgow)",
    "US": r"(?:the usa|the us|the united states|united states|america)",
}
_ADJECTIVES: dict[str, str] = {
    "NL": r"(?:dutch|nederlandse?|hollandse?)",
    "BE": r"(?:belgian|belgische?|belge)",
    "DE": r"(?:german|deutsche?s?|duitse?)",
    "FR": r"(?:french|fran[çc]aise?|franse?)",
    "GB": r"(?:british|uk-based|uk|english|britse?)",
    "US": r"(?:american|us-based|amerikaanse?)",
}
_ORIGIN_RES: dict[str, re.Pattern[str]] = {
    code: re.compile(
        rf"(?<![a-z])(?:(?:we are|we're|wij zijn|we zijn|a|an|een|ons|onze|our|is a|is an)\s+(?:(?:real|true|"
        rf"proud|small|echte?|trotse?|kleine?|independent|onafhankelijke?)\s+)?{_ADJECTIVES[code]}\s+{_NOUNS}"
        rf"|(?:based|located|headquartered|gevestigd|founded|opgericht|ans[äa]ssig|basiert)\s+in\s+{_PLACES[code]}"
        rf"|(?:family business|family-owned business|familiebedrijf|small business|klein bedrijf)\s+(?:in|from|uit)"
        rf"\s+{_PLACES[code]})(?![a-z])",
        re.IGNORECASE,
    )
    for code in _PLACES
}
# Only words for premises: "visit our shop" or "our store" is as often the website ("visit our shop in your
# language", "visit our Amazon store").
_STORE_RE = re.compile(
    r"(?<![a-z])(?:(?:visit|come (?:by|to|and see)|stop by|drop by|bezoek|kom langs (?:in|bij)|besuchen sie)\s+"
    r"(?:us (?:in|at) )?(?:our|onze|ons|unseren?|unser)\s+(?:showroom|flagship store|ladengesch[äa]ft"
    r"|(?:physical|brick-and-mortar) (?:store|shop)|fysieke winkel)"
    r"|(?:our|onze|ons) (?:physical|brick-and-mortar) (?:store|shop)|(?:onze|ons) fysieke winkel)(?![a-z])",
    re.IGNORECASE,
)
_FOUNDED_YEAR_RE = re.compile(
    r"(?<![a-z0-9])(?:since|established(?: in)?|est\.|founded(?: in)?|opgericht(?: in)?|sinds|seit|gegr[üu]ndet"
    r"(?: im jahr)?|depuis|fond[ée]e? en|desde)\s+(1[89]\d\d|20[0-3]\d)(?![0-9])",
    re.IGNORECASE,
)
_YEARS_AGO_RE = re.compile(
    r"(?<![a-z0-9])(?:(?:founded|established|started|opgericht|gestart|begonnen)\s+(?:over |more than |ruim |meer "
    r"dan |bijna )?(\d{1,3})\s+(?:years|jaar)\s+(?:ago|geleden)"
    r"|(?:for |al )(?:over |more than |ruim |meer dan )?(\d{1,3})\+?\s+(?:years|jaar)\s+(?:in business|actief"
    r"|een begrip)|(\d{1,3})\+?\s+years in business)(?![a-z])",
    re.IGNORECASE,
)
# A founding claim counts only about the shop itself: "we", "our", "the company" near it, not a brand it resells.
_SELF_RE = re.compile(
    r"(?<![a-z])(?:we|we're|our|us|wij|we zijn|ons|onze|uns|unser\w*|nous|notre|family|familie\w*|company|bedrijf"
    r"|shop|webshop|winkel|store|firma|unternehmen)(?![a-z])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Claim:
    """A claim the page makes, with the words it uses."""

    value: str  # a country code, or "store"
    quote: str


def origin_claims(text: str) -> list[Claim]:
    """
    The countries a page says the business is from or based in.

    :param text: The page text.
    :return: One claim per country, the first wording found.
    """
    found = []
    for code, pattern in _ORIGIN_RES.items():
        if m := pattern.search(text):
            found.append(Claim(code, " ".join(m.group().split())))
    return found


def store_claim(text: str) -> Claim | None:
    """
    A claim of a physical store or showroom ("visit our store").

    :param text: The page text.
    :return: The claim, or ``None``.
    """
    m = _STORE_RE.search(text)
    return Claim("store", " ".join(m.group().split())) if m else None


def claimed_founding(facts: PageFacts, text: str, this_year: int) -> tuple[int, str] | None:
    """
    The earliest founding year the shop claims for itself, with the claim's words.

    From "since 1998"-style wording that speaks of the shop ("we", "our", "the company" nearby), "founded 20 years
    ago" and "20 years in business", and the ``foundingDate`` of the site's Organization data.

    :param facts: The page facts (``founded_claims``).
    :param text: The page text.
    :param this_year: The current year (to turn "20 years ago" into a year).
    :return: ``(year, quote)``, or ``None`` without a claim about the shop itself.
    """
    claims: list[tuple[int, str]] = []
    for m in _FOUNDED_YEAR_RE.finditer(text):
        context = text[max(m.start() - 80, 0) : m.end() + 30]
        if _SELF_RE.search(context):
            claims.append((int(m.group(1)), " ".join(m.group().split())))
    for m in _YEARS_AGO_RE.finditer(text):
        years = int(next(g for g in m.groups() if g))
        if 1 <= years <= 200:
            claims.append((this_year - years, " ".join(m.group().split())))
    dated = (re.search(r"(1[89]\d\d|20[0-3]\d)", c) for c in facts.founded_claims if c.startswith("foundingDate "))
    claims.extend((int(year.group(1)), f"founded {year.group(1)}") for year in dated if year)
    claims = [c for c in claims if 1800 <= c[0] <= this_year]
    return min(claims) if claims else None


# ── Address countries ───────────────────────────────────────────────────────────────────────────────────────

_COUNTRY_WORDS: dict[str, str] = {
    "netherlands": "NL",
    "the netherlands": "NL",
    "nederland": "NL",
    "holland": "NL",
    "nl": "NL",
    "belgium": "BE",
    "belgië": "BE",
    "belgie": "BE",
    "belgique": "BE",
    "be": "BE",
    "germany": "DE",
    "deutschland": "DE",
    "duitsland": "DE",
    "de": "DE",
    "france": "FR",
    "frankrijk": "FR",
    "fr": "FR",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "gb": "GB",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "china": "CN",
    "pr china": "CN",
    "p.r. china": "CN",
    "people's republic of china": "CN",
    "prc": "CN",
    "cn": "CN",
    "hong kong": "HK",
    "hongkong": "HK",
    "hk": "HK",
    "singapore": "SG",
    "taiwan": "TW",
    "turkey": "TR",
    "türkiye": "TR",
    "poland": "PL",
    "polska": "PL",
    "cyprus": "CY",
    "estonia": "EE",
    "lithuania": "LT",
    "latvia": "LV",
    "bulgaria": "BG",
    "romania": "RO",
    "spain": "ES",
    "españa": "ES",
    "italy": "IT",
    "italia": "IT",
    "portugal": "PT",
    "ireland": "IE",
    "austria": "AT",
    "österreich": "AT",
    "switzerland": "CH",
    "schweiz": "CH",
    "denmark": "DK",
    "sweden": "SE",
    "czech republic": "CZ",
    "czechia": "CZ",
    "india": "IN",
    "vietnam": "VN",
    "thailand": "TH",
    "malaysia": "MY",
    "indonesia": "ID",
    "pakistan": "PK",
    "united arab emirates": "AE",
    "uae": "AE",
    "canada": "CA",
    "australia": "AU",
}
# Cities that name their country on their own, for addresses that leave the country out.
_CITY_COUNTRIES: dict[str, str] = {
    "shenzhen": "CN",
    "guangzhou": "CN",
    "dongguan": "CN",
    "shanghai": "CN",
    "beijing": "CN",
    "hangzhou": "CN",
    "yiwu": "CN",
    "ningbo": "CN",
    "xiamen": "CN",
    "foshan": "CN",
    "kowloon": "HK",
    "wan chai": "HK",
    "tsim sha tsui": "HK",
    "mong kok": "HK",
}
_NL_POSTCODE_RE = re.compile(r"(?<![0-9A-Za-z])[1-9]\d{3} ?[A-Z]{2}(?![A-Za-z0-9])")
_US_STATE_ZIP_RE = re.compile(
    r"(?<![A-Za-z])(?:AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ"
    r"|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY),? \d{5}(?:-\d{4})?(?![0-9])"
)
_UK_POSTCODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}(?![A-Za-z0-9])")


def address_country(address: str) -> str | None:  # noqa: PLR0911 - one return per kind of evidence
    """
    The country of an address, when its text says so.

    The last comma-separated part when it names a country, else a city that names its country, a Dutch postcode,
    a US state and ZIP code or a UK postcode.

    :param address: The address as the page writes it.
    :return: An ISO country code, or ``None`` when the text doesn't tell.
    """
    parts = [p.strip(" .").lower() for p in re.split(r"[,\n]|\s{2,}", address) if p.strip(" .")]
    if parts and parts[-1] in _COUNTRY_WORDS:
        return _COUNTRY_WORDS[parts[-1]]
    lowered = address.lower()
    for word, code in _COUNTRY_WORDS.items():
        if len(word) > 3 and re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered):
            return code
    for city, code in _CITY_COUNTRIES.items():
        if re.search(rf"(?<![a-z]){city}(?![a-z])", lowered):
            return code
    if _US_STATE_ZIP_RE.search(address):
        return "US"
    if _NL_POSTCODE_RE.search(address):
        return "NL"
    if _UK_POSTCODE_RE.search(address):
        return "GB"
    return None


def has_any_address(facts: PageFacts, text: str) -> bool:
    """
    Whether the page shows an address or a way to reach the business anywhere.

    Addresses in its facts or structured data, a postcode in its text or legal lines, or a published phone number
    (an address is often only on the contact page; a shop that can be called isn't hiding).

    :param facts: The page facts.
    :param text: The page text.
    :return: ``True`` when there is one.
    """
    if facts.addresses or facts.phones or any(o.get("address") for o in facts.organization_ld):
        return True
    written = " ".join([text, *facts.legal_mentions, *facts.copyright_lines])
    return bool(_NL_POSTCODE_RE.search(written) or _US_STATE_ZIP_RE.search(written) or _UK_POSTCODE_RE.search(written))


class ClaimsSkill(Skill):
    """A web shop's claims about where it is from and its physical store, against the addresses it publishes."""

    name = "claims"
    description = "Compares what a shop says about itself (Dutch company, our store) with the addresses it shows."
    page_types = STOREFRONTS
    features = ("origin_contradicted", "store_without_address")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Find the claims and the facts that contradict them.

        :param state: Current graph state; uses ``facts`` and ``cleaned_content``.
        :return: The skill's features and findings.
        """
        facts, text = state["facts"], state["cleaned_content"]
        observations: list[Observation] = []
        countries = [address_country(a) for a in facts.addresses]
        contradicted = False
        if countries and None not in countries:
            for claim in origin_claims(text):
                if claim.value not in countries:
                    contradicted = True
                    where = sorted({COUNTRY_NAMES.get(c, c) for c in countries if c})
                    observations.append(
                        Observation(
                            type="origin_contradicted",
                            evidence=f'Says "{claim.quote}"; the only address it gives is in {", ".join(where)}.',
                        )
                    )
                    break
        store = store_claim(text)
        no_address = store is not None and not has_any_address(facts, text)
        if store is not None and no_address:
            observations.append(
                Observation(type="store_without_address", evidence=f'Says "{store.quote}"; no address anywhere.')
            )
        return self.output({"origin_contradicted": contradicted, "store_without_address": no_address}, observations)
