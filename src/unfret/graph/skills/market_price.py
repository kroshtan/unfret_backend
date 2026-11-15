"""
A product priced far below what other shops charge for it, from Unfret's own anonymous observations.

Products with a barcode (GTIN/EAN) are compared across shops (:mod:`unfret.market_prices`): each other shop's median
price over the last 90 days, then the median of those. Conservative on purpose: it needs prices from at least
:data:`MIN_SHOPS` other shops in the same currency, and only a price at most :data:`FAR_BELOW` of that median
counts as far below (clearance sales and ordinary price differences stay well above it). Records today's price on the
way, unless observations are off.
"""

from unfret import market_prices
from unfret.config import settings
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

MIN_SHOPS = 3
FAR_BELOW = 0.4  # a price at most 40% of the market median (60% or more below it)


class MarketPriceSkill(Skill):
    """The page's price against other shops' prices for the same barcode."""

    name = "market_price"
    description = "Compares a barcoded product's price with what other shops charge for it."
    features = ("other_shops", "below_market", "far_below")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look the product up in the market store and record its price.

        :param state: Current graph state; uses ``url`` and ``facts``.
        :return: The skill's features and findings.
        """
        if not settings.price_observations:
            return self.output({}, note="Price observations are off.")
        facts = state["facts"]
        market = await market_prices.observe(state.get("url", ""), facts)
        if market is None or facts.price is None:
            return self.output({}, note="No barcode and price to compare.")
        features: dict[str, float | bool] = {"other_shops": market.shops}
        observations: list[Observation] = []
        if market.shops >= MIN_SHOPS and market.median:
            below = max(0.0, 1 - facts.price / market.median)
            features["below_market"] = round(below, 4)
            if facts.price <= FAR_BELOW * market.median:
                features["far_below"] = True
                observations.append(
                    Observation(
                        type="far_below_market",
                        evidence=f"Price {facts.price:g}; {market.shops} other shops charged a median of "
                        f"{market.median:g} for the same barcode in the last {market_prices.WINDOW_DAYS} days.",
                    )
                )
        return self.output(features, observations)
