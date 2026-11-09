"""
Fake discounts: implausible reductions, reference "was" prices, endless sales, and "was" prices never charged.

The last needs the price store to have a month of observations for the product. Records the page's price in the
anonymous price store (:mod:`veridex.price_store`) on the way.
"""

import re

from veridex import price_store
from veridex.config import settings
from veridex.graph.skills import Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState

# A reduction this deep is practically always measured against a price nobody paid.
IMPLAUSIBLE_DISCOUNT = 0.7
# A "was" price this much above the lowest price of the last 30 days was not the price before the reduction.
_ABOVE_LOW = 1.05

_PRICE = r"(?:[€$£]\s*\d|\d[\d.,]*\s*(?:€|eur|euro|usd|gbp)\b)"
# "Was" prices that are a reference (recommended retail, "usually", "elsewhere", "value"), not this shop's own
# earlier price. Plain "was"/"van" is the honest label and doesn't count; "van … voor …" only with a vague anchor.
_ANCHOR_RE = re.compile(
    rf"\b(adviesprijs|adviesverkoopprijs|vergelijkbare? (?:waarde|prijs)|meestal|normaal|elders|winkelwaarde"
    rf"|rrp|msrp|uvp|unverbindliche preisempfehlung|recommended retail price|compare at|comparable value"
    rf"|retail value|elsewhere|usually|typical price|list price|prix conseillé)\b\s*:?\s*(?:{_PRICE})",
    re.IGNORECASE,
)
# Marketplaces print their own reference-price label on every discounted listing (Amazon's "List Price" and
# "Typical price", bol.com's "Adviesprijs"), whatever the seller does, so on their pages it says nothing by itself.
# A made-up reference price there is caught by the price history (part 2) instead.
_OWN_LABELS: dict[str, frozenset[str]] = {
    "amazon": frozenset({"list price", "typical price"}),
    "bol.com": frozenset({"adviesprijs", "adviesverkoopprijs"}),
    "walmart": frozenset({"list price", "typical price"}),
}
_PERMANENT_SALE_RE = re.compile(
    r"\b(opheffingsuitverkoop|totale uitverkoop|alles moet weg|wegens opheffing|laatste kans|closing down sale"
    r"|everything must go|store closing|liquidation sale|going out of business|räumungsverkauf|alles muss raus"
    r"|liquidation totale|always on sale|permanent sale|altijd korting|elke dag uitverkoop)\b",
    re.IGNORECASE,
)


class PricingSkill(Skill):
    """Fake-discount signals from the facts and text, and the product's own price history."""

    name = "pricing"
    description = "Checks whether a discount is plausible and whether the 'was' price was ever charged."
    features = ("implausible_discount", "reference_anchor", "permanent_sale", "history_days", "was_above_30d_low")

    def __init__(self, *, history: bool = True) -> None:
        """
        Configure the skill.

        :param history: Whether to record the price and compare it with the product's price history (a lookup in
            Unfret's own price store). The quick tier turns it off and keeps the on-page checks.
        """
        self.history = history

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Read the discount, its label and sale wording; record the price and compare with its history.

        :param state: Current graph state; uses ``facts``, ``cleaned_content`` and ``url``.
        :return: The skill's features and findings.
        """
        facts, text = state["facts"], state["cleaned_content"]
        features: dict[str, float | bool] = {}
        observations: list[Observation] = []

        discount = 0.0
        if facts.price is not None and facts.list_price and facts.list_price > facts.price:
            discount = (facts.list_price - facts.price) / facts.list_price
        if discount >= IMPLAUSIBLE_DISCOUNT:
            features["implausible_discount"] = True
            observations.append(
                Observation(type="implausible_discount", evidence=f"{discount:.0%} off {facts.list_price:g}.")
            )
        own = _OWN_LABELS.get(facts.platform, frozenset())
        anchors = (m for m in _ANCHOR_RE.finditer(text) if m.group(1).lower() not in own)
        if discount > 0 and (anchor := next(anchors, None)):
            features["reference_anchor"] = True
            observations.append(Observation(type="reference_anchor", evidence=f'"{anchor.group(1)}"'))
        if state.get("page_type") == "storefront" and (sale := _PERMANENT_SALE_RE.search(text)):
            features["permanent_sale"] = True
            observations.append(Observation(type="permanent_sale", evidence=f'"{sale.group(1)}"'))

        if self.history and settings.price_observations:
            seen = await price_store.observe(state.get("url", ""), facts)
            if seen is not None:
                _, history = seen
                span = history.span_days(price_store.today())
                features["history_days"] = min(span, price_store.RETENTION_DAYS) / price_store.HISTORY_WINDOW_DAYS
                # Part 2 is gated: only a product observed for the whole reference period has a 30-day low.
                if (
                    span >= settings.price_history_min_days
                    and history.lowest is not None
                    and facts.list_price
                    and facts.list_price > history.lowest * _ABOVE_LOW
                ):
                    features["was_above_30d_low"] = True
                    observations.append(
                        Observation(
                            type="was_never_charged",
                            evidence=f"Lowest price in the last 30 days: {history.lowest:g}; 'was' price: "
                            f"{facts.list_price:g}.",
                        )
                    )
        return self.output(features, observations)
