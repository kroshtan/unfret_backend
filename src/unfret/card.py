"""
The result card: what every client shows around a verdict, worked out once, here.

A verdict (:mod:`unfret.shopper`, :mod:`unfret.shopper_p2p`, :mod:`unfret.graph.services`) says what was found. The
card adds what makes it checkable for a shopper, the same on the website, a shared result, the extension and the
phone app:

- **a source for every finding**: where its evidence comes from ("Domain registry (RDAP)", "Unfret's price
  history", "The page"), in words from the copy catalogue;
- **a checklist of what was checked**, area by area ("Who's behind the shop", "Reviews", …): checked, a concern (a
  finding above is about it), or not checked and why (the page shows no reviews; not part of a quick check; the
  registry couldn't be reached this time);
- **how many checks ran**, so a clean result says how much was looked at;
- **one suggested next step**, from the strongest finding or the band;
- **the same product sold elsewhere** (Unfret's image index), which clients show first, with both photos;
- on a quick result, **whether a deep check is worth it** (:func:`deep_check`): what it would add for this page, never
  a change to the quick verdict. What it costs the caller is added per response (:func:`with_cost`);
- when a key step didn't finish (the assessment is degraded), **one line that says so** (:func:`incomplete`): which
  step, that the answer may be milder than it should be, and that it still counted as a check.

Everything here comes from the catalogue or from what the skills measured; nothing quotes the page, and nothing
calls a shop names.
"""

from collections.abc import Callable, Iterable
from typing import Any, Literal

from unfret import levels, scoring, shopper
from unfret.config import settings
from unfret.credits import packs
from unfret.facts import PageFacts
from unfret.graph import platforms
from unfret.graph.skills import NOT_APPLICABLE, UNAVAILABLE, Skill
from unfret.graph.skills.listing import resold
from unfret.graph.state import P2P_TYPES, SERVICE_TYPES, AnalysisState
from unfret.market import shop_market
from unfret.prices import format_usd
from unfret.shopper import (
    CheckItem,
    DeepCheck,
    DeepCheckAdd,
    Incomplete,
    NextStep,
    SameProduct,
    Verdict,
    catalogue,
)

# Where each finding's evidence comes from (keys of the catalogue's "sources").
REASON_SOURCES: dict[str, str] = {
    # Products: what the page shows, read by rules.
    "timer_resets": "page",
    "countdown": "page",
    "preticked_addon": "page",
    "late_fees": "page",
    "big_discount": "page",
    "implausible_discount": "page",
    "reference_anchor": "page",
    "marketplace_reference": "page",
    "brand_reference": "page",
    "disclosed_reference": "page",
    "permanent_sale": "page",
    "seller_not_brand": "page",
    "seller_lookalike": "page",
    "ships_from_far": "page",
    "drip_pricing": "checkout",
    # The page's code.
    "fake_urgency_app": "page_code",
    "review_import_app": "page_code",
    "dropshipping_tools": "page_code",
    "supplier_links": "page_code",
    "hidden_ai_text": "page_code",
    "ai_text": "page",
    # Unfret's read of the page (the language model).
    "word_soup": "ai_read",
    "generic_brand": "ai_read",
    "generic_product": "ai_read",
    "implausible_claims": "ai_read",
    "claim_contradicted": "ai_read",
    "data_inconsistency": "ai_read",
    "variants_grouped": "ai_read",
    "variant_bait_price": "ai_read",
    "pushy_copy": "ai_read",
    "missing_trust_info": "ai_read",
    # The reviews on the page.
    "review_velocity": "reviews",
    "review_shape": "reviews",
    "duplicate_reviews": "reviews",
    "off_topic_reviews": "reviews",
    # The shop's terms and legal pages.
    "no_withdrawal": "legal_pages",
    "no_return_policy": "legal_pages",
    "no_imprint": "legal_pages",
    "returns_abroad": "legal_pages",
    "return_address_withheld": "legal_pages",
    "restocking_fee": "legal_pages",
    "origin_doubt": "legal_pages",
    "long_delivery": "legal_pages",
    "mailbox_address": "legal_pages",
    "irreversible_payment_only": "legal_pages",
    "origin_contradicted": "legal_pages",
    "store_without_address": "legal_pages",
    # Registers and lookups elsewhere.
    "invalid_vat": "vies",
    "new_domain": "rdap",
    "domain_repurposed": "archive",
    "short_registration": "rdap",
    "founded_contradicted": "rdap",
    "was_never_charged": "price_history",
    "far_below_market": "market_prices",
    "photo_reused": "image_index",
    "rebranded_photo": "image_index",
    "brand_photo_reused": "image_index",
    "linked_new_shops": "shop_records",
    "shared_address": "shop_records",
    "copied_reviews_elsewhere": "review_records",
    "brand_no_presence": "brand_records",
    "safety_gate_alert": "safety_gate",
    "photo_count_mismatch": "photos",
    "photo_size_mismatch": "photos",
    "overall": "model",
    # Service providers.
    "doorway_pages": "site_pages",
    "linked_sites": "shop_records",
    "shared_address_sites": "shop_records",
    "copied_photo": "image_index",
    "premium_number": "page",
    "upfront_payment": "page",
    "cash_only": "page",
    "quote_on_site": "page",
    "new_site": "rdap",
    "copied_testimonials": "page",
    "placeholder_names": "page",
    "testimonial_dates": "page",
    "all_five_testimonials": "page",
    "kvk_malformed": "page",
    "kvk_missing": "page",
    "licence_no_number": "page",
    "names_differ": "page",
    "bait_callout": "page",
    "emergency_pressure": "page",
    "vague_pricing": "page",
    "no_street_address": "page",
    "not_local": "page",
    "trade_town_name": "page",
    "national_number_only": "page",
    "address_elsewhere": "page",
    "details_on_request": "page",
    "new_on_platform": "platform",
    "few_platform_reviews": "platform",
    "low_response_rate": "platform",
    "unproven_profile": "platform",
    "low_platform_rating": "platform",
    "service_little_known": "page",
    # Peer-to-peer ads.
    "p2p_advance_payment": "page",
    "p2p_untraceable_payment": "page",
    "p2p_protection_refused": "page",
    "p2p_seller_abroad": "page",
    "p2p_bank_transfer": "page",
    "p2p_shipping_only": "page",
    "p2p_contact_outside": "page",
    "p2p_llm_off_platform": "ai_read",
    "p2p_photo_in_other_ads": "ad_records",
    "p2p_duplicate_text": "ad_records",
    "p2p_shared_contact": "ad_records",
    "p2p_photo_on_shops": "image_index",
    "p2p_price_far_below": "ad_records",
    "p2p_new_account": "platform",
    "p2p_listing_burst": "platform",
    "p2p_low_rating": "platform",
    "p2p_no_ratings": "platform",
    "p2p_slow_responses": "platform",
    "p2p_little_known": "page",
}
POSITIVE_SOURCES: dict[str, str] = {
    "known_brand": "brand_records",
    "sold_by_brand": "page",
    "vat_registered": "vies",
    "us_business": "legal_pages",
    "established_domain": "rdap",
    "established_archive": "archive",
    "trust_info_present": "legal_pages",
    "natural_reviews": "reviews",
    "protected_payment": "legal_pages",
    "nothing_found": "model",
    "service_established": "rdap",
    "service_identity_nl": "page",
    "service_identity_us": "page",
    "licence_shown": "page",
    "written_quote": "page",
    "platform_track_record": "platform",
    "platform_verified": "platform",
    "service_nothing_found": "model",
    "p2p_established": "platform",
    "p2p_well_rated": "platform",
    "p2p_verified": "platform",
    "p2p_protection": "page",
    "p2p_pickup": "page",
    "p2p_normal_price": "ad_records",
}

# The checklist's areas per kind of page, each with the skills that look at it (a skill may serve two areas), and
# which area each finding belongs to (a finding not listed belongs to none: the "overall" reason).
AREAS: dict[str, dict[str, tuple[str, ...]]] = {
    "shop": {
        "who": (
            "eu_registry",
            "us_identity",
            "domain_age",
            "shop_links",
            "claims",
            "payment_methods",
            "domain_history",
        ),
        "product": (
            "listing",
            "content",
            "claim_audit",
            "brand_presence",
            "safety_gate",
            "reverse_image",
            "photo_claims",
            "variant_check",
        ),
        "reviews": ("reviews", "review_copies"),
        "prices": ("pricing", "market_price", "dark_patterns", "shop_policies", "variant_check"),
        "tricks": ("html_source_signals", "injection", "dark_patterns"),
    },
    "ad": {
        "seller": ("seller_profile",),
        "price": ("price_outlier",),
        "photos": ("reverse_image", "duplicate_ads"),
        "payment": ("off_platform",),
        "text": ("content", "injection"),
    },
    "service": {
        "company": ("service_identity", "platform_profile", "eu_registry", "us_identity"),
        "location": ("doorway_pages", "shop_links", "domain_age"),
        "reviews": ("testimonials", "platform_profile"),
        "terms": ("service_terms", "dark_patterns"),
        "photos": ("reverse_image",),
    },
}
REASON_AREAS: dict[str, str] = {
    **dict.fromkeys(
        (
            "no_imprint",
            "missing_trust_info",
            "invalid_vat",
            "new_domain",
            "domain_repurposed",
            "short_registration",
            "origin_doubt",
            "mailbox_address",
            "irreversible_payment_only",
            "founded_contradicted",
            "origin_contradicted",
            "store_without_address",
            "linked_new_shops",
            "shared_address",
        ),
        "who",
    ),
    **dict.fromkeys(
        (
            "word_soup",
            "generic_brand",
            "generic_product",
            "implausible_claims",
            "claim_contradicted",
            "data_inconsistency",
            "variants_grouped",
            "seller_lookalike",
            "seller_not_brand",
            "ships_from_far",
            "photo_reused",
            "rebranded_photo",
            "brand_photo_reused",
            "brand_no_presence",
            "safety_gate_alert",
            "photo_count_mismatch",
            "photo_size_mismatch",
        ),
        "product",
    ),
    **dict.fromkeys(
        (
            "review_velocity",
            "review_shape",
            "duplicate_reviews",
            "off_topic_reviews",
            "review_import_app",
            "copied_reviews_elsewhere",
        ),
        "reviews",
    ),
    **dict.fromkeys(
        (
            "preticked_addon",
            "drip_pricing",
            "late_fees",
            "big_discount",
            "was_never_charged",
            "implausible_discount",
            "reference_anchor",
            "marketplace_reference",
            "brand_reference",
            "disclosed_reference",
            "variant_bait_price",
            "permanent_sale",
            "far_below_market",
            "long_delivery",
            "no_withdrawal",
            "no_return_policy",
            "returns_abroad",
            "return_address_withheld",
            "restocking_fee",
        ),
        "prices",
    ),
    **dict.fromkeys(
        (
            "timer_resets",
            "fake_urgency_app",
            "countdown",
            "pushy_copy",
            "dropshipping_tools",
            "supplier_links",
            "hidden_ai_text",
            "ai_text",
        ),
        "tricks",
    ),
    # Ads.
    **dict.fromkeys(
        ("p2p_new_account", "p2p_listing_burst", "p2p_low_rating", "p2p_no_ratings", "p2p_slow_responses"), "seller"
    ),
    "p2p_price_far_below": "price",
    **dict.fromkeys(("p2p_photo_in_other_ads", "p2p_photo_on_shops"), "photos"),
    **dict.fromkeys(
        (
            "p2p_advance_payment",
            "p2p_untraceable_payment",
            "p2p_protection_refused",
            "p2p_seller_abroad",
            "p2p_bank_transfer",
            "p2p_shipping_only",
            "p2p_contact_outside",
            "p2p_llm_off_platform",
        ),
        "payment",
    ),
    **dict.fromkeys(("p2p_duplicate_text", "p2p_shared_contact"), "text"),
    # Service providers.
    **dict.fromkeys(
        (
            "kvk_malformed",
            "kvk_missing",
            "licence_no_number",
            "names_differ",
            "details_on_request",
            "trade_town_name",
            "unproven_profile",
            "new_on_platform",
        ),
        "company",
    ),
    **dict.fromkeys(
        (
            "doorway_pages",
            "linked_sites",
            "shared_address_sites",
            "new_site",
            "not_local",
            "national_number_only",
            "address_elsewhere",
            "no_street_address",
            "premium_number",
        ),
        "location",
    ),
    **dict.fromkeys(
        (
            "copied_testimonials",
            "placeholder_names",
            "testimonial_dates",
            "all_five_testimonials",
            "few_platform_reviews",
            "low_platform_rating",
            "low_response_rate",
        ),
        "reviews",
    ),
    **dict.fromkeys(
        ("upfront_payment", "cash_only", "quote_on_site", "bait_callout", "emergency_pressure", "vague_pricing"),
        "terms",
    ),
    "copied_photo": "photos",
}
# Reasons whose area depends on the kind of page (shared between shops, ads and service providers).
_SHARED_AREAS: dict[str, dict[str, str]] = {
    "ad": {"hidden_ai_text": "text", "ai_text": "text", "pushy_copy": "text"},
    "service": {
        "hidden_ai_text": "terms",
        "ai_text": "terms",
        "pushy_copy": "terms",
        "invalid_vat": "company",
        "timer_resets": "terms",
    },
}
# A skill that ran but had nothing to look at (its feature below 1), and the note that says so.
_HAD_NOTHING: dict[str, tuple[str, str]] = {
    "reviews": ("reviews.has_reviews", "no_reviews"),
    "review_copies": ("review_copies.checked", "no_reviews"),
    "domain_age": ("domain_age.known", "registry_silent"),
    "reverse_image": ("reverse_image.searched", "no_photo"),
    "photo_claims": ("photo_claims.checked", "nothing"),
    "domain_history": ("domain_history.snapshots", "no_archive"),
}

# The next step for the strongest finding (keys of the catalogue's "next_steps"); otherwise the band's.
STEP_FOR_REASON: dict[str, str] = {
    **dict.fromkeys(
        (
            "new_domain",
            "domain_repurposed",
            "short_registration",
            "founded_contradicted",
            "linked_new_shops",
            "shared_address",
        ),
        "shop_reviews",
    ),
    **dict.fromkeys(
        ("no_imprint", "missing_trust_info", "invalid_vat", "mailbox_address", "origin_contradicted"), "find_company"
    ),
    **dict.fromkeys(("no_withdrawal", "no_return_policy", "returns_abroad", "restocking_fee"), "returns"),
    **dict.fromkeys(("return_address_withheld", "long_delivery", "ships_from_far", "origin_doubt"), "returns"),
    **dict.fromkeys(
        (
            "was_never_charged",
            "implausible_discount",
            "big_discount",
            "reference_anchor",
            "marketplace_reference",
            "brand_reference",
            "disclosed_reference",
            "far_below_market",
        ),
        "compare_price",
    ),
    **dict.fromkeys(("permanent_sale", "timer_resets", "fake_urgency_app", "countdown", "pushy_copy"), "take_time"),
    **dict.fromkeys(("preticked_addon", "drip_pricing", "late_fees"), "check_basket"),
    **dict.fromkeys(
        (
            "review_velocity",
            "review_shape",
            "duplicate_reviews",
            "off_topic_reviews",
            "review_import_app",
            "copied_reviews_elsewhere",
        ),
        "read_reviews",
    ),
    **dict.fromkeys(
        (
            "word_soup",
            "generic_brand",
            "generic_product",
            "dropshipping_tools",
            "supplier_links",
            "photo_reused",
            "rebranded_photo",
            "brand_no_presence",
            "seller_lookalike",
            "seller_not_brand",
        ),
        "compare_elsewhere",
    ),
    "brand_photo_reused": "real_brand",
    **dict.fromkeys(("photo_count_mismatch", "photo_size_mismatch"), "check_listing"),
    **dict.fromkeys(("variants_grouped", "variant_bait_price"), "check_variant"),
    "irreversible_payment_only": "buyer_protection",
    "safety_gate_alert": "safety_alert",
    **dict.fromkeys(
        (
            "p2p_advance_payment",
            "p2p_untraceable_payment",
            "p2p_protection_refused",
            "p2p_seller_abroad",
            "p2p_bank_transfer",
            "p2p_contact_outside",
            "p2p_llm_off_platform",
        ),
        "p2p_pay_safely",
    ),
    **dict.fromkeys(("p2p_photo_in_other_ads", "p2p_photo_on_shops", "p2p_shipping_only"), "p2p_see_item"),
    **dict.fromkeys(
        ("upfront_payment", "cash_only", "quote_on_site", "bait_callout", "vague_pricing", "emergency_pressure"),
        "service_quote",
    ),
}
# Reasons whose photos the same-product block already shows (clients drop "photo_reused"'s themselves).
_SHOWN_WITH_SAME_PRODUCT = frozenset({"rebranded_photo", "brand_photo_reused"})
# Findings that come before the band's own step even at "Serious concerns".
_URGENT_STEPS = frozenset({"safety_alert"})


def kind_of(page_type: str) -> str:
    """
    Which checklist a page gets.

    :param page_type: The classified page type.
    :return: ``shop``, ``ad`` or ``service``.
    """
    if page_type in P2P_TYPES:
        return "ad"
    if page_type in SERVICE_TYPES:
        return "service"
    return "shop"


_COMMON_CRAWL_LINK = "https://index.commoncrawl.org/"
# The first-seen source (domain_age) as a card source: an archive's copies, or the shops Unfret has checked.
_SEEN_SOURCES = {shopper.COMMON_CRAWL: "archive_cc", shopper.WAYBACK: "archive", shopper.UNFRET: "shop_records"}


def _archive_source(key: str, links: list[str], features: dict[str, float]) -> str:
    """
    The archive a reason or positive of the archives' kind names: the one its copies came from.

    A reason with links names the archive of its links; the "Established shop" of a first sighting names its source.

    :param key: The source key by its type (``archive`` for the archives' reasons and positives).
    :param links: The reason's links.
    :param features: The skill features.
    :return: The source key.
    """
    if key != "archive":
        return key
    if links:
        return "archive_cc" if all(u.startswith(_COMMON_CRAWL_LINK) for u in links) else "archive"
    return _SEEN_SOURCES[shopper.seen_source(features)]


def source_label(key: str) -> str:
    """
    A source in words.

    :param key: A key of the catalogue's ``sources``.
    :return: The words ("Domain registry (RDAP)").
    """
    sources: dict[str, str] = catalogue()["sources"]
    return sources.get(key) or sources["page"]


def _ran(skill: str, features: dict[str, float]) -> bool:
    """A skill ran and measured something (it isn't marked unavailable or not applicable)."""
    prefix = f"{skill}."
    names = [n for n in features if n.startswith(prefix)]
    return bool(names) and f"{prefix}{UNAVAILABLE}" not in features and f"{prefix}{NOT_APPLICABLE}" not in features


def counted(skill: str, features: dict[str, float]) -> bool:
    """
    Whether a skill counts as a check that ran: it measured something, and had something to look at.

    :param skill: The skill's name.
    :param features: The analysis's features.
    :return: ``True`` when it counts.
    """
    if not _ran(skill, features):
        return False
    needs = _HAD_NOTHING.get(skill)
    return needs is None or features.get(needs[0], 0.0) >= 1


def _note(skills: tuple[str, ...], features: dict[str, float], available: set[str], applicable: set[str]) -> str:
    """Why an area wasn't checked (a key of the catalogue's ``checklist.notes``)."""
    for skill in skills:
        needs = _HAD_NOTHING.get(skill)
        if needs and _ran(skill, features) and features.get(needs[0], 0.0) < 1:
            return needs[1]
    if any(f"{s}.{UNAVAILABLE}" in features for s in skills):
        return "unavailable"
    if any(f"{s}.{NOT_APPLICABLE}" in features for s in skills):
        return "not_applicable"
    if any(s in applicable and s not in available for s in skills):
        return "quick"
    return "nothing"


def checklist(
    kind: str,
    features: dict[str, float],
    concerns: dict[str, str],
    *,
    available: set[str],
    applicable: set[str],
) -> tuple[list[CheckItem], int]:
    """
    The card's "What we checked", and how many checks ran.

    Each area carries how many checks looked at it: a check that serves two areas (the dark-pattern read: prices and
    hidden tricks) counts once, in the first, so the areas' counts add up to the number of checks that ran.

    :param kind: ``shop``, ``ad`` or ``service`` (:func:`kind_of`).
    :param features: The analysis's features.
    :param concerns: The areas a finding is about, each with the strongest such finding's title (the note).
    :param available: The skills this analysis could run on this page (the quick tier runs fewer).
    :param applicable: The skills a full analysis would run on this page.
    :return: The checklist (areas no skill applies to are left out) and the number of checks that ran (the sum of
        the areas' counts).
    """
    copy = catalogue()["checklist"]
    items: list[CheckItem] = []
    seen: set[str] = set()
    for key, skills in AREAS[kind].items():
        if not any(s in applicable or s in available for s in skills):
            continue  # nothing looks at this on such a page (a marketplace has no shop of its own to look up)
        label = str(copy["areas"][kind][key])
        ran = {s for s in skills if counted(s, features)}
        checks = len(ran - seen)
        seen |= ran
        if key in concerns:
            items.append(CheckItem(key=key, label=label, status="concern", note=concerns[key], checks=checks))
        elif ran:
            items.append(CheckItem(key=key, label=label, status="checked", checks=checks))
        else:
            note = _note(skills, features, available, applicable)
            items.append(
                CheckItem(key=key, label=label, status="not_checked", note=str(copy["notes"][note]), checks=0)
            )
    return items, sum(i.checks or 0 for i in items)


def area_of(reason_type: str, kind: str) -> str | None:
    """
    The checklist area a finding is about.

    :param reason_type: The reason's type.
    :param kind: ``shop``, ``ad`` or ``service``.
    :return: The area's key, or ``None`` (a finding about the whole page, such as the "overall" reason).
    """
    area = _SHARED_AREAS.get(kind, {}).get(reason_type) or REASON_AREAS.get(reason_type)
    return area if area in AREAS[kind] else None


def small_signs(state: AnalysisState) -> list[str]:
    """
    The scorer's small signs on this page (:func:`unfret.shopper.small_signs`), from its assessment.

    :param state: The analysis state, with the judge's assessment.
    :return: The signs' feature names, strongest first; none without an assessment.
    """
    flags = (state.get("assessment") or {}).get("flags")
    return shopper.small_signs(flags if isinstance(flags, list) else [])


def _sign_concerns(signs: list[str], kind: str, taken: set[str]) -> dict[str, str]:
    """
    The areas the scorer's small signs are about, each with its signs in words ("A sizeable discount").

    The small signs set the band as much as the reasons do, so no area that moved the answer off "Looks fine" is
    ticked as fine; the verdict counts and names the same signs. An area a reason already marks keeps that note.

    :param signs: :func:`small_signs`.
    :param kind: ``shop``, ``ad`` or ``service``.
    :param taken: The areas a reason already marks.
    :return: The notes per area.
    """
    signed: dict[str, list[str]] = {}
    for sign in signs:
        if (area := area_of(shopper.SIGN_REASONS.get(sign, ""), kind)) is not None and area not in taken:
            signed.setdefault(area, []).append(shopper.sign_text(sign))
    return {area: _joined([texts[0], *(t[0].lower() + t[1:] for t in texts[1:])]) for area, texts in signed.items()}


def is_check(skill: str, features: dict[str, float], page_type: str) -> bool:
    """
    Whether a finished step is one of the checks the result's checklist counts (its ``checks_run``).

    A skill that counts (:func:`counted`: it measured something and had something to look at) and belongs to an area
    of the page's checklist. The streamed progress counts these, so "7 checks done" while checking is the "7 checks"
    the result then says it ran; reading the page, classifying it and triage are steps, not checks.

    :param skill: The step's node name.
    :param features: What the step measured.
    :param page_type: The page's type (its checklist).
    :return: ``True`` for a check.
    """
    areas = AREAS[kind_of(page_type)]
    return any(skill in skills for skills in areas.values()) and counted(skill, features)


def next_step(verdict: Verdict, kind: str, same_product: bool) -> NextStep:
    """
    One suggested next step: the strongest finding's, else the band's.

    At "Serious concerns" the band's step ("better to buy this from a shop you know") comes first, unless a finding
    is about safety. A clean result with the same product elsewhere suggests comparing.

    :param verdict: The verdict.
    :param kind: ``shop``, ``ad`` or ``service``.
    :param same_product: Whether the same product was found on another shop.
    :return: The step.
    """
    steps: dict[str, str] = catalogue()["next_steps"]
    band_key = f"{kind}_{verdict.band}" if f"{kind}_{verdict.band}" in steps else verdict.band
    first = verdict.reasons[0].type if verdict.reasons else None
    step = STEP_FOR_REASON.get(first or "")
    if verdict.band == "reliable" and not first:
        step = None
    if same_product and verdict.band in {"reliable", "concerns"} and step in {None, "compare_elsewhere"}:
        step = "same_product"
    if step is None or (verdict.band == "junk_or_scam" and step not in _URGENT_STEPS):
        return NextStep(type=band_key, text=steps[band_key])
    return NextStep(type=step, text=steps[step])


# What a deep check adds over a quick one, for the suggestion under a quick result: per page type the deep check would
# treat the page as, each extra with the checklist areas it is about (an extra about an area the quick check found a
# concern in is named first) and the skill that does it, when that skill is deep-only (the price history and the EU
# VAT register are the deep variants of quick skills: pricing, eu_registry). Each needs something on the page to look
# at (:func:`_has_something`). Keys of the catalogue's ``deep_check.adds``.
DEEP_EXTRAS: dict[str, tuple[tuple[str, tuple[str, ...], str | None], ...]] = {
    "storefront": (
        ("shop_age", ("who",), "domain_age"),
        ("photo_elsewhere", ("product", "tricks"), "reverse_image"),
        ("reviews", ("reviews",), "reviews"),
        ("brand", ("product",), "brand_presence"),
        ("price_history", ("prices",), None),
        ("vat_register", ("who",), None),
    ),
    "aggregate_listing": (
        ("photo_elsewhere", ("product", "tricks"), "reverse_image"),
        ("reviews", ("reviews",), "reviews"),
        ("brand", ("product",), "brand_presence"),
        ("price_history", ("prices",), None),
    ),
    # A quick check reads an ad or a provider's profile on a known platform as a shop's page (it has no page classes
    # beyond products); a deep check, with the expansion classes on, reads it as what it is.
    "p2p_listing": (
        ("seller_profile", ("seller",), "seller_profile"),
        ("ad_photos", ("photos",), "reverse_image"),
        ("ad_price", ("price",), "price_outlier"),
    ),
    "service_listing": (
        ("provider_profile", ("company", "reviews"), "platform_profile"),
        ("service_terms", ("terms",), "service_terms"),
    ),
}
DeepAction = Literal["run_deep", "get_checks"]
# At most this many extras are named: the suggestion stays one sentence.
MAX_DEEP_ADDS = 2


def _was_price(facts: PageFacts) -> bool:
    """A 'was' price above the price, which the price history can test (while price observations are on)."""
    was = facts.list_price
    return settings.price_observations and facts.price is not None and was is not None and was > facts.price


# What each extra needs on the page; an extra not listed always has something to look at.
_NEEDS: dict[str, Callable[[PageFacts], bool]] = {
    "photo_elsewhere": lambda f: bool(f.image_urls),
    "ad_photos": lambda f: bool(f.image_urls),
    "reviews": lambda f: bool(f.reviews),
    "brand": lambda f: bool(f.brand),
    "price_history": _was_price,
    "vat_register": lambda f: bool(f.vat_ids),
    "ad_price": lambda f: f.price is not None,
}


def _has_something(extra: str, facts: PageFacts) -> bool:
    """Whether the page gives a deep check's extra something to look at (a photo to search, reviews to read, …)."""
    needs = _NEEDS.get(extra)
    return needs is None or needs(facts)


def deep_page_type(state: AnalysisState, page_type: str) -> str:
    """
    The page type a deep check would give the page that a quick check classified as ``page_type``.

    The quick check classifies by its product rules only; a deep check (with the expansion classes on) first reads a
    listing on a known peer-to-peer or service platform as an ad or a provider's profile (an eBay business seller's
    listing as a marketplace listing).

    :param state: The analysis state (facts, URL, page text).
    :param page_type: The quick check's page type.
    :return: The page type.
    """
    facts = state.get("facts")
    if settings.expansion_classes and facts is not None and page_type == "storefront":
        settled = platforms.platform_rule(facts, state.get("url", ""), state.get("cleaned_content", ""))
        if settled is not None:
            return settled[0]
    return page_type


def _known_seller(page_type: str, facts: PageFacts) -> bool:
    """
    A seller a quick check can tell is no unknown: a big marketplace's own listing, or the brand's own on it.

    A big marketplace is one the quick check itself recognises by its rules (Amazon, bol.com, Walmart: the page type
    ``aggregate_listing``); its own listing is one that the listing skill doesn't call a reseller's
    (:func:`unfret.graph.skills.listing.resold`: the seller named is the brand or the marketplace's own retail arm, or
    none is named). Whether a web shop is a known brand's own takes outside evidence (Wikidata, the AI read), which
    only a deep check looks up: by the page alone, a three-week-old copy of a brand's shop looks the same.
    """
    return page_type == "aggregate_listing" and not resold(facts)


def _joined(labels: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    word = str(catalogue()["and"])
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} {word} {labels[-1]}"


def deep_check(verdict: Verdict, state: AnalysisState, *, concerns: set[str], available: set[str]) -> DeepCheck | None:
    """
    On a quick result: whether to suggest a deep check of the same page, and what it would add for this page.

    Worked out from the quick result alone, after the rest of the card, which it never changes:

    - "Worth a look" or "Think twice": suggested (the result is on the fence).
    - "Looks fine": suggested quietly, unless the seller is known to the quick check (:func:`_known_seller`): a quick
      check can't see a three-week-old domain or a supplier's photo.
    - "Serious concerns": not suggested; the advice is already to buy elsewhere.
    - Nothing a deep check would add here (:data:`DEEP_EXTRAS`, :func:`_has_something`): not suggested.

    :param verdict: The quick verdict, with its checklist.
    :param state: The analysis state (page type, facts).
    :param concerns: The checklist areas with a concern.
    :param available: The skills the quick check could run on this page.
    :return: The suggestion (without its cost: :func:`with_cost`), or ``None`` for a page type without one.
    """
    page_type = state.get("page_type", "storefront")
    facts = state.get("facts") or PageFacts()
    extras = DEEP_EXTRAS.get(deep_page_type(state, page_type))
    if extras is None:
        return None
    if verdict.band == "junk_or_scam":
        return DeepCheck(suggested=False, reason="serious_concerns")
    if verdict.band == "reliable" and _known_seller(page_type, facts):
        return DeepCheck(suggested=False, reason="known_seller")
    copy = catalogue()["deep_check"]
    found = [
        (key, areas)
        for key, areas, skill in extras
        if (skill is None or skill not in available) and _has_something(key, facts)
    ]
    found.sort(key=lambda e: not concerns & set(e[1]))  # stable: about a concern first, then in the table's order
    adds = [DeepCheckAdd(key=k, area=areas[0], label=str(copy["adds"][k])) for k, areas in found[:MAX_DEEP_ADDS]]
    if not adds:
        return DeepCheck(suggested=False, reason="nothing_to_add")
    quiet = verdict.band == "reliable"
    return DeepCheck(
        suggested=True,
        reason="unknown_seller" if quiet else "on_the_fence",
        quiet=quiet,
        adds=adds,
        text=str(copy["text"]).format(adds=_joined([a.label for a in adds])),
    )


def _dollars(usd_micros: int) -> str:
    """A price for the suggestion: "$6" for whole dollars, else as everywhere ("$0.20")."""
    text = format_usd(usd_micros)
    return text.removesuffix(".00")


def deep_check_cost(
    *, free_left: int = 0, paused: bool = False, bought: int = 0, x402_usd_micros: int | None = None
) -> tuple[str, DeepAction]:
    """
    What a deep check would cost the caller, in words, and whether they can run it now.

    Paid as a deep check is paid (``POST /v1/analyze``): a pass's free checks first while today's aren't all used,
    then bought checks; otherwise the smallest pack's card price. With x402, the deep tier's price.

    :param free_left: The pass's free checks left.
    :param paused: Whether today's free deep checks are all used.
    :param bought: The bought checks the balance covers.
    :param x402_usd_micros: The deep tier's x402 price, for a caller paying with x402.
    :return: The words and the action (``run_deep`` or ``get_checks``).
    """
    cost: dict[str, str] = catalogue()["deep_check"]["cost"]
    if x402_usd_micros is not None:
        return cost["x402"].format(price=_dollars(x402_usd_micros)), "run_deep"
    if free_left > 0 and not paused:
        return (cost["free_last"] if free_left == 1 else cost["free"].format(count=free_left)), "run_deep"
    if bought > 0:
        return cost["bought"].format(count=bought), "run_deep"
    smallest = packs.packs(settings)[0]
    text = cost["paused" if free_left > 0 else "packs"]
    return text.format(price=_dollars(smallest.card_usd_micros), checks=smallest.checks), "get_checks"


def with_cost(offer: DeepCheck, cost: tuple[str, DeepAction]) -> DeepCheck:
    """
    A suggestion with what it costs this caller and its one button (nothing for one that isn't suggested).

    :param offer: The suggestion (as the result cache keeps it, for whoever asks).
    :param cost: :func:`deep_check_cost`.
    :return: The suggestion for this caller.
    """
    if not offer.suggested:
        return offer
    text, action = cost
    label = str(catalogue()["deep_check"]["actions"][action])
    return offer.model_copy(update={"cost": text, "action": action, "action_label": label})


def incomplete(features: dict[str, float]) -> Incomplete | None:
    """
    The card's line when a key step didn't finish: the same condition as the assessment's ``degraded``.

    A key step (:data:`unfret.scoring.KEY_SKILLS`: the AI read of the page) that failed or ran out of time leaves the
    verdict on less evidence, usually milder than it should be (its findings are among the strongest). The line names
    the step and says what the check cost: a degraded result is charged like any other (only a page that couldn't be
    assessed, a failed analysis or a client that hung up is given back), and the same input checked again within the
    hour gets this result back from the result cache (:data:`unfret.result_cache.RESULT_TTL_S`).

    :param features: The analysis's features.
    :return: The line, or ``None`` when every key step finished.
    """
    steps = sorted(scoring.unavailable_skills(features) & scoring.KEY_SKILLS)
    return Incomplete(steps=steps, text=incomplete_text(steps)) if steps else None


def incomplete_text(steps: list[str], *, public: bool = False) -> str:
    """
    The words of :func:`incomplete`'s line.

    :param steps: The steps that didn't finish (keys of the catalogue's ``incomplete.steps``).
    :param public: For a shared result's page, whose readers didn't pay for the check: without what it cost.
    :return: The line.
    """
    copy = catalogue()["incomplete"]
    text = str(copy["text"]).format(steps=_joined([str(copy["steps"][step]) for step in steps]))
    return text if public else f"{text} {copy['after']}"


def charge_note(
    *,
    tier: str,
    score: int,
    charged: bool | None,
    free_check: bool,
    free_left: int | None = None,
    bought_left: int | None = None,
) -> str:
    """
    What this check cost the caller, as one sentence every client shows under the result.

    :param tier: ``quick`` or ``deep``.
    :param score: The score (-1: the page couldn't be assessed).
    :param charged: Whether the check used one of the caller's checks.
    :param free_check: Whether that was one of the free ones.
    :param free_left: The free checks left after it, when known.
    :param bought_left: The bought checks left after it, when known.
    :return: The sentence: "This check used one of your free checks; 4 are left.", "Quick checks are free.", …
    """
    copy: dict[str, str] = catalogue()["charge"]
    if tier == "quick":
        return copy["quick"]
    if score < 0:
        return copy["not_assessed"]
    if not charged:
        return copy["repeat"]
    kind, left = ("free", free_left) if free_check else ("bought", bought_left)
    if left is None:
        return copy[kind]
    words = copy["left_none"] if left <= 0 else copy["left_one"] if left == 1 else copy["left_many"].format(count=left)
    return copy[f"{kind}_left"].format(left=words)


def _photos_once(verdict: Verdict) -> None:
    """With the same product shown up top (its photos, the other brand names), its reasons don't show them again."""
    if verdict.same_product is not None:
        for reason in verdict.reasons:
            if reason.type in _SHOWN_WITH_SAME_PRODUCT:
                reason.thumbnails = []


def complete(
    verdict: dict[str, Any], state: AnalysisState, *, available: Iterable[Skill], applicable: Iterable[Skill]
) -> dict[str, Any]:
    """
    Add the card to a verdict: sources, the checklist, the checks, the next step, the same product, a missing step.

    The last is the line that says a key step didn't finish (:func:`incomplete`).

    :param verdict: The verdict as the judge returned it (:meth:`Verdict.model_dump`).
    :param state: The analysis state (features, page type, the image index's same-product match).
    :param available: The skills this analysis could run (the quick tier's are fewer).
    :param applicable: All skills, as a full analysis would run them.
    :return: The verdict with the card's fields.
    """
    page_type = state.get("page_type", "storefront")
    kind = kind_of(page_type)
    parsed = Verdict.model_validate(verdict)
    features = state.get("features", {})
    for reason in parsed.reasons:
        reason.source = source_label(_archive_source(REASON_SOURCES.get(reason.type, "page"), reason.links, features))
    for positive in parsed.positives:
        positive.source = source_label(_archive_source(POSITIVE_SOURCES.get(positive.type, "page"), [], features))
    titles = {r.type: r.title for r in parsed.reasons}
    if kind == "shop" and parsed.band != "reliable":
        # Every reason that fired, not only the three shown: an area with a finding is never ticked as fine. (A
        # reliable verdict leaves out the minor ones on purpose; it shows its one note.)
        facts = state.get("facts") or PageFacts()
        for key in shopper.fired_reasons(
            features=features,
            findings=state.get("findings", []),
            facts=facts,
            page_type=page_type,
            market=shop_market(facts, state.get("url", ""), state.get("cleaned_content", "")),
        ):
            titles.setdefault(key, str(catalogue()["reasons"][key]["title"]))
    # The card orders reasons by level (worst first), then by how strong their evidence is.
    parsed.reasons.sort(key=lambda r: levels.order_key(r.type))
    if parsed.risk is None:
        parsed.risk = levels.from_band(
            parsed.band,
            sorted(titles, key=lambda t: (levels.order_key(t), shopper.evidence_rank(t))),
            positive_types=[p.type for p in parsed.positives],
            titles=titles,
        )
    concerns: dict[str, str] = {}
    for key, title in titles.items():
        if (area := area_of(key, kind)) is not None:
            concerns.setdefault(area, title)
    if parsed.band != "reliable":
        concerns |= _sign_concerns(small_signs(state), kind, set(concerns))
    found = state.get("same_product")
    parsed.same_product = SameProduct.model_validate(found) if found and kind == "shop" else None
    _photos_once(parsed)
    ran = {s.name for s in available}
    parsed.checklist, parsed.checks_run = checklist(
        kind,
        features,
        concerns,
        available={s.name for s in available if page_type in s.page_types},
        applicable={s.name for s in applicable if page_type in s.page_types},
    )
    parsed.next_step = next_step(parsed, kind, parsed.same_product is not None)
    parsed.incomplete = incomplete(features)
    if {s.name for s in applicable} - ran:  # a quick check: everything above is its verdict, unchanged
        marked = {i.key for i in parsed.checklist if i.status == "concern"}
        parsed.deep_check = deep_check(parsed, state, concerns=marked, available=ran)
    return parsed.model_dump()
