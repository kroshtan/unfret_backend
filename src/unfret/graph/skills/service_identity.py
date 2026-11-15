"""
What a service company's own site says about who it is, and whether that hangs together.

Only what the site itself shows: no register is looked up (VIES, which ``eu_registry`` already asks about a VAT
number, is the one exception, and it lives there). In the Netherlands every business must show its KvK
(Chamber of Commerce) number on its website, so a missing or malformed one is reported; in the US many trades
(plumbers, electricians, HVAC and other contractors) are licensed by the state, and a site that says "licensed" but
shows no licence number is reported as exactly that, never as unlicensed. Elsewhere the skill doesn't apply.
"""

import re

from unfret.facts.schema import PageFacts
from unfret.graph.skills import SERVICE_SITES, Observation, Skill, SkillOutput
from unfret.graph.skills.eu_registry import name_matches
from unfret.graph.state import AnalysisState
from unfret.market import service_market

# A KvK number that isn't eight digits: "KvK 1234567", "KvK-nummer: 123 456 789".
_KVK_ANY_RE = re.compile(
    r"(?<![a-z])(?:kvk|k\.v\.k\.|kamer van koophandel)(?:[\s.:#-]|nummer|number|nr|no)*(\d[\d .]{2,14}\d)(?![0-9])",
    re.IGNORECASE,
)
_LICENSED_RE = re.compile(r"(?<![a-z])licen[sc]ed(?![a-z])", re.IGNORECASE)
_NL_ADDRESS_RE = re.compile(r"\d.*(?<![0-9])[1-9]\d{3}\s?[A-Z]{2}(?![A-Za-z])")
_US_ADDRESS_RE = re.compile(r"^\s*\d{1,6}\s+\S.*(?<![A-Za-z])[A-Z]{2}\b")
_COPYRIGHT_NAME_RE = re.compile(
    r"(?:(?:copyright\s*)?(?:©|\(c\))|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?[\s,.]*(?P<name>[^|·]{3,80}?)"
    r"\s*(?:\.\s|\.?$|\s(?:all rights|alle rechten)|[|·])",
    re.IGNORECASE,
)
_SHOWN = 2


def copyright_company(facts: PageFacts) -> str:
    """
    The company a copyright line names ("© 2026 Installatiebedrijf De Vries B.V." → "Installatiebedrijf De Vries B.V").

    :param facts: The page facts.
    :return: The name, or empty.
    """
    for line in facts.copyright_lines:
        if m := _COPYRIGHT_NAME_RE.search(line):
            name = m.group("name").strip(" ,.")
            if any(c.isalpha() for c in name):
                return name
    return ""


def has_street_address(facts: PageFacts, market: str) -> bool:
    """
    Whether the site publishes a street address (a number and a postcode or state), not just a town.

    :param facts: The page facts.
    :param market: ``nl`` or ``us`` (anything with a house number counts elsewhere).
    :return: Whether there is one.
    """
    if market == "nl":
        return any(_NL_ADDRESS_RE.search(a) for a in facts.addresses)
    if market == "us":
        return any(_US_ADDRESS_RE.search(a) for a in facts.addresses)
    return any(any(c.isdigit() for c in a) for a in facts.addresses)


def _malformed_kvk(texts: list[str]) -> str:
    for text in texts:
        for m in _KVK_ANY_RE.finditer(text):
            digits = re.sub(r"\D", "", m.group(1))
            if len(digits) != 8:
                return m.group(1).strip()
    return ""


class ServiceIdentitySkill(Skill):
    """KvK number, VAT number, company name and address (NL); company, address and licence number (US)."""

    name = "service_identity"
    description = "Checks what a service company's site says about who it is (KvK, licence, name, address)."
    page_types = SERVICE_SITES
    features = (
        "market_nl",
        "market_us",
        "kvk_shown",
        "kvk_malformed",
        "vat_shown",
        "name_shown",
        "address_shown",
        "licence_shown",
        "licence_claim_no_number",
        "names_differ",
        "complete",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Read the site's identity statements from the facts and the page text.

        :param state: Current graph state; uses ``facts``, ``cleaned_content`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        market = service_market(facts, state.get("url", ""))
        if market == "other":
            return self.not_applicable("Not a Dutch or US service company.")
        company = copyright_company(facts)
        names = [n for n in (facts.business_name, company) if n]
        name_shown = bool(names) or bool(facts.legal_form)
        address = has_street_address(facts, market)
        names_differ = (
            bool(facts.business_name)
            and bool(company)
            and not name_matches(facts.business_name, [company])
            and not name_matches(company, [facts.business_name])
        )
        observations: list[Observation] = []
        features: dict[str, float | bool] = {
            "market_nl": market == "nl",
            "market_us": market == "us",
            "name_shown": name_shown,
            "address_shown": address,
            "names_differ": names_differ,
        }
        if names_differ:
            observations.append(
                Observation(
                    type="names_differ", evidence=f'Names two companies: "{facts.business_name}" and "{company}".'
                )
            )
        if not address:
            observations.append(Observation(type="no_street_address", evidence="No street address on the site."))
        if market == "nl":
            texts = [*facts.legal_mentions, state.get("cleaned_content", "")]
            malformed = "" if facts.kvk_numbers else _malformed_kvk(texts)
            features |= {"kvk_shown": bool(facts.kvk_numbers), "kvk_malformed": bool(malformed)}
            features["vat_shown"] = bool(facts.vat_ids)
            features["complete"] = bool(facts.kvk_numbers) and name_shown and address
            if facts.kvk_numbers:
                observations.append(Observation(type="kvk", evidence=f"KvK number shown: {facts.kvk_numbers[0]}."))
            elif malformed:
                observations.append(
                    Observation(type="kvk_malformed", evidence=f"The KvK number shown ({malformed}) isn't 8 digits.")
                )
            else:
                observations.append(Observation(type="no_kvk", evidence="No KvK number on the site."))
        else:
            claims = any(_LICENSED_RE.search(c) for c in facts.certifications)
            licences = facts.licences
            features |= {"licence_shown": bool(licences), "licence_claim_no_number": claims and not licences}
            features["complete"] = name_shown and address and not (claims and not licences)
            if licences:
                shown = ", ".join(f"{lic.state} {lic.number}".strip() for lic in licences[:_SHOWN])
                observations.append(Observation(type="licence", evidence=f"Licence number shown: {shown}."))
            elif claims:
                observations.append(
                    Observation(type="licence_no_number", evidence="Says it is licensed, but shows no licence number.")
                )
        if name_shown:
            observations.append(Observation(type="company", evidence=f"Company: {(names or [facts.legal_form])[0]}."))
        return self.output(features, observations)
