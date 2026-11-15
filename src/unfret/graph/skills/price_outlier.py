"""
Market price outlier: an asking price far below what the item goes for, the classic bait of a non-delivery ad.

Two references, both conservative:

- **The same product new**, for an ad naming a barcode (EAN/GTIN): the median shop price for it over the last 90
  days in Unfret's own anonymous market prices (:func:`unfret.market_prices.for_ad`, at least two shops; the ad
  itself is never recorded). Far below means under half of that for an item sold as new, under a quarter for a
  used one.
- **The kind of item**, otherwise: a short list of the goods non-delivery ads use most (recent phones, consoles,
  laptops, graphics cards) and rentals, each with a floor well under what a used one fetches honestly. A price
  under the floor is far below; an ad that is for an accessory, a broken item, a swap or a wanted ad is never
  compared, nor a starting bid, a price under 10 or a currency other than euros, dollars or pounds.

Anything else is not compared at all: no reference, no finding.
"""

import re
from dataclasses import dataclass

from unfret import market_prices
from unfret.graph.skills import P2P_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState


@dataclass(frozen=True)
class Reference:
    """A kind of item and a price an honest used one hardly ever goes under."""

    pattern: re.Pattern[str]
    label: str
    floor: float


def _ref(pattern: str, label: str, floor: float) -> Reference:
    return Reference(re.compile(pattern, re.IGNORECASE), label, floor)


# Floors in euros, dollars or pounds alike (pounds are worth more, so the floor is stricter there, never looser).
# More specific patterns come first. Floors are well below typical used prices in late 2026.
REFERENCES: tuple[Reference, ...] = (
    _ref(r"iphone\s?17", "an iPhone 17", 450),
    _ref(r"iphone\s?16", "an iPhone 16", 350),
    _ref(r"iphone\s?15", "an iPhone 15", 250),
    _ref(r"iphone\s?14", "an iPhone 14", 180),
    _ref(r"iphone\s?13", "an iPhone 13", 130),
    _ref(r"galaxy\s?s2[56]", "a Samsung Galaxy S25", 300),
    _ref(r"galaxy\s?s24", "a Samsung Galaxy S24", 220),
    _ref(r"playstation\s?5|(?<![a-z])ps\s?5(?![a-z0-9])", "a PlayStation 5", 180),
    _ref(r"xbox series x", "an Xbox Series X", 180),
    _ref(r"(?:nintendo\s)?switch\s?2(?![0-9])", "a Nintendo Switch 2", 220),
    _ref(r"steam deck", "a Steam Deck", 180),
    _ref(r"macbook (?:air|pro)(?:\s\S+){0,4}\sm4(?![0-9])", "an M4 MacBook", 550),
    _ref(r"macbook (?:air|pro)(?:\s\S+){0,4}\sm3(?![0-9])", "an M3 MacBook", 450),
    _ref(r"macbook (?:air|pro)(?:\s\S+){0,4}\sm2(?![0-9])", "an M2 MacBook", 350),
    _ref(r"macbook (?:air|pro)(?:\s\S+){0,4}\sm1(?![0-9])", "an M1 MacBook", 250),
    _ref(r"rtx\s?5090", "an RTX 5090", 1200),
    _ref(r"rtx\s?4090", "an RTX 4090", 800),
    _ref(r"rtx\s?(?:5080|4080)", "an RTX 4080 or 5080", 500),
    _ref(r"ipad pro", "an iPad Pro", 250),
)
# Rentals: a monthly rent this low for a whole home, or a room, is bait in any city these platforms serve.
_RENTAL_RE = re.compile(
    r"huizen en kamers|te huur|for rent|apts/housing|huurwoning|rooms? for rent|kamer te huur"
    r"|(?<![a-z])\d\s?br(?![a-z])|bedroom|slaapkamer",
    re.IGNORECASE,
)
_ROOM_RE = re.compile(r"(?<![a-z])(?:kamer|room|studio)(?![a-z])", re.IGNORECASE)
_BEDROOMS_RE = re.compile(r"\d\s?(?:br|bedroom|slaapkamer)", re.IGNORECASE)
HOME_FLOOR, ROOM_FLOOR = 500.0, 250.0
# An accessory on its own, not the item itself (unless the title also names the item's own specifications).
_ACCESSORY_RE = re.compile(
    r"(?<![a-z])(?:hoes(?:je)?|case|cover|screen ?protector|beschermglas|tempered glass|lader|charger|kabel|cable"
    r"|doos|box|houder|stand|skin|sticker|controllers?|dualsense|joy-?cons?|games?|spel(?:len)?|hdd|ssd)(?![a-z])",
    re.IGNORECASE,
)
_WHOLE_ITEM_RE = re.compile(
    r"\d{2,4}\s?(?:gb|tb)(?![a-z])|(?<![a-z])(?:console|consola|disc|digital edition|slim|bundle)(?![a-z])",
    re.IGNORECASE,
)
_NOT_COMPARABLE_RE = re.compile(
    r"(?<![a-z])(?:defect|kapot|broken|cracked|gebarsten|icloud|locked|onderdelen|for parts|parts only|ruil|ruilen"
    r"|swap|trade|gezocht|wanted|gevraagd|zoek|zoekt|inkoop|we buy|lease|abonnement|contract|lege doos|empty box)"
    r"(?![a-z])",
    re.IGNORECASE,
)
_NEW_RE = re.compile(r"(?<![a-z])(?:new|nieuw|neu|neuf|sealed|geseald)(?![a-z])", re.IGNORECASE)
_CURRENCIES = frozenset({"EUR", "USD", "GBP"})
MIN_PRICE = 10.0
MARKET_MIN_SHOPS = 2
_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def money(amount: float, currency: str) -> str:
    """
    A price as shoppers read it.

    :param amount: The amount.
    :param currency: Its ISO code.
    :return: E.g. ``€350`` or ``$1,100.50``.
    """
    text = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    return f"{_SYMBOLS.get(currency, '')}{text}" if currency in _SYMBOLS else f"{text} {currency}".strip()


def reference(title: str, category: str) -> tuple[str, float] | None:
    """
    The kind of item an ad is for and its floor price, when it is one of the compared kinds.

    :param title: The ad's title.
    :param category: The platform's category path.
    :return: ``(label, floor)``, or ``None``.
    """
    if _NOT_COMPARABLE_RE.search(title):
        return None
    if _RENTAL_RE.search(f"{title} {category}"):
        room = bool(_ROOM_RE.search(title)) and not _BEDROOMS_RE.search(title)
        return ("a room for rent", ROOM_FLOOR) if room else ("a home for rent", HOME_FLOOR)
    if _ACCESSORY_RE.search(title) and not _WHOLE_ITEM_RE.search(title):
        return None
    for ref in REFERENCES:
        if ref.pattern.search(title):
            return ref.label, ref.floor
    return None


class PriceOutlierSkill(Skill):
    """The asking price against the same product new, or against the floor for its kind of item."""

    name = "price_outlier"
    description = "Flags an asking price far below what the same product, or that kind of item, goes for."
    page_types = P2P_PAGES
    features = ("checked", "market", "ratio", "far_below", "normal")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Compare the asking price with a reference, when there is one.

        :param state: Current graph state; uses ``facts``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        listing = facts.listing
        price, currency = facts.price, (facts.currency or "").upper()
        if price is None or price < MIN_PRICE or currency not in _CURRENCIES:
            return self.output({"checked": False}, note="No asking price to compare.")
        if listing is not None and listing.bidding and not listing.negotiable and price < 50:
            return self.output({"checked": False}, note="A starting bid, not a price.")
        condition = listing.condition if listing is not None else ""
        if _NOT_COMPARABLE_RE.search(condition):
            return self.output({"checked": False}, note="The item is sold as broken or for parts.")
        market = await market_prices.for_ad(facts)
        if market is not None and market.median and market.shops >= MARKET_MIN_SHOPS:
            new = bool(_NEW_RE.search(condition))
            ratio = price / market.median
            far = ratio < (0.5 if new else 0.25)
            evidence = (
                f"Asking {money(price, currency)}; the same product (by its barcode) costs about "
                f"{money(market.median, currency)} new in {market.shops} shops we checked in the last "
                f"{market_prices.WINDOW_DAYS} days."
            )
            return self._result(ratio, far=far, normal=ratio >= 0.7, market=True, evidence=evidence)
        found = reference(facts.title, listing.category if listing is not None else "")
        if found is None:
            return self.output({"checked": False}, note="No reference price for this kind of item.")
        label, floor = found
        ratio = price / floor
        evidence = (
            f"Asking {money(price, currency)} for {label}: honest ads for one rarely go under "
            f"{money(floor, currency)}."
        )
        return self._result(ratio, far=ratio < 1, normal=ratio >= 1.5, market=False, evidence=evidence)

    def _result(self, ratio: float, *, far: bool, normal: bool, market: bool, evidence: str) -> SkillOutput:
        observations = [Observation(type="price_far_below", evidence=evidence)] if far else []
        return self.output(
            {
                "checked": True,
                "market": market,
                "ratio": round(min(ratio, 10.0), 3),
                "far_below": far,
                "normal": normal,
            },
            observations,
            note="" if far else "The asking price is not far below the reference.",
        )
