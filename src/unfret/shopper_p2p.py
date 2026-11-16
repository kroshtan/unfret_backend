"""
The shopper verdict for a peer-to-peer ad: cautious hand-written rules until a scorer is trained on labelled ads.

The same :class:`~unfret.shopper.Verdict` as for products (a band, up to three reasons, the positives, a headline),
so the extension and the link checker show it unchanged; the copy is in ``copy/en.json`` with the product copy.

The rules, from the p2p skills' features (``seller_profile``, ``off_platform``, ``price_outlier``,
``duplicate_ads``, ``reverse_image``) and the shared ones (``content``, ``injection``):

- **Many warning signs** (``junk_or_scam``) only for the strong combinations: payment pushed off the platform
  (up front, untraceable, protection refused, "I'm abroad", or a bank transfer for an item that is only shipped)
  together with a weak or copied seller (a new account, no ratings, many ads at once, text, photo or phone number
  seen in other ads) or a price far below the usual; a photo from other ads with a price far below the usual; or
  three serious reasons at once.
- **Some warning signs** (``careful``): any serious reason on its own, or two of the middling ones.
- **Looks fine** (``reliable``) only when nothing at all fired and the positives are concrete: an account of two
  years or more, well rated, and the platform's payment protection, pickup or a checked normal price; and the ad's
  own text was read. Never for lack of evidence.
- **A few things to check** (``concerns``) otherwise, with "little to go on" as the reason when nothing fired.

Wording is factual, never "scam": what the ad says or shows, and what that usually means for a buyer.
"""

from dataclasses import dataclass
from typing import Any

from unfret import shopper
from unfret.config import settings
from unfret.facts.schema import PageFacts
from unfret.graph.skills.off_platform import names_a_way_off
from unfret.graph.state import Finding

# The ad reasons' severities and tie-break order live with the product ones (``shopper.REASON_SEVERITY``, the
# ``p2p_`` keys), and so do the positives' keys.
# Shared reasons that apply to ads as they do to shops.
_SHARED = ("hidden_ai_text", "ai_text", "pushy_copy")
# Reasons that tell the same story; only the first (strongest) of each group is shown.
_GROUPS = (
    ("p2p_untraceable_payment", "p2p_bank_transfer", "p2p_llm_off_platform"),
    ("p2p_photo_in_other_ads", "p2p_photo_on_shops"),
    ("hidden_ai_text", "ai_text"),
)
_PAYMENT_PUSH = ("p2p_advance_payment", "p2p_untraceable_payment", "p2p_protection_refused", "p2p_seller_abroad")
_WEAK_SELLER = ("p2p_new_account", "p2p_no_ratings", "p2p_listing_burst")
_COPIED = ("p2p_photo_in_other_ads", "p2p_duplicate_text", "p2p_shared_contact")
SHARED_CONTACT_MIN = 3  # one number in two or three ads is one seller with a few things to sell
LLM_PUSH_MIN = 0.7
_RANK = {"high": 0, "medium": 1, "low": 2}
_PAYMENT_METHODS = {
    "gift card": "gift cards",
    "crypto": "crypto",
    "money transfer": "a money transfer service",
    "payment app": "a payment app without buyer protection",
}
_CHANNELS = ("WhatsApp", "Telegram", "phone", "e-mail")


@dataclass
class _Ad:
    features: dict[str, float]
    findings: dict[str, list[Finding]]
    facts: PageFacts

    def f(self, name: str) -> float:
        return float(self.features.get(name, 0.0))

    def first(self, finding_type: str) -> Finding | None:
        found = self.findings.get(finding_type)
        return found[0] if found else None

    def quote(self, finding_type: str) -> str:
        return shopper._quoted(self.first(finding_type)) or "…"


def _severity(key: str) -> shopper.Severity:
    return shopper.REASON_SEVERITY[key]


def _payment(ad: _Ad, fired: dict[str, dict[str, str]]) -> None:
    """Payment and contact pushed off the platform (the off_platform rules, then the LLM's reading)."""
    if ad.f("off_platform.advance_payment") >= 1:
        fired["p2p_advance_payment"] = {"quote": ad.quote("advance_payment")}
    if ad.f("off_platform.untraceable_payment") >= 1:
        found = ad.first("untraceable_payment")
        method = next((v for k, v in _PAYMENT_METHODS.items() if found and k in found.evidence), "")
        fired["p2p_untraceable_payment"] = {"quote": ad.quote("untraceable_payment"), "method": method or "…"}
    if ad.f("off_platform.protection_refused") >= 1:
        fired["p2p_protection_refused"] = {"quote": ad.quote("protection_refused")}
    if ad.f("off_platform.abroad") >= 1:
        fired["p2p_seller_abroad"] = {"quote": ad.quote("abroad")}
    if ad.f("off_platform.bank_transfer") >= 1:
        fired["p2p_bank_transfer"] = {"quote": ad.quote("bank_transfer")}
    if ad.f("off_platform.shipping_only") >= 1:
        fired["p2p_shipping_only"] = {"quote": ad.quote("shipping_only")}
    if ad.f("off_platform.contact_outside") >= 1:
        found = ad.first("contact_outside")
        channels = [c for c in _CHANNELS if found and c in found.evidence]
        fired["p2p_contact_outside"] = {
            "quote": ad.quote("contact_outside"),
            "channel": " or ".join(channels) or "other means",
        }
    rules = ad.f("off_platform.signals") >= 1
    llm_quote = ad.first("off_platform_quote")
    if (
        not rules
        and ad.f("content.off_platform_push") >= LLM_PUSH_MIN
        and llm_quote is not None
        and names_a_way_off(llm_quote.evidence)
    ):
        # Only a quote naming a channel or payment outside the platform: "contact us for prices" is not one.
        fired["p2p_llm_off_platform"] = {"quote": shopper._quote(llm_quote.evidence)}


def _seller(ad: _Ad, fired: dict[str, dict[str, str]]) -> None:
    """The seller's account as the platform shows it."""
    days = ad.f("seller_profile.account_days")
    if ad.f("seller_profile.new_account") >= 1:
        fired["p2p_new_account"] = {"age": _age(days)}
    if ad.f("seller_profile.no_ratings") >= 1:
        fired["p2p_no_ratings"] = {}
    if ad.f("seller_profile.low_rating") >= 1 and (low := ad.first("low_rating")):
        fired["p2p_low_rating"] = {"rating": low.evidence.removeprefix("The seller is rated ").rstrip(".")}
    if ad.f("seller_profile.listing_burst") >= 1:
        listings = ad.facts.seller_profile.other_listings if ad.facts.seller_profile else None
        fired["p2p_listing_burst"] = {"count": f"{listings or 0:,}", "age": _age(days)}
    if ad.f("seller_profile.slow_responses") >= 1:
        said = ad.facts.seller_profile.response_time if ad.facts.seller_profile else ""
        fired["p2p_slow_responses"] = {"said": f"“{said}”" if said else "…"}


def _copies(ad: _Ad, fired: dict[str, dict[str, str]]) -> None:
    """The same photo, text or phone number in other ads, the price against the usual."""
    if (count := int(ad.f("reverse_image.other_ads"))) >= 1 and (photo := ad.first("photo_in_other_ads")):
        fired["p2p_photo_in_other_ads"] = {
            "count": f"{count:,}",
            "ads": "ad" if count == 1 else "ads",
            "platforms": photo.evidence.rpartition("(")[2].rstrip(").") if "(" in photo.evidence else "…",
            "thumbnails": " ".join(photo.thumbnails),
        }
    if (shops := int(ad.f("reverse_image.other_shops"))) >= 1 and (photo := ad.first("photo_on_other_shops")):
        fired["p2p_photo_on_shops"] = {
            "count": f"{shops:,}",
            "shops": shopper.catalogue()["shop_one" if shops == 1 else "shop_many"],
            "names": shopper._listed(photo.evidence),
            "thumbnails": " ".join(photo.thumbnails),
        }
    if ad.f("price_outlier.far_below") >= 1 and (low := ad.first("price_far_below")):
        fired["p2p_price_far_below"] = {"evidence": low.evidence}
    if (copies := int(ad.f("duplicate_ads.text_copies"))) >= 1 and (dup := ad.first("duplicate_text")):
        fired["p2p_duplicate_text"] = {
            "count": f"{copies:,}",
            "ads": "ad" if copies == 1 else "ads",
            "platforms": dup.evidence.rpartition("(")[2].rstrip(").") if "(" in dup.evidence else "…",
        }
    if (shared := int(ad.f("duplicate_ads.contact_ads"))) >= SHARED_CONTACT_MIN:
        fired["p2p_shared_contact"] = {"count": f"{shared:,}"}


def _shared(ad: _Ad, fired: dict[str, dict[str, str]]) -> None:
    """Text aimed at AI tools and pressure wording, as on product pages."""
    if ad.f("injection.instruction_hidden") >= 1:
        fired["hidden_ai_text"] = {}
    if ad.f("injection.instruction_visible") >= 1 or ad.f("content.addresses_ai") >= 1:
        fired["ai_text"] = {}
    if ad.f("content.scam_copy") >= 0.6:
        fired["pushy_copy"] = {}


def _age(days: float) -> str:
    units = shopper.catalogue()["age_units"]
    days = max(int(days), 1)
    for limit, size, unit in ((14, 1, "day"), (60, 7, "week"), (730, 30, "month")):
        if days < limit:
            n = max(1, round(days / size))
            return f"{n} {units[unit][0 if n == 1 else 1]}"
    n = days // 365
    return f"{n} {units['year'][0 if n == 1 else 1]}"


def _positives(ad: _Ad) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    profile = ad.facts.seller_profile
    if ad.f("seller_profile.established") >= 1:
        found["p2p_established"] = {"age": _age(ad.f("seller_profile.account_days"))}
    if ad.f("seller_profile.well_rated") >= 1 and profile is not None:
        count = f"{profile.rating_count or 0:,}"
        if profile.rating is not None:
            found["p2p_well_rated"] = {"count": count, "rating": f"{profile.rating:g} out of 5 on average"}
        elif ad.facts.seller_rating is not None:
            found["p2p_well_rated"] = {"count": count, "rating": f"{ad.facts.seller_rating:g}% positive"}
    if ad.f("seller_profile.verified") >= 1 and profile is not None:
        badges = ", ".join(b for b in profile.badges if b.startswith("Verified"))
        found["p2p_verified"] = {"badges": badges.lower()}
    listing = ad.facts.listing
    if listing is not None and listing.payment_protection and ad.f("off_platform.protection_refused") < 1:
        found["p2p_protection"] = {}
    if listing is not None and "pickup" in listing.delivery and ad.f("off_platform.shipping_only") < 1:
        found["p2p_pickup"] = {}
    if ad.f("price_outlier.normal") >= 1:
        found["p2p_normal_price"] = {}
    return found


def _band(fired: dict[str, dict[str, str]], positives: dict[str, Any], ad: _Ad) -> shopper.Band:
    keys = set(fired)
    high = [k for k in keys if _severity(k) == "high"]
    medium = [k for k in keys if _severity(k) == "medium"]
    push = bool(keys & set(_PAYMENT_PUSH)) or {"p2p_bank_transfer", "p2p_shipping_only"} <= keys
    weak = bool(keys & set(_WEAK_SELLER))
    copied = bool(keys & set(_COPIED))
    cheap = "p2p_price_far_below" in keys
    if (push and (weak or copied or cheap)) or ("p2p_photo_in_other_ads" in keys and cheap) or len(high) >= 3:
        return "junk_or_scam"
    if high or len(medium) >= 2 or (cheap and weak):
        return "careful"
    concrete = (
        "p2p_established" in positives
        and "p2p_well_rated" in positives
        and bool({"p2p_protection", "p2p_pickup", "p2p_normal_price"} & set(positives))
        and ad.f("off_platform.text_read") >= 1
    )
    return "reliable" if not keys and concrete else "concerns"


def _scores() -> dict[shopper.Band, int]:
    reliable = max(settings.verdict_reliable_min, 85)
    concerns = min(max(60, settings.verdict_concerns_min), settings.verdict_reliable_min - 1)
    careful = min(max(40, settings.verdict_careful_min), settings.verdict_concerns_min - 1)
    junk = max(min(15, settings.verdict_careful_min - 1), 0)
    return {"reliable": min(reliable, 100), "concerns": concerns, "careful": careful, "junk_or_scam": junk}


def _reason(key: str, params: dict[str, str]) -> shopper.Reason:
    copy = shopper.catalogue()["reasons"][key]
    return shopper.Reason(
        type=key,
        title=copy["title"],
        detail=copy["detail"].format(**params),
        severity=_severity(key),
        thumbnails=params.get("thumbnails", "").split(),
    )


def _positive(key: str, params: dict[str, str]) -> shopper.Positive:
    copy = shopper.catalogue()["positives"][key]
    return shopper.Positive(type=key, title=copy["title"], detail=copy["detail"].format(**params))


def build(*, features: dict[str, float], findings: list[Finding], facts: PageFacts) -> tuple[int, shopper.Verdict]:
    """
    The verdict for a peer-to-peer ad.

    :param features: The skill features from the graph state.
    :param findings: The skill findings.
    :param facts: The page facts.
    :return: The 0–100 score that goes with the band (for clients that read it) and the verdict.
    """
    by_type: dict[str, list[Finding]] = {}
    for finding in findings:
        by_type.setdefault(finding.type, []).append(finding)
    ad = _Ad(features, by_type, facts)
    fired: dict[str, dict[str, str]] = {}
    for rules in (_payment, _seller, _copies, _shared):
        rules(ad, fired)
    positives = _positives(ad)
    band = _band(fired, positives, ad)
    for group in _GROUPS:
        for key in [k for k in group if k in fired][1:]:
            del fired[key]
    order = [*(k for k in shopper.REASON_SEVERITY if k.startswith("p2p_")), *_SHARED]
    keys = sorted(fired, key=lambda k: (_RANK[_severity(k)], order.index(k)))
    if band == "concerns" and not keys:
        keys, fired = ["p2p_little_known"], {"p2p_little_known": {}}
    reasons = [_reason(k, fired[k]) for k in keys][:3]
    shown = [_positive(k, v) for k, v in positives.items()]
    shown = (
        shown[:3]
        if band == "reliable"
        else shown[:2]
        if band == "concerns"
        else shown[:1]
        if band == "careful"
        else []
    )
    copy = shopper.catalogue()
    if reasons:
        headline = shopper.template_headline(band, keys[:2], reasons, copy)
    else:
        headline = copy["class_headlines"]["p2p_listing"][band]
    verdict = shopper.Verdict(
        band=band, label=copy["labels"][band], headline=headline, reasons=reasons, positives=shown
    )
    return _scores()[band], verdict
