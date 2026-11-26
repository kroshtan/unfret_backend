"""
One overall risk level per result, with the reasons that set it.

Levels: a higher number is always worse.

4. ``wont_arrive``: the order may never arrive (a shop that may not be real), card or personal data may be taken,
   or the product may be unsafe.
3. ``doesnt_work``: the product may not do what the page says (claims that don't add up, specs that contradict).
2. ``overpaying``: it may cost much more than it is worth (generic goods relabelled, dropship markup, hidden costs,
   poor quality).
1. ``manipulation``: pressure and tricks (fake countdowns and scarcity, misleading discounts, fake reviews).
0. ``none``: no concern found. Two things move a level that are not levels themselves:

- **Combining.** Smaller problems add up: a page with problems at levels 1, 2 and 3 together counts as level 4
  (it doesn't work, costs too much and pushes you to buy anyway).
- **Protection** is a modifier: recourse (card or platform payment with buyer protection, a real return address)
  lowers the level by one, payment you can't get back (bank transfer, crypto, gift cards, cash up front) raises it
  by one.

**Who sets the level.** Checks only emit findings; a model sets the answer: the level judge
(:mod:`unfret.level_judge`) when it is on, a level classifier once one is trained on level labels. Until then the
level is a *translation of the band* the scorer set (:func:`from_band`, ``source="band"``): no concern at "Looks
fine", level 4 at "Serious concerns", otherwise the level the findings shown point at, combined and modified as
above. :data:`FINDING_LEVELS` is data (what each finding is evidence for, and the card's order), not a rule that
sets the answer by itself.

The level always comes with its reasons (``because``): the findings that drove it, by level, and the combining or
protection step in words when one applied. The texts never call a shop a scam.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal

from pydantic import BaseModel, Field

LevelKey = Literal["wont_arrive", "doesnt_work", "overpaying", "manipulation", "none"]
Direction = Literal["raised", "lowered"]
LevelSource = Literal["band", "judge", "classifier"]

NONE = 0
LEVELS: tuple[int, ...] = (1, 2, 3, 4)
KEYS: dict[int, LevelKey] = {4: "wont_arrive", 3: "doesnt_work", 2: "overpaying", 1: "manipulation", 0: "none"}
LEVEL_OF_KEY: dict[str, int] = {v: k for k, v in KEYS.items()}


@dataclass(frozen=True)
class FindingLevel:
    """What one finding (a card reason's type) is evidence for."""

    level: int | None  # 1–4, or None: not a level of its own (a modifier, or a note)
    modifier: Direction | None = None  # "raised": takes recourse away; "lowered": gives recourse


def _table(level: int | None, keys: str, modifier: Direction | None = None) -> dict[str, FindingLevel]:
    return {k: FindingLevel(level, modifier) for k in keys.split()}


# Every card reason type (``copy/en.json`` "reasons") and what it is evidence for. Data for the level judge, the
# classifier's features and the card's order; not a rule that sets the answer.
FINDING_LEVELS: dict[str, FindingLevel] = {
    # 4 · won't arrive / a shop that may not be real / data theft / unsafe
    **_table(
        4,
        "new_domain domain_repurposed short_registration invalid_vat founded_contradicted far_below_market "
        "seller_lookalike linked_new_shops missing_trust_info no_imprint hidden_ai_text safety_gate_alert "
        "brand_photo_reused mailbox_address store_without_address shared_address origin_contradicted "
        # peer-to-peer ads
        "p2p_photo_in_other_ads p2p_seller_abroad p2p_price_far_below p2p_new_account p2p_listing_burst "
        "p2p_duplicate_text p2p_shared_contact p2p_contact_outside p2p_llm_off_platform p2p_low_rating "
        "p2p_no_ratings p2p_little_known p2p_slow_responses p2p_shipping_only p2p_photo_on_shops "
        # service providers
        "doorway_pages linked_sites copied_photo new_site kvk_malformed kvk_missing licence_no_number names_differ "
        "no_street_address new_on_platform few_platform_reviews low_response_rate service_little_known not_local "
        "trade_town_name national_number_only address_elsewhere details_on_request unproven_profile "
        "shared_address_sites",
    ),
    # 3 · doesn't work: false claims, specs that contradict each other or the photos
    **_table(
        3,
        "claim_contradicted implausible_claims data_inconsistency photo_count_mismatch photo_size_mismatch "
        "origin_doubt word_soup ai_text variant_bait_price",
    ),
    # 2 · overpaying: generic goods relabelled, dropship markup, hidden costs, costly returns, poor quality
    **_table(
        2,
        "generic_brand generic_product brand_no_presence rebranded_photo photo_reused dropshipping_tools "
        "supplier_links ships_from_far long_delivery seller_not_brand drip_pricing late_fees restocking_fee "
        "premium_number bait_callout vague_pricing quote_on_site low_platform_rating",
    ),
    # 2: returns that cost a lot or can't be made (the money is the buyer's risk; the payment modifier is separate)
    **_table(2, "returns_abroad return_address_withheld no_return_policy no_withdrawal"),
    # 1 · manipulation: pressure, fake scarcity, misleading discounts, fake or inflated reviews
    **_table(
        1,
        "timer_resets fake_urgency_app countdown preticked_addon pushy_copy big_discount was_never_charged "
        "implausible_discount reference_anchor marketplace_reference brand_reference disclosed_reference "
        "permanent_sale review_velocity review_shape duplicate_reviews off_topic_reviews review_import_app "
        "copied_reviews_elsewhere variants_grouped emergency_pressure copied_testimonials placeholder_names "
        "testimonial_dates all_five_testimonials",
    ),
    # 1: an "only N left" that never changes, or keeps going back up, across days (Unfret's records of the page)
    **_table(1, "scarcity_resets scarcity_static"),
    # Protection modifiers: payment you can't get back raises the level
    **_table(
        None,
        "irreversible_payment_only p2p_advance_payment p2p_untraceable_payment p2p_protection_refused "
        "p2p_bank_transfer upfront_payment cash_only",
        "raised",
    ),
    # The scorer's small signs standing in as one reason: no level of its own
    "overall": FindingLevel(None),
}
# Positives that give recourse: they lower the level.
RECOURSE_POSITIVES: frozenset[str] = frozenset({"protected_payment", "p2p_protection", "p2p_pickup"})
# Notes the card shows but that never carry a level by themselves (a contradiction judged immaterial, a reference
# price the shop discloses): they are listed under the level, never the reason for it.
NOTES: frozenset[str] = frozenset(
    {"data_inconsistency", "variants_grouped", "marketplace_reference", "brand_reference", "disclosed_reference"}
)


# Finding types a shopper can mark as missing in a "Wrong result?" report: every card reason but the scorer's small
# signs ("overall", not a finding of its own). GET /v1/catalogue/findings lists them.
MARKABLE: tuple[str, ...] = tuple(t for t in FINDING_LEVELS if t != "overall")


def level_of(reason_type: str) -> int | None:
    """
    The level a finding is evidence for.

    :param reason_type: A card reason's type.
    :return: 1–4, or ``None`` (a modifier, the small signs, or a type this table doesn't know).
    """
    entry = FINDING_LEVELS.get(reason_type)
    return entry.level if entry else None


def order_key(reason_type: str) -> int:
    """
    The card's sort key for a reason: the highest (worst) level first, reasons without a level last.

    :param reason_type: A card reason's type.
    :return: The key.
    """
    level = level_of(reason_type)
    return -level if level is not None else 1


class LevelReason(BaseModel):
    """One finding behind the level."""

    type: str = Field(description="The card reason's type (the same as in verdict.reasons).")
    level: int | None = Field(
        description="The level this finding is evidence for (1–4, higher is worse), or null for a modifier."
    )
    title: str = Field(description='The finding in words: "Fake countdown".')
    source: str | None = Field(
        default=None,
        description='Where the evidence comes from, in words, as on the card\'s reasons: "The page", '
        '"Domain registry (RDAP)", …',
    )


class LevelModifier(BaseModel):
    """Protection moved the level."""

    direction: Direction = Field(description="raised (payment you can't get back) or lowered (recourse).")
    type: str = Field(description="The reason (or positive) type behind it: irreversible_payment_only, …")
    text: str = Field(description='In words: "Raised: only payments you can\'t get back."')


class Risk(BaseModel):
    """The overall risk level and why."""

    level: int = Field(ge=0, le=4, description="0 no concern found, then 1 to 4: higher is worse.")
    key: LevelKey = Field(description="wont_arrive, doesnt_work, overpaying, manipulation or none.")
    label: str = Field(description='The level in words: "May not arrive, or may not be safe", …')
    because: list[LevelReason] = Field(
        default_factory=list, description="The findings that drove the level, ordered by level (most serious first)."
    )
    combined: str | None = Field(
        default=None,
        description="Set when lower-level problems together moved the level up: which ones, in words.",
    )
    modifier: LevelModifier | None = Field(default=None, description="Set when protection raised or lowered it.")
    explanation: str = Field(
        description="One line: \"Level 4 because: A claim that doesn't add up + Far below the usual price + Fake "
        "countdown. Raised: only payments you can't get back.\""
    )
    source: LevelSource = Field(
        description="Who set it: band (translated from the scorer's band, until a level model is on), judge (the "
        "level judge), classifier (a level classifier)."
    )
    confidence: float | None = Field(default=None, ge=0, le=1, description="The model's confidence, when it has one.")


@cache
def level_copy() -> dict[str, Any]:
    """
    The level texts of the copy catalogue (``copy/en.json`` "levels").

    :return: The texts.
    """
    from unfret.shopper import catalogue  # noqa: PLC0415 - shopper imports this module

    return dict(catalogue()["levels"])


def title_of(reason_type: str, titles: dict[str, str] | None = None) -> str:
    """
    A reason's (or positive's) title: as the card shows it, else the catalogue's.

    :param reason_type: The type.
    :param titles: Titles as the card shows them, by type.
    :return: The title.
    """
    from unfret.shopper import catalogue  # noqa: PLC0415 - shopper imports this module

    if titles and reason_type in titles:
        return titles[reason_type]
    copy = catalogue()
    entry = copy["reasons"].get(reason_type) or copy["positives"].get(reason_type) or {}
    return str(entry.get("title", reason_type))


def combine(levels: Iterable[int]) -> int | None:
    """
    The combining step: problems at levels 1, 2 and 3 together count as level 4.

    :param levels: The levels found (1–4).
    :return: 4 when they combine upward, else ``None``.
    """
    found = set(levels)
    return 4 if 4 not in found and {1, 2, 3} <= found else None


def explain(
    level: int,
    because: list[LevelReason],
    *,
    combined: str | None,
    modifier: LevelModifier | None,
) -> str:
    """
    The level's one-line explanation.

    :param level: 0–4.
    :param because: The findings behind it.
    :param combined: The combining step in words, if any.
    :param modifier: The protection step, if any.
    :return: The line.
    """
    copy = level_copy()
    if level == NONE:
        return str(copy["none_explanation"])
    if combined:
        text = str(copy["because"]).format(level=level, reasons=combined)
    else:
        leveled = [r.title for r in because if r.level is not None][:4]
        text = str(copy["because"]).format(level=level, reasons=" + ".join(leveled) or str(copy["small_signs"]))
    if modifier:
        text += " " + modifier.text
    return text


def from_band(
    band: str,
    reason_types: Iterable[str],
    *,
    positive_types: Iterable[str] = (),
    titles: dict[str, str] | None = None,
) -> Risk:
    """
    The level as a translation of the scorer's band, until a level model sets it (``source="band"``).

    "Looks fine" is no concern; otherwise the most serious level the findings point at (level 4 at "Serious
    concerns", which the scorer reserves for pages it is sure about), combined upward and moved by protection
    (:func:`combine`; one step either way). Two caps keep the translation as mild as the band: "Worth a look" is
    never level 4 (it is a thing to look at, not a warning), and recourse never lowers level 4 (a chargeback doesn't
    undo stolen card details or an unsafe product).

    :param band: reliable, concerns, careful or junk_or_scam.
    :param reason_types: Every reason that fired (not only the three shown), strongest first.
    :param positive_types: The positives shown.
    :param titles: Reason titles as the card shows them (by type); the catalogue's otherwise.
    :return: The risk.
    """
    titles = titles or {}
    types = list(dict.fromkeys(reason_types))
    copy = level_copy()
    if band == "reliable":
        return Risk(
            level=NONE, key="none", label=copy["labels"]["none"], explanation=copy["none_explanation"], source="band"
        )
    weighed = [t for t in types if level_of(t) is not None and t not in NOTES]
    found = sorted({lv for t in weighed if (lv := level_of(t)) is not None})
    level = found[-1] if found else 1  # only the scorer's small signs: the mildest level
    combined_text: str | None = None
    if combine(found) is not None:
        level = 4
        parts = [title_of(next(t for t in weighed if level_of(t) == lv), titles) for lv in (3, 2, 1)]
        combined_text = str(copy["combined"]).format(reasons=" + ".join(parts))
    if band == "junk_or_scam":
        level = 4
    modifier: LevelModifier | None = None
    raising = next((t for t in types if (e := FINDING_LEVELS.get(t)) and e.modifier == "raised"), None)
    lowering = next((t for t in positive_types if t in RECOURSE_POSITIVES), None)
    if raising and level < 4:
        level += 1
        modifier = LevelModifier(
            direction="raised", type=raising, text=str(copy["raised"]).format(reason=title_of(raising, titles))
        )
    elif lowering and not raising and 1 < level < 4:
        level -= 1
        modifier = LevelModifier(
            direction="lowered", type=lowering, text=str(copy["lowered"]).format(reason=title_of(lowering, titles))
        )
    if band == "concerns" and level == 4:
        level = 3
    because = sorted(
        (LevelReason(type=t, level=level_of(t), title=title_of(t, titles)) for t in types if t != "overall"),
        key=lambda r: order_key(r.type),
    )
    return Risk(
        level=level,
        key=KEYS[level],
        label=copy["labels"][KEYS[level]],
        because=because,
        combined=combined_text,
        modifier=modifier,
        explanation=explain(level, because, combined=combined_text, modifier=modifier),
        source="band",
    )
