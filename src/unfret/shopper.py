"""
The shopper-facing verdict: a band in words, at most three plain-language reasons and the things that look good.

The scorer's ``assessment`` (risk, confidence, weighted feature flags) is for the details view and for agents; this
module turns the same evidence into what a shopper reads. Every reason comes from a curated copy catalogue
(``copy/en.json``; the app is in English), fires only when a skill measured something concrete, and never shows a
feature name or a raw value. Structural facts (is it a marketplace, is it on Shopify, does it have reviews)
describe the page, not its reliability, so they never surface. The headline sentence is written separately (by the
judge's LLM, or taken from the catalogue's template for the band).
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from unfret.config import settings
from unfret.facts.schema import PageFacts
from unfret.graph.state import Finding

Band = Literal["reliable", "concerns", "careful", "junk_or_scam"]
Severity = Literal["high", "medium", "low"]
LANGUAGE = "en"  # everything the app writes itself is in English
COPY_DIR = Path(__file__).with_name("copy")

# Severity per reason; the order is also the tie-break between reasons of equal severity.
REASON_SEVERITY: dict[str, Severity] = {
    "timer_resets": "high",
    "fake_urgency_app": "medium",
    "countdown": "low",
    "preticked_addon": "high",
    "drip_pricing": "high",
    "late_fees": "low",
    "review_velocity": "high",
    "review_shape": "medium",
    "duplicate_reviews": "medium",
    "off_topic_reviews": "high",
    "review_import_app": "medium",
    "word_soup": "medium",
    "generic_brand": "medium",
    "generic_product": "medium",
    "implausible_claims": "medium",
    "pushy_copy": "medium",
    "big_discount": "medium",
    "was_never_charged": "high",
    "implausible_discount": "high",
    "reference_anchor": "medium",
    "permanent_sale": "medium",
    "seller_not_brand": "low",
    "ships_from_far": "low",
    "dropshipping_tools": "medium",
    "supplier_links": "medium",
    "photo_reused": "medium",
    "linked_new_shops": "medium",
    "missing_trust_info": "medium",
    "no_withdrawal": "medium",
    "no_return_policy": "medium",
    "no_imprint": "medium",
    "invalid_vat": "high",
    "new_domain": "high",
    "short_registration": "low",
    "returns_abroad": "medium",
    "return_address_withheld": "medium",
    "restocking_fee": "medium",
    "origin_doubt": "medium",
    "long_delivery": "low",
    "mailbox_address": "low",
    "hidden_ai_text": "high",
    "ai_text": "medium",
    "overall": "low",
}
POSITIVE_KEYS = (
    "known_brand",
    "sold_by_brand",
    "vat_registered",
    "us_business",
    "established_domain",
    "trust_info_present",
    "natural_reviews",
    "nothing_found",
)
# Features that describe the page rather than judge it (what kind of page, how many ratings, whether a policy is
# shown): when the scorer leans on them they're shown as neutral context, never as an accusation.
CONTEXT_FEATURES = frozenset(
    {
        "content.brand_recognized",
        "listing.title_length",
        "listing.variations",
        "listing.log_rating_count",
        "listing.rating",
        "listing.is_marketplace",
        "listing.is_shopify",
        "reviews.has_reviews",
        "reviews.share_five",
        "reviews.share_one",
        "reviews.share_middle",
        "reviews.verified_share",
        "reviews.title_similarity_mean",
        "eu_registry.vat_present",
        "eu_registry.vat_valid",
        "eu_registry.vat_name_match",
        "eu_registry.withdrawal_right",
        "eu_registry.imprint",
        "domain_age.known",
        "domain_age.age_years",
        "domain_age.term_years",
        "domain_age.redacted",
        "domain_age.cert_age_years",
        "reverse_image.searched",
    }
)
# Reasons that only say something is missing from the page (which a page fetched without its footer, or a shop
# outside the EU, can lack without being any less reliable).
ABSENCE_REASONS = ("no_withdrawal", "no_return_policy", "no_imprint", "missing_trust_info")
# Reasons that tell the same story; only the first (strongest) of each group is shown.
_GROUPS = (
    ("timer_resets", "fake_urgency_app", "countdown"),
    ("generic_brand", "generic_product"),
    ABSENCE_REASONS,
    ("hidden_ai_text", "ai_text"),
    ("was_never_charged", "implausible_discount", "big_discount"),
    ("returns_abroad", "return_address_withheld"),
)
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_NUMBERS_RE = re.compile(r"\d+(?:\.\d+)?")
_QUOTED_RE = re.compile(r'"([^"]{1,150})"')


class Reason(BaseModel):
    """Something that lowered the score, for a shopper."""

    type: str
    title: str
    detail: str
    severity: Severity
    thumbnails: list[str] = Field(
        default_factory=list, max_length=3, description="Up to three thumbnail URLs of the matching photos."
    )


class Positive(BaseModel):
    """Something that looks good, for a shopper."""

    type: str
    title: str
    detail: str


class Verdict(BaseModel):
    """The shopper-facing result of an analysis."""

    band: Band
    label: str = Field(description="The band in words.")
    headline: str = Field(description="One plain sentence explaining the verdict.")
    reasons: list[Reason] = Field(default_factory=list, max_length=3)
    positives: list[Positive] = Field(default_factory=list, max_length=3)
    language: str = Field(default=LANGUAGE, description="Always en (kept for clients that read it).")


@cache
def catalogue() -> dict[str, Any]:
    """
    The copy catalogue (``copy/en.json``).

    :return: Labels, headline templates, reason and positive copy, and small phrases.
    """
    data: dict[str, Any] = json.loads((COPY_DIR / f"{LANGUAGE}.json").read_text(encoding="utf-8"))
    return data


HEADLINE_MAX_WORDS = 25


def template_headline(band: Band, fired: list[str], reasons: list[Reason], copy: dict[str, Any]) -> str:
    """
    The headline without an LLM: the verdict label and its (up to two) strongest concerns, else the band's sentence.

    :param band: The verdict band.
    :param fired: The keys of the reasons that fired, strongest first.
    :param reasons: The verdict's reasons (same order).
    :param copy: The copy catalogue.
    :return: The headline.
    """
    sentence: str = copy["headlines"][band]
    if band == "reliable" or not fired:
        return sentence
    titles = [r.title[0].lower() + r.title[1:] for r in reasons[: len(fired)] if r.title]
    for count in (2, 1):
        if len(titles) >= count:
            joined = f" {copy['and']} ".join(titles[:count])
            headline = str(copy["headline_reasons"].format(label=copy["labels"][band], reasons=joined))
            if len(headline.split()) <= HEADLINE_MAX_WORDS:
                return headline
    return sentence


def fit_headline(text: str) -> str | None:
    """
    A written headline, if it is one sentence of at most :data:`HEADLINE_MAX_WORDS` words.

    :param text: The LLM's sentence.
    :return: The sentence, stripped, or ``None`` when it is empty or too long (the template is used instead).
    """
    text = " ".join(text.split())
    if not text or len(text.split()) > HEADLINE_MAX_WORDS:
        return None
    return text


def feature_label(name: str) -> str:
    """
    The human label of a skill feature (``content.generic_brand`` → "Unknown brand").

    :param name: The prefixed feature name.
    :return: The label; a neutral fallback for a feature the catalogue doesn't know yet.
    """
    labels: dict[str, str] = catalogue()["features"]
    return labels.get(name) or catalogue()["labels"]["concerns"]


def label_flags(flags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Add ``label``, ``kind`` and ``severity`` to the scorer's flags, so no client shows a feature name.

    ``kind`` is ``context`` for features that describe the page (see :data:`CONTEXT_FEATURES`), ``signal``
    otherwise; ``severity`` follows the flag's weight (its log-odds contribution).

    :param flags: The assessment's flags (``type``, ``evidence``, ``weight``).
    :return: The same flags with the human fields added.
    """
    labelled = []
    for flag in flags:
        weight = float(flag.get("weight", 0))
        severity: Severity = "high" if weight >= 1.0 else "medium" if weight >= 0.4 else "low"
        context = flag.get("type") in CONTEXT_FEATURES
        labelled.append(
            flag
            | {
                "label": feature_label(str(flag.get("type"))),
                "kind": "context" if context else "signal",
                "severity": "low" if context else severity,
            }
        )
    return labelled


def band_for(score: int) -> Band:
    """
    The verdict band for a 0–100 reliability score (thresholds are settings).

    :param score: Reliability, 100 = fully reliable.
    :return: The band.
    """
    if score >= settings.verdict_reliable_min:
        return "reliable"
    if score >= settings.verdict_concerns_min:
        return "concerns"
    if score >= settings.verdict_careful_min:
        return "careful"
    return "junk_or_scam"


@dataclass
class _Evidence:
    features: dict[str, float]
    findings: dict[str, list[Finding]]
    facts: PageFacts
    page_type: str
    market: str = "eu"

    def f(self, name: str) -> float:
        return float(self.features.get(name, 0.0))

    def has(self, name: str) -> bool:
        return name in self.features

    def first(self, finding_type: str) -> Finding | None:
        found = self.findings.get(finding_type)
        return found[0] if found else None

    def phrase(self, key: str) -> str:
        return str(catalogue()[key])

    def age(self, days: int) -> str:
        units = catalogue()["age_units"]
        for limit, size, unit in ((14, 1, "day"), (60, 7, "week"), (730, 30, "month")):
            if days < limit:
                n = max(1, round(days / size))
                return f"{n} {units[unit][0 if n == 1 else 1]}"
        n = round(days / 365)
        return f"{n} {units['year'][0 if n == 1 else 1]}"

    def money(self, amount: str) -> str:
        symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get((self.facts.currency or "").upper(), "")
        try:
            text = f"{float(amount):,.2f}"
        except ValueError:
            return amount
        return f"{symbol}{text}" if symbol else text

    def number(self, n: int) -> str:
        return f"{n:,}"

    @property
    def storefront(self) -> bool:
        return self.page_type == "storefront"

    @property
    def marketplace(self) -> bool:
        return self.page_type == "aggregate_listing"


def _pct(share: float) -> str:
    return f"{round(share * 100)}%"


def _quote(text: str) -> str:
    return f"“{text.strip()[:120]}”"


def _quoted(finding: Finding | None) -> str:
    if finding is None:
        return ""
    m = _QUOTED_RE.search(finding.evidence)
    return _quote(m.group(1)) if m else ""


def _pressure(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Countdowns, pre-ticked extras and prices that change at checkout."""
    if e.f("dark_patterns.timer_resets") >= 1:
        fired["timer_resets"] = {}
    if e.storefront and e.f("html_source_signals.timer_app") >= 1:
        app = e.first("timer_app")
        fired["fake_urgency_app"] = {"app": app.evidence[:60] if app else e.phrase("an_urgency_tool")}
    if e.storefront and e.f("dark_patterns.timer") >= 1:
        fired["countdown"] = {}
    if e.f("dark_patterns.preticked_addons") >= 1:
        fired["preticked_addon"] = {"label": _quoted(e.first("preticked_addon")) or "…"}
    if e.f("dark_patterns.checkout_markup_pct") >= 0.1:
        fired["drip_pricing"] = {"pct": _pct(e.f("dark_patterns.checkout_markup_pct"))}
    if e.storefront and e.f("dark_patterns.fee_mentions") >= 1:
        fired["late_fees"] = {}
    if e.f("content.scam_copy") >= 0.6:
        fired["pushy_copy"] = {}
    if e.f("listing.discount_pct") >= 0.5:
        fired["big_discount"] = {"pct": _pct(e.f("listing.discount_pct"))}
    _discounts(e, fired)


def _discounts(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Fake discounts: implausible depth, reference-price anchors, endless sales, a 'was' price never charged."""
    if e.f("pricing.was_above_30d_low") >= 1 and (seen := e.first("was_never_charged")):
        # "Lowest price in the last 30 days: 19.95; 'was' price: 79.95.": the prices are the last two numbers.
        low, was = _NUMBERS_RE.findall(seen.evidence)[-2:] or ("?", "?")
        fired["was_never_charged"] = {"low": e.money(low), "was": e.money(was)}
    if e.f("pricing.implausible_discount") >= 1:
        fired["implausible_discount"] = {"pct": _pct(e.f("listing.discount_pct"))}
    if e.f("pricing.reference_anchor") >= 1:
        fired["reference_anchor"] = {"anchor": _quoted(e.first("reference_anchor")) or "…"}
    if e.storefront and e.f("pricing.permanent_sale") >= 1:
        fired["permanent_sale"] = {"phrase": _quoted(e.first("permanent_sale")) or "…"}


def _reviews(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Review counts, rating shape, copied and borrowed reviews."""
    if e.first("review_velocity") and e.facts.rating_count and e.facts.date_first_available:
        days = (datetime.now(tz=UTC).date() - e.facts.date_first_available).days
        fired["review_velocity"] = {"count": e.number(e.facts.rating_count), "age": e.age(max(days, 1))}
    if e.f("reviews.j_shaped") >= 1:
        fired["review_shape"] = {}
    if e.f("reviews.duplicate_share") >= 0.15:
        fired["duplicate_reviews"] = {"pct": _pct(e.f("reviews.duplicate_share"))}
    if e.f("reviews.off_topic_share") >= 0.3:
        fired["off_topic_reviews"] = {"pct": _pct(e.f("reviews.off_topic_share"))}
    if e.f("html_source_signals.review_import_app") >= 1:
        fired["review_import_app"] = {}


def _product(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """White-label goods: word-soup titles, made-up brands, implausible claims, who sells and ships it."""
    if e.f("content.title_word_soup") >= settings.verdict_llm_signal_min:
        fired["word_soup"] = {}
    # On a shop's own page "not a brand I know" is true of half the honest small shops in the corpus (160 of 321),
    # so as a reason it's only said on marketplace listings, where it means white-label goods. The model still
    # weighs it everywhere.
    llm_says_generic = e.marketplace and e.f("content.generic_brand") >= settings.verdict_llm_signal_min
    if llm_says_generic or e.first("brand_name"):
        fired["generic_brand"] = {"brand": _quote(e.facts.brand) if e.facts.brand else e.phrase("this_brand")}
    if e.f("content.generic_product") >= 0.7:
        fired["generic_product"] = {}
    claim = e.first("implausible_claim")
    if e.f("content.implausible_claims") >= 0.6 and claim:
        fired["implausible_claims"] = {"claim": _quote(claim.evidence)}
    if e.marketplace and e.f("listing.seller_not_brand") >= 1 and e.facts.seller:
        fired["seller_not_brand"] = {"seller": _quote(e.facts.seller)}
    if e.f("listing.ships_from_china") >= 1:
        fired["ships_from_far"] = {"place": e.facts.ships_from[:60] or "China"}


def _supply(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Traces of the supplier behind the shop: dropshipping tools and wholesale links."""
    if e.f("html_source_signals.dropship_app") >= 1:
        fired["dropshipping_tools"] = {}
    if e.f("html_source_signals.supplier_trace") >= 1:
        fired["supplier_links"] = {}
    # The same photo on other shops is normal for brands and their resellers: a warning only for an unknown brand
    # with other signs of a resold supplier product.
    photo = e.first("photo_on_other_shops")
    if photo and e.f("reverse_image.other_shops") >= 1 and e.f("content.brand_recognized") < 1 and _resale_context(e):
        count = int(e.f("reverse_image.other_shops"))
        fired["photo_reused"] = {
            "count": e.number(count),
            "shops": e.phrase("shop_one" if count == 1 else "shop_many"),
            "names": _listed(photo.evidence),
            "thumbnails": " ".join(photo.thumbnails),
        }


def _resale_context(e: _Evidence) -> bool:
    """Signs that a product is a resold supplier item rather than a brand's own."""
    return (
        e.f("domain_age.under_6_months") >= 1
        or e.f("content.generic_product") >= 0.5
        or e.f("content.title_word_soup") >= 0.5
        or e.f("listing.generated_brand") >= 0.5
        or e.f("html_source_signals.dropship_app") >= 1
        or e.f("html_source_signals.supplier_trace") >= 1
        or e.f("shop_links.linked_young_shops") >= 1
    )


def _listed(evidence: str) -> str:
    """The names after the colon of a "…: a, b and others." finding."""
    return evidence.partition(": ")[2].rstrip(".")[:160]


def _network(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Other shops run with the same accounts that are themselves young (storefronts only)."""
    young = e.first("linked_young_shops")
    if e.storefront and young and (count := int(e.f("shop_links.linked_young_shops"))) >= 1:
        fired["linked_new_shops"] = {
            "count": e.number(count),
            "shops": e.phrase("shop_one" if count == 1 else "shop_many"),
            "names": _listed(young.evidence),
        }


def _shop(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """A web shop's own identity: return policy, company details, VAT number, domain age (storefronts only)."""
    if not e.storefront:
        return
    if e.has("eu_registry.withdrawal_right") and e.f("eu_registry.withdrawal_right") < 1:
        # Only EU law requires a withdrawal right. Outside the EU, eu_registry reports a missing policy as not
        # applicable, so this fires there only for features recorded before that (never citing EU law).
        fired["no_withdrawal" if e.market == "eu" else "no_return_policy"] = {}
    if e.has("eu_registry.imprint") and e.f("eu_registry.imprint") < 1:
        fired["no_imprint"] = {}
    if e.f("content.missing_trust_info") >= 0.6:
        fired["missing_trust_info"] = {}
    if e.f("eu_registry.vat_present") >= 1 and e.has("eu_registry.vat_valid") and e.f("eu_registry.vat_valid") < 1:
        fired["invalid_vat"] = {}
    if e.f("domain_age.under_6_months") >= 1:
        fired["new_domain"] = {"age": e.age(max(round(e.f("domain_age.age_years") * 365), 1))}
    if e.f("domain_age.known") >= 1 and e.has("domain_age.term_years") and e.f("domain_age.term_years") <= 1.1:
        fired["short_registration"] = {}
    _policies(e, fired)


def _policies(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Shipping and return terms of a dropshipper, and a US shop whose only address is a mailbox."""
    if e.f("shop_policies.returns_abroad") >= 1 and (abroad := e.first("returns_abroad")):
        fired["returns_abroad"] = {"place": abroad.evidence.removeprefix("Returns go to ").rstrip(".")[:60]}
    if e.f("shop_policies.return_address_withheld") >= 1:
        fired["return_address_withheld"] = {}
    if e.f("shop_policies.restocking_fee_high") >= 1 and (fee := e.first("restocking_fee")):
        fired["restocking_fee"] = {"pct": (_NUMBERS_RE.findall(fee.evidence) or ["?"])[0] + "%"}
    if e.f("shop_policies.origin_doubt") >= 1:
        fired["origin_doubt"] = {}
    if e.f("shop_policies.long_delivery") >= 1 and (slow := e.first("long_delivery")):
        fired["long_delivery"] = {"days": (_NUMBERS_RE.findall(slow.evidence) or ["?"])[0]}
    if e.f("us_identity.mailbox_only") >= 1:
        fired["mailbox_address"] = {}


def _manipulation(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Text aimed at AI tools, hidden or visible."""
    if e.f("injection.instruction_hidden") >= 1:
        fired["hidden_ai_text"] = {}
    if e.f("injection.instruction_visible") >= 1 or e.f("content.addresses_ai") >= 1:
        fired["ai_text"] = {}


def _positive_params(e: _Evidence) -> dict[str, dict[str, str]]:
    fired: dict[str, dict[str, str]] = {}
    generic = e.f("content.generic_brand") >= 0.4 or e.first("brand_name") is not None
    if e.f("content.brand_recognized") >= 1 and not generic and e.facts.brand:
        fired["known_brand"] = {"brand": _quote(e.facts.brand)}
    # A white-label seller is "the brand" too, so that only counts as good news for a real brand.
    sold_by_brand = e.marketplace and e.facts.brand and e.facts.seller and e.has("listing.seller_not_brand")
    if sold_by_brand and not generic and e.f("listing.seller_not_brand") < 1:
        fired["sold_by_brand"] = {}
    if e.f("eu_registry.vat_valid") >= 1:
        fired["vat_registered"] = {}
    company = e.first("business_entity")
    if e.storefront and e.f("us_identity.us_address") >= 1 and company:
        fired["us_business"] = {"company": _quote(company.evidence.removeprefix("Company: "))}
    if e.storefront and e.f("domain_age.age_years") >= 3:
        fired["established_domain"] = {"years": str(int(e.f("domain_age.age_years")))}
    if e.storefront and e.f("eu_registry.withdrawal_right") >= 1 and e.f("eu_registry.imprint") >= 1:
        fired["trust_info_present"] = {}
    natural = (
        e.f("reviews.has_reviews") >= 1
        and e.has("reviews.share_five")
        and e.f("reviews.j_shaped") < 1
        and e.f("reviews.duplicate_share") < 0.05
        and e.f("reviews.off_topic_share") < 0.1
        and (e.facts.rating_count or 0) >= 20
        and not e.first("review_velocity")
    )
    if natural:
        fired["natural_reviews"] = {}
    return fired


def _reason(key: str, params: dict[str, str]) -> Reason:
    copy = catalogue()["reasons"][key]
    thumbnails = params.get("thumbnails", "").split()
    return Reason(
        type=key,
        title=copy["title"],
        detail=copy["detail"].format(**params),
        severity=REASON_SEVERITY[key],
        thumbnails=thumbnails,
    )


def _positive(key: str, params: dict[str, str]) -> Positive:
    copy = catalogue()["positives"][key]
    return Positive(type=key, title=copy["title"], detail=copy["detail"].format(**params))


def build(
    score: int,
    *,
    features: dict[str, float],
    findings: list[Finding],
    facts: PageFacts,
    page_type: str,
    headline: str | None = None,
    market: str = "eu",
    confidence: float | None = None,
    concrete_only: bool = False,
) -> Verdict:
    """
    Build the shopper verdict for an analysed page.

    Reliable pages lead with what looks good and show at most one note; the other bands show up to three reasons,
    strongest first, and at most two positives. When the score is low but no single reason fired, an honest
    "several small signals" reason stands in.

    A shopper is only told "careful" (or worse) when there is something concrete to show for it, or the scorer is
    sure: when the only reasons are things missing from the page (:data:`ABSENCE_REASONS`, or none at all) and the
    scorer's confidence is below ``verdict_absence_only_min_confidence``, the band is "concerns" instead. With
    ``concrete_only`` (the quick tier, which reads too little to be sure) that holds however sure the scorer is.

    :param score: The 0–100 reliability score.
    :param features: The skill features from the graph state.
    :param findings: The skill findings.
    :param facts: The page facts.
    :param page_type: The classified page type.
    :param headline: The headline sentence; the catalogue's template for the band when not given.
    :param market: The shop's market (:func:`unfret.market.shop_market`), for reasons that depend on its law.
    :param confidence: The scorer's confidence (:class:`unfret.scoring.Assessment`); ``None`` without a scorer.
    :param concrete_only: Never warn without a concrete reason, whatever the scorer's confidence.
    :return: The verdict.
    """
    by_type: dict[str, list[Finding]] = {}
    for finding in findings:
        by_type.setdefault(finding.type, []).append(finding)
    evidence = _Evidence(features, by_type, facts, page_type, market)
    band = band_for(score)

    fired: dict[str, dict[str, str]] = {}
    for rules in (_pressure, _reviews, _product, _supply, _network, _shop, _manipulation):
        rules(evidence, fired)
    for group in _GROUPS:
        for key in [k for k in group if k in fired][1:]:
            del fired[key]
    unsure = concrete_only or (confidence is not None and confidence < settings.verdict_absence_only_min_confidence)
    if band in {"careful", "junk_or_scam"} and unsure and set(fired) <= set(ABSENCE_REASONS):
        band = "concerns"  # nothing concrete to point at, and the scorer isn't sure: don't warn on absences alone
    order = list(REASON_SEVERITY)
    keys = sorted(fired, key=lambda k: (_SEVERITY_RANK[REASON_SEVERITY[k]], order.index(k)))
    reasons = [_reason(k, fired[k]) for k in keys]
    positives = [_positive(k, v) for k, v in _positive_params(evidence).items()]

    if band == "reliable":
        reasons = reasons[:1]
        positives = positives[:3] or [_positive("nothing_found", {})]
    else:
        reasons = reasons[:3] or [_reason("overall", {})]
        positives = positives[: 2 if band == "concerns" else 1]

    copy = catalogue()
    return Verdict(
        band=band,
        label=copy["labels"][band],
        headline=headline or template_headline(band, keys[:2], reasons, copy),
        reasons=reasons,
        positives=positives,
    )
