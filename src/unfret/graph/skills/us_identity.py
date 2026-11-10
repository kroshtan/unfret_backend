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

from unfret.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState
from unfret.market import shop_market

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
_FORM = r"LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|LLP|Ltd\.?"
# A word of a company name ("Acme", "ACME", "J&J", "Co.", "Wix.com", "5econds"; not a number like a year), never the
# company form itself, so that "Wix.com Ltd. Square, Inc." is two companies; "&", "and" and "of" join two words.
_WORD = rf"(?!(?:{_FORM})(?![A-Za-z]))(?:[A-Z]|\d+[A-Za-z])[A-Za-z0-9&'.-]*"


def _name(gap: str) -> str:
    return rf"{_WORD}(?:{gap}(?:(?:&|and|of){gap})?{_WORD}){{0,4}}"


# A footer's copyright line: "© 2026 Acme Outdoor LLC", "Copyright © 2024-2026 Acme Goods, Inc.", "©2025 ACME LLC".
# The page text breaks the line around every inline tag ("©\n2026\n, \nAcme \nOutdoor\n \nLLC"); the name starts
# right after the year, so its words may be on separate lines.
_COPYRIGHT_ENTITY_RE = re.compile(
    rf"(?:©|\([cC]\)|\b(?i:copyright)\b)(?:\s*©)?[\s,]*(?:\d{{4}}(?:\s*[-–]\s*\d{{4}})?[\s,]*)?"
    rf"(?P<name>{_name(r'\s+')}),?\s+(?:{_FORM})(?![A-Za-z])"
)
# A company named anywhere else. Its words share a line: across lines, menu items and table labels ("About Us",
# "Manufacturer") would join the name.
_ENTITY_RE = re.compile(rf"\b(?P<name>{_name(r'[ \t]+')}),?[ \t]+(?:{_FORM})(?![A-Za-z])")
# Sentence and label words that don't start a name ("Sold By Acme LLC", "We Are Not An LLC"); a name made only of
# them ("Our Company LLC") is no name.
_LEAD_WORDS = frozenset(
    {"a", "an", "the", "we", "are", "is", "was", "not", "our", "your", "sold", "by", "from", "of", "with", "at", "why"}
    | {"choose", "form", "forming", "become", "be", "copyright", "all", "rights", "reserved", "about", "us", "contact"}
    | {"note", "operated", "owned", "powered", "built", "designed", "developed", "hosted", "website", "company"}
    | {"business", "store", "shop", "brand", "seller", "vendor", "manufacturer", "co", "pte"}
)
# Platforms, payment providers and other services a shop's footer or cookie notice credits by their company name,
# and their subsidiaries ("Google Ireland Ltd", "Amazon.com Services LLC").
_SERVICES = frozenset(
    {"shopify", "wix", "squarespace", "bigcommerce", "automattic", "wordpress", "woocommerce", "godaddy", "ecwid"}
    | {"weebly", "magento", "square", "block", "paypal", "stripe", "klarna", "afterpay", "affirm", "sezzle", "visa"}
    | {"mastercard", "discover", "american express", "adobe", "amazon", "google", "alphabet", "meta", "facebook"}
    | {"instagram", "tiktok", "pinterest", "snap", "twitter", "x corp", "youtube", "microsoft", "apple", "salesforce"}
    | {"klaviyo", "yotpo", "trustpilot", "cloudflare"}
)
_SUBSIDIARY_WORDS = frozenset(
    {"ireland", "usa", "us", "uk", "europe", "eu", "international", "services", "payments", "commerce"}
    | {"technologies", "platforms", "operations"}
)
# "Powered by Shopify Inc.", "Website by Pixel Studio LLC": a credit names the platform or agency, not the shop.
_CREDIT_RE = re.compile(
    r"\b(?:powered|built|designed|developed|hosted|website|site|theme|payments?|processed)\s+(?:by|with|on)\s*$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")
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
    return Identity(
        street_addresses=[a for a in addresses if not _MAILBOX_RE.search(a)],
        mailbox_addresses=[a for a in addresses if _MAILBOX_RE.search(a)],
        entity=_entity(text),
        phone=_PHONE_RE.search(text) is not None,
    )


def _entity(text: str) -> str:
    """
    The shop's company: from a copyright line if there is one (the surest place), else the first company named.

    :param text: Page text.
    :return: The company with its form ("Acme Outdoor LLC"), or empty if there is none.
    """
    for pattern in (_COPYRIGHT_ENTITY_RE, _ENTITY_RE):
        for m in pattern.finditer(text):
            if entity := _shop_company(text, m):
                return entity
    return ""


def _shop_company(text: str, m: re.Match[str]) -> str:
    """
    The company a match names, without the words in front of it; empty if that is no name or not the shop's.

    :param text: The text that was matched.
    :param m: A match of a company pattern, with a ``name`` group.
    :return: The company on one line ("Acme Outdoor LLC"), or empty.
    """
    words = list(re.finditer(r"\S+", m.group("name")))
    lead = 0
    while lead < len(words) and words[lead].group().strip(".,'").lower() in _LEAD_WORDS:
        lead += 1
    if lead == len(words):
        return ""
    if lead and words[lead - 1].group() == "The":  # "The Acme Company LLC" is a name of its own
        lead -= 1
    start = m.start("name") + words[lead].start()
    if _is_service(text[start : m.end("name")]) or _CREDIT_RE.search(text[max(start - 40, 0) : start]):
        return ""
    return _WS_RE.sub(" ", text[start : m.end()]).replace(" ,", ",")


def _is_service(name: str) -> bool:
    """
    Whether a company name is a platform, payment provider or other service, or one of their subsidiaries.

    :param name: The name without its form ("Shopify", "Google Ireland").
    :return: Whether it names a service rather than a shop.
    """
    name = _WS_RE.sub(" ", name).lower().removeprefix("the ").replace(".com", "").strip(" .,")
    if name in _SERVICES:
        return True
    first, _, rest = name.partition(" ")
    return first in _SERVICES and bool(rest) and set(rest.split()) <= _SUBSIDIARY_WORDS


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
