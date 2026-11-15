"""
Off-platform push: the ad moves the deal away from the platform and its payment protection.

Almost every non-delivery or advance-fee ad does it: get in touch by WhatsApp, pay by bank transfer, gift cards,
crypto or a payment app, pay a deposit first, "shipping only, no pickup", "I'm abroad / on a ship", "no payment
through the platform". Rules first, in Dutch and English (Marktplaats and 2dehands are Dutch), over the ad's own
text (:attr:`ListingDetails.description`), so the platform's own safety tips ("never pay in advance") don't count;
a payment the ad rules out ("no bank transfer") doesn't either. Every finding quotes the ad. The content skill's
LLM read adds its own judgement of the wording (``content.off_platform_push``), which the verdict weighs too.

Without the ad's own text (an unknown layout), only the payment wording is read from the page text, never contact
methods: the platform's share buttons ("share on WhatsApp") would look like one.
"""

import re

from unfret.facts.p2p import CONTACT_PATTERNS, EMAIL_RE, PAYMENT_PATTERNS, negated, phones_in
from unfret.graph.skills import P2P_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

_SHIPPING_ONLY_RE = re.compile(
    r"alleen (?:verzenden|verzending|opsturen)|enkel verzenden|uitsluitend verzenden|niet (?:worden )?opgehaald"
    r"|geen ophalen|ophalen (?:is )?niet mogelijk|kan niet (?:worden )?opgehaald|shipping only|ship only|only ship"
    r"|no (?:local )?pick ?-?ups?|pick ?-?up (?:is )?not (?:possible|available)|(?:cannot|can't|unable to) meet"
    r"|no meet ?ups?|can't show|cannot show|nur versand|keine abholung",
    re.IGNORECASE,
)
_ABROAD_RE = re.compile(
    r"in het buitenland|op (?:een )?(?:schip|boorplatform|booreiland)|op zee(?![a-z])|verhuisd naar het buitenland"
    r"|(?:i am|i'm|currently|now|temporarily) (?:abroad|overseas|deployed|offshore|out of (?:the )?(?:country|state))"
    r"|out of the country|working (?:on an oil rig|offshore|overseas|abroad)|on a (?:ship|rig|cruise|mission)"
    r"|military (?:deployment|base)|deployed overseas|missionary|moved (?:abroad|overseas)|im ausland",
    re.IGNORECASE,
)
_REFUSES_PROTECTION_RE = re.compile(
    r"geen gelijk oversteken|niet via (?:marktplaats|2dehands|vinted|ebay|facebook) (?:betalen|betaling|kopen)"
    r"|buiten (?:marktplaats|2dehands|vinted|ebay) om|geen (?:betalen|betaling) via (?:marktplaats|2dehands|vinted)"
    r"|geen kopersbescherming|no (?:buyer|purchase) protection|outside (?:of )?(?:ebay|vinted|the platform"
    r"|marketplace|facebook)|off[- ](?:ebay|platform|site)|not (?:through|via) (?:ebay|paypal|vinted|the platform)"
    r"|no paypal goods(?: and | & )services|no (?:goods (?:and|&) services|g&s)",
    re.IGNORECASE,
)
_UNTRACEABLE = frozenset({"gift_card", "crypto", "money_transfer", "payment_app"})
_TRANSFER = frozenset({"bank_transfer", "payment_link"})
# A transfer or payment link at the handover ("Tikkie bij ophalen", "cash or Venmo at pickup") is paying on the spot.
_ON_COLLECTION_RE = re.compile(
    r"bij (?:het )?(?:ophalen|afhalen|overdracht)|at (?:the )?pick ?-?up|on (?:pick ?-?up|collection)|in person"
    r"|when (?:you )?(?:collect|pick)|bei abholung|à la remise",
    re.IGNORECASE,
)
_PAYMENT_LABELS = {
    "bank_transfer": "bank transfer",
    "payment_link": "a payment request link",
    "gift_card": "gift cards",
    "crypto": "crypto",
    "money_transfer": "a money transfer service",
    "payment_app": "a payment app without buyer protection",
}
_CONTACT_LABELS = {"whatsapp": "WhatsApp", "telegram": "Telegram", "phone": "phone", "email": "e-mail"}
_QUOTE_CHARS = 150
_FALLBACK_CHARS = 6_000
_SENTENCE_END = ".!?\n"


def quote(text: str, start: int, end: int) -> str:
    """
    The sentence around a match, at most :data:`_QUOTE_CHARS` characters, for evidence.

    :param text: The ad text.
    :param start: Where the match starts.
    :param end: Where it ends.
    :return: The quote.
    """
    lo = max((text.rfind(c, 0, start) for c in _SENTENCE_END), default=-1) + 1
    ends = [i for c in _SENTENCE_END if (i := text.find(c, end)) >= 0]
    hi = min(ends) + 1 if ends else len(text)
    sentence = " ".join(text[lo:hi].split())
    if len(sentence) <= _QUOTE_CHARS:
        return sentence
    middle = " ".join(text[max(start - 60, 0) : end + 60].split())
    return f"…{middle[:_QUOTE_CHARS]}…"


def _first(pattern: re.Pattern[str], text: str, *, negatable: bool = True) -> re.Match[str] | None:
    return next((m for m in pattern.finditer(text) if not (negatable and negated(text, m.start()))), None)


def _on_collection(text: str, match: re.Match[str]) -> bool:
    return bool(_ON_COLLECTION_RE.search(quote(text, *match.span())))


class OffPlatformSkill(Skill):
    """Wording in the ad that takes the contact or the payment off the platform."""

    name = "off_platform"
    description = "Flags ads that move the deal off the platform: other contact, payment outside it, deposits."
    page_types = P2P_PAGES
    features = (
        "text_read",
        "contact_outside",
        "bank_transfer",
        "untraceable_payment",
        "advance_payment",
        "shipping_only",
        "abroad",
        "protection_refused",
        "signals",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Match the ad text against the rules and quote what matched.

        :param state: Current graph state; uses ``facts`` and ``cleaned_content``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        listing = facts.listing
        own = listing.description if listing is not None else ""
        text = own or state.get("cleaned_content", "")[:_FALLBACK_CHARS]
        observations: list[Observation] = []
        fired: set[str] = set()

        def found(kind: str, match: re.Match[str] | None, evidence: str) -> None:
            if match is not None:
                fired.add(kind)
                observations.append(Observation(type=kind, evidence=f'{evidence}: "{quote(text, *match.span())}".'))

        contacts = self._contacts(own, listing.contact_methods if listing is not None else [])
        if contacts:
            kind, match = contacts[0]
            names = " and ".join(dict.fromkeys(_CONTACT_LABELS.get(k, k) for k, _ in contacts))
            if match is not None:
                found("contact_outside", match, f"The ad asks to be contacted by {names}")
            else:
                fired.add("contact_outside")
                observations.append(
                    Observation(type="contact_outside", evidence=f"The ad mentions contact by {names}.")
                )
        # The ad's own words decide; the extractor's list only when the text didn't come along.
        requested = set(listing.payment_requests) if listing is not None and not own else set()
        for name, pattern in PAYMENT_PATTERNS:
            match = _first(pattern, text)
            if match is not None and name in _TRANSFER and _on_collection(text, match):
                continue  # paid at the handover, with the item in hand
            if match is None and name not in requested:
                continue
            kind = (
                "advance_payment"
                if name == "deposit"
                else "untraceable_payment"
                if name in _UNTRACEABLE
                else "bank_transfer"
            )
            if kind in fired:
                continue
            evidence = (
                "The ad asks for payment up front"
                if kind == "advance_payment"
                else f"The ad asks for payment by {_PAYMENT_LABELS.get(name, name)}"
            )
            if match is not None:
                found(kind, match, evidence)
            else:
                fired.add(kind)
                observations.append(Observation(type=kind, evidence=f"{evidence}."))
        found("shipping_only", _first(_SHIPPING_ONLY_RE, text, negatable=False), "The seller won't meet in person")
        found("abroad", _first(_ABROAD_RE, text), "The seller says they are abroad")
        found(
            "protection_refused",
            _first(_REFUSES_PROTECTION_RE, text, negatable=False),
            "The ad refuses the platform's payment",
        )
        return self.output(
            {
                "text_read": bool(own),
                "contact_outside": "contact_outside" in fired,
                "bank_transfer": "bank_transfer" in fired,
                "untraceable_payment": "untraceable_payment" in fired,
                "advance_payment": "advance_payment" in fired,
                "shipping_only": "shipping_only" in fired,
                "abroad": "abroad" in fired,
                "protection_refused": "protection_refused" in fired,
                "signals": len(fired),
            },
            observations,
            note="" if observations else "The ad keeps the deal on the platform.",
        )

    @staticmethod
    def _contacts(text: str, kinds: list[str]) -> list[tuple[str, re.Match[str] | None]]:
        """The ways of contact the ad's own text mentions, with where (or ``None`` when only the facts say so)."""
        found: list[tuple[str, re.Match[str] | None]] = []
        for name, pattern in CONTACT_PATTERNS:
            if m := pattern.search(text):
                found.append((name, m))
        if not any(k == "phone" for k, _ in found) and phones_in(text):
            m = next(re.finditer(re.escape(phones_in(text)[0]), text))
            found.append(("phone", m))
        if not any(k == "email" for k, _ in found) and (m := EMAIL_RE.search(text)):
            found.append(("email", m))
        named = {k for k, _ in found}
        found += [(k, None) for k in kinds if k not in named]
        return found
