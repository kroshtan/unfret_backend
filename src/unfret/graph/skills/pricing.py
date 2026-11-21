"""
Fake discounts: implausible reductions, reference "was" prices, endless sales, and "was" prices never charged.

The last needs the price store to have a month of observations for the product. Records the page's price in the
anonymous price store (:mod:`unfret.price_store`) on the way.

A marketplace's own reference-price label (Amazon's "List Price", "Adviesprijs", "UVP", "RRP", …) is a note
(``marketplace_reference``), not the ``reference_anchor`` reason: the marketplace prints it on every discounted
listing. It becomes the reason only once the price history shows the price was never charged.

So is a recommended-retail label ("UVP", "RRP", "Adviesprijs", …) on the brand's own shop (``brand_reference``):
there it is the maker's own recommended price. And so is one on a page that gives the lowest price of the last 30
days (the EU's price-reduction disclosure: "30 Tage Bestpreis", "Laagste prijs van de afgelopen 30 dagen", …) with
today's price at or below it (``disclosed_reference``). Any other web shop's label stays the reason.
"""

import re

from unfret import brands, price_store
from unfret.config import settings
from unfret.facts._parse import parse_number
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.skills.listing import generated_brand_score
from unfret.graph.state import AnalysisState
from unfret.net import hostname

# A reduction this deep is practically always measured against a price nobody paid.
IMPLAUSIBLE_DISCOUNT = 0.7
# A "was" price this much above the lowest price of the last 30 days was not the price before the reduction.
_ABOVE_LOW = 1.05

# A price with its currency before ("€ 14,39", "EUR 14.39", eBay's "US $14.39") or after the number ("14,39 €",
# "1.299,00 zł", "1 299 kr", the usual order in most of the EU), with "." or "," for decimals and "." "," or a space
# for thousands.
# A trailing sign is matched without a word boundary: "€" is no word character, so "14,39 €" ends in none.
_NUMBER = r"\d(?:[\d.,]|[ \u00a0\u202f]\d{3}(?!\d))*"
_PRICE = (
    rf"(?:(?:(?:\b(?:us|au|ca|c)\s?)?[€$£]|\b(?:eur|usd|gbp|chf|sek|pln)\s)\s*\d"
    rf"|{_NUMBER}\s*(?:[€$£]|(?:eur|euro|usd|gbp|chf|sek|nok|dkk|pln|zł|kr)(?!\w)))"
)
# Recommended-retail and list-price labels on Amazon's other EU locales and the big EU marketplaces: Italian,
# Spanish, Portuguese, Polish, Swedish, Danish, Norwegian, Finnish, Czech.
_EU_LABEL_LIST = (
    "prezzo consigliato",
    "prezzo di listino",
    "precio de venta recomendado",
    "precio recomendado",
    "p.v.p.r.",
    "pvpr",
    "preço recomendado",
    "cena katalogowa",
    "sugerowana cena",
    "rekommenderat pris",
    "rek. pris",
    "listpris",
    "vejledende pris",
    "veiledende pris",
    "suositushinta",
    "doporučená cena",
)
_EU_LABELS = "|".join(re.escape(label) for label in _EU_LABEL_LIST)
# "Was" prices that are a reference (recommended retail, "usually", "elsewhere", "value"), not this shop's own
# earlier price. Plain "was"/"van" is the honest label and doesn't count; "van … voor …" only with a vague anchor.
_ANCHOR_RE = re.compile(
    rf"\b(adviesprijs|adviesverkoopprijs|vergelijkbare? (?:waarde|prijs)|meestal|normaal|elders|winkelwaarde"
    rf"|rrp|msrp|uvp|unverbindliche preisempfehlung|unverb\. preisempf\.|recommended retail price|compare at"
    rf"|comparable value|retail value|elsewhere|usually|typical price|list price|prix conseillé"
    rf"|{_EU_LABELS})(?!\w)\s*:?\s*(?:{_PRICE})",
    re.IGNORECASE,
)
# Marketplaces print their own reference-price label on every discounted listing, whatever the seller does, so on
# their pages it is a note (``marketplace_reference``), not a reason. Amazon's in every locale its pages are read in
# ("List Price" and "Typical price" on .com, "RRP" on .co.uk, "UVP" or "Unverbindliche Preisempfehlung" on .de,
# "Adviesprijs" on .nl, "Prix conseillé" on .fr, "Prezzo consigliato" on .it, "Precio recomendado" or "PVPR" on
# .es, "Cena katalogowa" on .pl, "Rek. pris" on .se, …), bol.com's "Adviesprijs" and "Meestal", Walmart's, and
# eBay's struck "List price".
# A made-up reference price there is the reason only once the price history shows it was never charged (part 2).
_OWN_LABELS: dict[str, frozenset[str]] = {
    "amazon": frozenset(
        {
            "list price",
            "typical price",
            "rrp",
            "recommended retail price",
            "uvp",
            "unverbindliche preisempfehlung",
            "adviesprijs",
            "unverb. preisempf.",
            "prix conseillé",
            *_EU_LABEL_LIST,
        }
    ),
    "bol.com": frozenset({"adviesprijs", "adviesverkoopprijs", "meestal"}),
    "walmart": frozenset({"list price", "typical price"}),
    # eBay strikes its "List price" through above the price on every discounted listing.
    "ebay": frozenset({"list price"}),
}
# The labels that name the maker's recommended retail price, in every language read ("compare at", "usually",
# "elsewhere", "list price" and the like are the shop's own claims, not the maker's).
_RECOMMENDED_LABELS = frozenset(
    {
        "adviesprijs",
        "adviesverkoopprijs",
        "rrp",
        "msrp",
        "uvp",
        "unverbindliche preisempfehlung",
        "unverb. preisempf.",
        "recommended retail price",
        "prix conseillé",
        "prezzo consigliato",
        "precio de venta recomendado",
        "precio recomendado",
        "p.v.p.r.",
        "pvpr",
        "preço recomendado",
        "sugerowana cena",
        "rekommenderat pris",
        "rek. pris",
        "vejledende pris",
        "veiledende pris",
        "suositushinta",
        "doporučená cena",
    }
)
# Shop systems a brand runs its own shop on; marketplaces have their own labels (above).
_WEB_SHOPS = frozenset({"generic", "shopify", "woocommerce"})
# The lowest price of the last 30 days, as the EU's price-reduction rule (the Omnibus directive) has shops disclose
# it next to a reduced price, in the languages read: German, Dutch, English, French, Italian, Spanish, Portuguese,
# Polish, Swedish, Danish, Norwegian, Finnish, Czech. The price follows the label.
_THIRTY_DAY_LOW_RE = re.compile(
    r"(?:30[- ]?tage[- ]?(?:best|tiefst|niedrigst)preis|(?:best|tiefst)preis der letzten 30 tage"
    r"|niedrigster preis (?:der|in den|innerhalb der) letzten 30 tage[n]?"
    r"|laagste prijs (?:van |in )?(?:de )?(?:afgelopen|laatste) 30 dagen|30[- ]dagen[- ]laagste prijs"
    r"|lowest price (?:in|of|over|during) the (?:last|past|previous|prior) 30 days"
    r"|30[- ]day (?:low(?:est price)?|best price)"
    r"|prix le plus bas (?:pratiqué )?(?:des|au cours des|sur les|lors des) 30 derniers jours"
    r"|prezzo più basso (?:degli|negli|praticato negli) ultimi 30 giorni"
    r"|precio más bajo (?:de los|en los) últimos 30 días|preço mais baixo (?:dos|nos) últimos 30 dias"
    r"|najniższa cena (?:z|w ciągu) (?:ostatnich )?30 dni(?: przed obniżką)?"
    r"|lägsta pris(?:et)? (?:de|under de) senaste 30 dagarna|laveste pris (?:i |de )*seneste 30 dage"
    r"|laveste pris (?:de |i )*siste 30 dager|alin hinta (?:viimeisen|edellisen) 30 päivän aikana"
    r"|30 päivän alin hinta|nejnižší cena (?:za )?(?:posledních )?30 dn[ií])"
    rf"(?!\w)\s*\(?\s*:?\s*({_PRICE}[\d.,]*)",
    re.IGNORECASE,
)
# Today's price may be this much above the disclosed 30-day low (rounding) and still be at it.
_AT_LOW = 1.005
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
        anchors = list(_ANCHOR_RE.finditer(text)) if discount > 0 else []
        own, note, suffix = _notes(state) if anchors else (frozenset(), "", "")
        anchor = next((m for m in anchors if m.group(1).lower() not in own), None)
        own_anchor = None if anchor else next((m for m in anchors if m.group(1).lower() in own), None)
        if anchor:
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
        if own_anchor:
            label = f'"{own_anchor.group(1)}"'
            if features.get("was_above_30d_low"):
                # The price history's positive hit: the marketplace's reference price was never charged.
                features["reference_anchor"] = True
                observations.append(Observation(type="reference_anchor", evidence=label))
            else:
                observations.append(Observation(type=note, evidence=label + suffix))
        return self.output(features, observations)


def _notes(state: AnalysisState) -> tuple[frozenset[str], str, str]:
    """
    The reference-price labels that are a note on this page, not the reason, and which note they give.

    A marketplace's own labels (``marketplace_reference``); on a web shop, a recommended-retail label on the brand's
    own shop (``brand_reference``) or next to a 30-day low that agrees with the discount (``disclosed_reference``).

    :param state: The graph state; uses ``facts``, ``cleaned_content``, ``url`` and ``page_type``.
    :return: The labels (lower case), the note's finding type, and what its evidence adds after the label.
    """
    facts = state["facts"]
    if own := _OWN_LABELS.get(facts.platform):
        return own, "marketplace_reference", ""
    if _brands_own_shop(state):
        return _RECOMMENDED_LABELS, "brand_reference", ""
    low = thirty_day_low(state["cleaned_content"])
    if low is not None and _agrees(facts.price, facts.list_price, low):
        return _RECOMMENDED_LABELS, "disclosed_reference", f"; lowest price in the last 30 days: {low:g}"
    return frozenset(), "", ""


def _brands_own_shop(state: AnalysisState) -> bool:
    """
    Whether a page is the brand's own web shop: the shop's domain is the brand's name, or the seller is the brand.

    :param state: The graph state; uses ``facts``, ``url`` and ``page_type``.
    :return: ``True`` on a web shop (not a marketplace) whose brand runs it, by a brand that doesn't look generated.
    """
    facts = state["facts"]
    if state.get("page_type") != "storefront" or facts.platform not in _WEB_SHOPS or not facts.brand:
        return False
    if generated_brand_score(facts.brand) >= 0.6:  # a made-up brand's "recommended" price is the shop's own
        return False
    host = hostname(state.get("url", ""))
    if host and brands.is_shop_name(facts.brand, price_store.registrable_domain(host)):
        return True
    return bool(brands.same_company(facts.brand, facts.seller))


def thirty_day_low(text: str) -> float | None:
    """
    The lowest price of the last 30 days the page discloses (the EU's price-reduction rule), if it does.

    :param text: The page's cleaned text.
    :return: The first disclosed 30-day low, or ``None``.
    """
    m = _THIRTY_DAY_LOW_RE.search(text)
    return parse_number(m.group(1)) if m else None


def _agrees(price: float | None, was: float | None, low: float) -> bool:
    # Today's price at or below the disclosed 30-day low, and the low at or below the "was" price.
    return price is not None and was is not None and 0 < price <= low * _AT_LOW and low <= was
