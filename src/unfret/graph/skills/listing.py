"""Deterministic listing signals from the page facts: brand name, discount, title, seller and origin."""

import math
import re

from unfret.facts import PageFacts
from unfret.graph.skills import NOT_APPLICABLE, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState

_PINYIN_RE = re.compile(r"zh|xi|qi|xu|ao|uo|ang|eng|ong|iu|ui")
_CONSONANT_RUN_RE = re.compile(r"[bcdfghjklmnpqrstvwxz]{4,}")
_CHINA_RE = re.compile(r"\b(?:china|cn|shenzhen|guangzhou|yiwu|hong kong|dongguan|ningbo)\b", re.IGNORECASE)
_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def generated_brand_score(brand: str) -> float:
    """
    Score how much a brand name looks machine-generated ("ZHUIBO", "XIEERDUO", "HUAXINGDA"), from 0 to 1.

    Generated marketplace brands are typically a single all-caps token of 5–12 letters with pinyin-like letter
    pairs or rare letters. This is a weak signal on its own (real brands can look like this too); the content
    skill's ``brand_recognized`` answers whether the brand is actually known.

    :param brand: The brand or store name.
    :return: The score.
    """
    name = brand.strip()
    lowered = name.lower()
    if not name:
        return 0.0
    score = 0.0
    if len(name.split()) == 1 and name.isalpha() and 5 <= len(name) <= 12:
        score += 0.35
        if name.isupper():
            # Shops and marketplaces write well-known brands in capitals too: the shape alone stays below the 0.6 of
            # a "looks generated" finding; it takes the letter patterns below as well.
            score += 0.15
    if _PINYIN_RE.search(lowered):
        score += 0.2
    if any(c in lowered for c in "xzq"):
        score += 0.1
    if _CONSONANT_RUN_RE.search(lowered):
        score += 0.1
    return round(min(score, 1.0), 2)


def _normalise(text: str) -> str:
    return _NORMALISE_RE.sub("", text.lower())


def resold(facts: PageFacts) -> bool:
    """
    Whether a marketplace listing is sold by someone other than the brand: a reseller.

    The marketplace selling it itself (its own retail arm named as the seller) is the retailer, not a reseller.

    :param facts: The listing's facts.
    :return: ``True`` when the seller is neither the brand nor the marketplace.
    """
    brand, seller = _normalise(facts.brand), _normalise(facts.seller)
    if not brand or not seller or brand in seller or seller in brand:
        return False
    platform = _normalise(facts.platform).removesuffix("com")
    return not (platform and platform in seller)


class ListingSkill(Skill):
    """Brand name, discount depth, title stuffing, seller vs brand, and shipping origin, from the page facts."""

    name = "listing"
    description = "Checks the brand name, discount, title, seller and shipping origin in the page facts."
    features = (
        "generated_brand",
        "discount_pct",
        "keyword_stuffed",
        "title_length",
        "seller_not_brand",
        "ships_from_china",
        "variations",
        "log_rating_count",
        "rating",
        "is_marketplace",
        "is_shopify",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Derive listing features from ``facts``.

        :param state: Current graph state; uses ``facts`` and ``page_type``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        observations: list[Observation] = []

        brand_score = generated_brand_score(facts.brand)
        if brand_score >= 0.6:
            observations.append(Observation(type="brand_name", evidence=f'The brand "{facts.brand}" looks generated.'))

        discount = 0.0
        if facts.price is not None and facts.list_price and facts.list_price > facts.price:
            discount = (facts.list_price - facts.price) / facts.list_price
            if discount >= 0.5:
                observations.append(
                    Observation(
                        type="discount",
                        evidence=f"Price {facts.price:g} against a 'was' price of {facts.list_price:g} "
                        f"({discount:.0%} off).",
                    )
                )

        stuffed = len(facts.title) >= 120 and facts.title.count(",") >= 3
        if stuffed:
            observations.append(
                Observation(type="title", evidence=f"Keyword-stuffed title ({len(facts.title)} chars).")
            )

        marketplace = state.get("page_type") == "aggregate_listing"
        seller_not_brand = marketplace and resold(facts)
        if seller_not_brand:
            observations.append(
                Observation(type="seller", evidence=f'Sold by "{facts.seller}", not the brand "{facts.brand}".')
            )

        china = bool(_CHINA_RE.search(facts.ships_from))
        if china:
            observations.append(Observation(type="ships_from", evidence=f"Ships from {facts.ships_from}."))

        # A shop's own page names the shop as the seller of every brand it stocks: that is what a retailer is, so
        # there it says nothing (neutral to the scorer).
        seller = (
            {"seller_not_brand": seller_not_brand} if marketplace else {f"seller_not_brand.{NOT_APPLICABLE}": True}
        )
        return self.output(
            {
                **seller,
                "generated_brand": brand_score,
                "discount_pct": discount,
                "keyword_stuffed": stuffed,
                "title_length": min(len(facts.title), 400) / 400,
                "ships_from_china": china,
                "variations": min(len(facts.variations), 50),
                "log_rating_count": math.log10(1 + (facts.rating_count or 0)),
                "rating": facts.rating if facts.rating is not None else 0.0,
                "is_marketplace": marketplace,
                "is_shopify": facts.platform == "shopify",
            },
            observations,
        )
