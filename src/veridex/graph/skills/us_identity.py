"""
US merchant identity: whether a US shop says who it is and where it is, from what the page itself shows.

US law has no imprint or VAT ID, so the honest equivalents are a named company (LLC, Inc., Corp.) and a physical
business address with a state and ZIP code. Only what is found counts: a shop that shows neither isn't marked down
here (the page text may simply not include its footer); a shop whose only address is a mailbox (PMB, PO Box) is
reported as a fact. "Suite" is an ordinary office address and never counts as a mailbox.

There is no free, terms-permitted API for the 50 state business registries: OpenCorporates' free keys are only for
share-alike open-data projects (its data is ODbL), and a commercial service needs its paid non-share-alike plan
(from about £2,250 a year), so registrations are not looked up.
"""

import re
from dataclasses import dataclass

from veridex.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState
from veridex.market import shop_market

_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK"
    "|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|PR"
)
_STREET_SUFFIX = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Parkway|Pkwy|Highway|Hwy|Place|Pl"
    r"|Plaza|Square|Sq|Terrace|Ter|Circle|Cir|Trail|Trl|Pike|Broadway"
)
# "1200 Main Street, Suite 4, Austin, TX 78701" or "PO Box 123, Reno, NV 89501". Bounded gaps keep it linear.
_ADDRESS_RE = re.compile(
    rf"(?:\b\d{{1,6}}\s+(?:[A-Za-z0-9.'-]+\s+){{0,5}}(?:{_STREET_SUFFIX})\b\.?"
    rf"|\b(?:P\.?\s?O\.?\s?Box|PMB|Private Mail\s?box)\s*#?\s*\d+)"
    rf"[^\n]{{0,80}}?\b(?:{_STATES}),?\s+\d{{5}}(?:-\d{{4}})?\b"
)
_MAILBOX_RE = re.compile(r"\b(?:P\.?\s?O\.?\s?Box|PMB|Private Mail\s?box)\b", re.IGNORECASE)
_ENTITY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9&'.-]*(?:\s+[A-Z][A-Za-z0-9&'.-]*){0,4}),?\s+(LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?"
    r"|Corporation|LLP|Ltd\.?)(?![A-Za-z])"
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
_MAX_TEXT = 200_000


@dataclass(frozen=True)
class Identity:
    """What a page says about the business behind it."""

    street_addresses: list[str]
    mailbox_addresses: list[str]
    entity: str
    phone: bool


def find_identity(text: str) -> Identity:
    """
    Find the company name, US addresses and phone number in page text.

    :param text: Page text (only the first 200,000 characters are read).
    :return: What was found.
    """
    text = text[:_MAX_TEXT]
    addresses = [m.group().strip() for m in _ADDRESS_RE.finditer(text)][:10]
    entity = next((m for m in _ENTITY_RE.finditer(text) if not m.group(1).startswith("The ")), None)
    return Identity(
        street_addresses=[a for a in addresses if not _MAILBOX_RE.search(a)],
        mailbox_addresses=[a for a in addresses if _MAILBOX_RE.search(a)],
        entity=entity.group().strip() if entity else "",
        phone=_PHONE_RE.search(text) is not None,
    )


class UsIdentitySkill(Skill):
    """A named company, a US business address and phone number, and mailbox-only addresses, for US storefronts."""

    name = "us_identity"
    description = "Checks whether a US shop names its company and a physical US business address."
    page_types = STOREFRONTS
    features = ("us_address", "mailbox_only", "business_entity", "us_phone")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look for the company name, address and phone number in the page text and its legal snippets.

        :param state: Current graph state; uses ``facts``, ``cleaned_content`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        if shop_market(facts, state.get("url", ""), state.get("cleaned_content", "")) != "us":
            return self.not_applicable("Not a US shop.")
        found = find_identity(" ".join([state["cleaned_content"][:_MAX_TEXT], *facts.legal_mentions]))
        street, mailbox = found.street_addresses, found.mailbox_addresses
        features: dict[str, float | bool] = {
            "us_address": bool(street),
            "mailbox_only": bool(mailbox) and not street,
            "business_entity": bool(found.entity),
            "us_phone": found.phone,
        }
        observations: list[Observation] = []
        if street:
            observations.append(Observation(type="us_address", evidence=f"Business address: {street[0][:200]}"))
        elif mailbox:
            observations.append(Observation(type="mailbox_address", evidence=f"Only a mailbox: {mailbox[0][:200]}"))
        if found.entity:
            observations.append(Observation(type="business_entity", evidence=f"Company: {found.entity}"))
        return self.output(features, observations)
