"""
Shipping and return terms that give a dropshipper away.

Long overseas delivery, returns abroad, a withheld return address, steep restocking fees, and a "Made in USA" claim
on goods that ship from overseas. Each signal needs the wording itself on the page, never an absence: a shop whose
policies aren't in the page text is not marked down. Delivery ranges count only next to shipping words, so "30-day
returns" or "14-day withdrawal" never read as delivery times.
"""

import re

from veridex.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState
from veridex.market import shop_market

# A delivery range ("7-20 business days", "2–4 weeks") whose upper end is this long means overseas shipping.
_LONG_DAYS = 15
_RANGE_RE = re.compile(
    r"(?<![\d/])(\d{1,2})\s*(?:-|–|—|to|tot|bis|à)\s*(\d{1,2})\s*(business\s+|working\s+|werk|arbeits)?"
    r"(days?|dagen|tage|jours|weeks?|weken|wochen|semaines)\b",
    re.IGNORECASE,
)
_SHIPPING_WORDS_RE = re.compile(
    r"ship|deliver|arriv|transit|dispatch|verzend|lever|bezorg|versand|liefer|livraison|exp[ée]di", re.IGNORECASE
)
_RETURN_WORDS_RE = re.compile(r"return|refund|warrant|guarantee|retour|garantie|widerruf|rückgabe", re.IGNORECASE)
_ABROAD_RE = re.compile(
    r"\b(?:return(?:s|ed)?|(?:sen[dt]|ship(?:ped)?|mail(?:ed)?)\b[^.]{0,30}?\bback)\b"
    r"[^.]{0,80}?\b(?:to|in)\s+(?:our\s+)?(?:\w+\s+){0,2}?"
    r"(China|Hong Kong|Shenzhen|Guangzhou|Yiwu|Dongguan|Ningbo)\b",
    re.IGNORECASE,
)
# Dropshippers don't stock the goods, so they hand out a return address only on request, and ask buyers not to
# use the sender address on the parcel (the supplier's).
_WITHHELD_RE = re.compile(
    r"(?:contact us|e-?mail us|reach out)[^.]{0,60}?(?:for|to (?:get|receive|obtain))\s+(?:the|a|our)\s+return address"
    r"|return address (?:will be|is) (?:provided|sent|given)"
    r"|do not (?:send|return|ship)[^.]{0,60}?(?:address on the (?:package|parcel|label)|sender'?s? address)",
    re.IGNORECASE,
)
_RESTOCKING_RE = re.compile(r"restocking fee[^.%]{0,40}?(\d{1,2})\s?%|(\d{1,2})\s?%\s+restocking", re.IGNORECASE)
_MAX_RESTOCKING_PCT = 20
_MADE_IN_USA_RE = re.compile(r"\bmade in (?:the )?(?:usa|u\.s\.a\.?|united states|america)\b", re.IGNORECASE)
_CHINA_RE = re.compile(r"\b(?:china|cn|shenzhen|guangzhou|yiwu|hong kong|dongguan|ningbo)\b", re.IGNORECASE)
_WINDOW = 60
_MAX_TEXT = 200_000


def longest_delivery_days(text: str) -> int:
    """
    The longest delivery time the text states, in days, counting only ranges next to shipping words.

    :param text: Page text.
    :return: The upper end of the longest delivery range, in days (weeks count as 7 days), or 0 when none.
    """
    longest = 0
    for m in _RANGE_RE.finditer(text):
        low, high = int(m.group(1)), int(m.group(2))
        if not 0 < low <= high:
            continue
        # The sentence around the range must be about shipping, and not lead with returns ("Returns accepted
        # 15-30 days after delivery").
        lo = max(m.start() - _WINDOW, 0)
        start = text.rfind(".", lo, m.start()) + 1 or lo
        end = text.find(".", m.end(), m.end() + _WINDOW)
        sentence = text[start : end if end != -1 else m.end() + _WINDOW]
        if not _SHIPPING_WORDS_RE.search(sentence) or _RETURN_WORDS_RE.search(text[start : m.start()]):
            continue
        weeks = m.group(4).lower().startswith(("week", "woch", "sem"))
        longest = max(longest, high * 7 if weeks else high)
    return longest


class ShopPoliciesSkill(Skill):
    """Overseas delivery times, returns abroad or without an address, restocking fees, doubtful origin claims."""

    name = "shop_policies"
    description = "Reads the shipping and return terms for signs of overseas dropshipping."
    page_types = STOREFRONTS
    features = ("long_delivery", "returns_abroad", "return_address_withheld", "restocking_fee_high", "origin_doubt")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Read the delivery and return wording in the page text.

        :param state: Current graph state; uses ``facts``, ``cleaned_content`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        text = state["cleaned_content"][:_MAX_TEXT]
        observations: list[Observation] = []

        days = longest_delivery_days(text)
        long_delivery = days >= _LONG_DAYS
        if long_delivery:
            observations.append(Observation(type="long_delivery", evidence=f"Delivery can take up to {days} days."))
        abroad = _ABROAD_RE.search(text)
        if abroad:
            observations.append(Observation(type="returns_abroad", evidence=f"Returns go to {abroad.group(1)}."))
        withheld = _WITHHELD_RE.search(text)
        if withheld:
            observations.append(Observation(type="return_address_withheld", evidence=f'"{withheld.group()[:150]}"'))
        restocking = max((int(m.group(1) or m.group(2)) for m in _RESTOCKING_RE.finditer(text)), default=0)
        if restocking > _MAX_RESTOCKING_PCT:
            observations.append(
                Observation(type="restocking_fee", evidence=f"Returns cost a {restocking}% restocking fee.")
            )
        # "Made in USA" must be all or virtually all made in the US (FTC); goods that ship from overseas aren't.
        overseas = bool(_CHINA_RE.search(facts.ships_from)) or abroad is not None or long_delivery
        origin_doubt = (
            shop_market(facts, state.get("url", ""), state.get("cleaned_content", "")) == "us"
            and overseas
            and bool(_MADE_IN_USA_RE.search(text))
        )
        if origin_doubt:
            observations.append(
                Observation(type="origin_doubt", evidence='Says "Made in USA", but ships from overseas.')
            )
        return self.output(
            {
                "long_delivery": long_delivery,
                "returns_abroad": abroad is not None,
                "return_address_withheld": withheld is not None,
                "restocking_fee_high": restocking > _MAX_RESTOCKING_PCT,
                "origin_doubt": origin_doubt,
            },
            observations,
        )
