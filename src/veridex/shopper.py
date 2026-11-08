"""
The shopper-facing verdict: a band in words, at most three plain-language reasons and the things that look good.

The scorer's ``assessment`` (risk, confidence, weighted feature flags) is for the details view and for agents; this
module turns the same evidence into what a shopper reads. Every reason comes from a curated copy catalogue in the
shopper's language (``copy/<language>.json``), fires only when a skill measured something concrete, and never shows
a feature name or a raw value. Structural facts (is it a marketplace, is it on Shopify, does it have reviews)
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

from veridex.config import settings
from veridex.facts.schema import PageFacts
from veridex.graph.state import Finding

Band = Literal["reliable", "concerns", "careful", "junk_or_scam"]
Severity = Literal["high", "medium", "low"]
LANGUAGES = ("en", "nl")
DEFAULT_LANGUAGE = "en"
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
    "seller_not_brand": "low",
    "ships_from_far": "low",
    "dropshipping_tools": "medium",
    "supplier_links": "medium",
    "supplier_photos": "high",
    "photo_predates": "medium",
    "missing_trust_info": "medium",
    "no_withdrawal": "medium",
    "no_imprint": "medium",
    "invalid_vat": "high",
    "new_domain": "high",
    "short_registration": "low",
    "hidden_ai_text": "high",
    "ai_text": "medium",
    "overall": "low",
}
POSITIVE_KEYS = (
    "known_brand",
    "sold_by_brand",
    "vat_registered",
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
        "reverse_image.log_matches",
        "reverse_image.image_age_years",
    }
)
# Reasons that tell the same story; only the first (strongest) of each group is shown.
_GROUPS = (
    ("timer_resets", "fake_urgency_app", "countdown"),
    ("generic_brand", "generic_product"),
    ("no_withdrawal", "no_imprint", "missing_trust_info"),
    ("hidden_ai_text", "ai_text"),
    ("supplier_photos", "supplier_links"),
)
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_QUOTED_RE = re.compile(r'"([^"]{1,150})"')


class Reason(BaseModel):
    """Something that lowered the score, for a shopper."""

    type: str
    title: str
    detail: str
    severity: Severity


class Positive(BaseModel):
    """Something that looks good, for a shopper."""

    type: str
    title: str
    detail: str


class Verdict(BaseModel):
    """The shopper-facing result of an analysis."""

    band: Band
    label: str = Field(description="The band in words, in the requested language.")
    headline: str = Field(description="One plain sentence explaining the verdict.")
    reasons: list[Reason] = Field(default_factory=list, max_length=3)
    positives: list[Positive] = Field(default_factory=list, max_length=3)
    language: str = DEFAULT_LANGUAGE


@cache
def copy_for(language: str) -> dict[str, Any]:
    """
    The copy catalogue for a language (``copy/<language>.json``), English for unsupported languages.

    :param language: A language code.
    :return: Labels, headline templates, reason and positive copy, and small phrases.
    """
    lang = language if language in LANGUAGES else DEFAULT_LANGUAGE
    data: dict[str, Any] = json.loads((COPY_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    return data


def pick_language(accept_language: str | None) -> str:
    """
    Choose the response language from an ``Accept-Language`` header (supported: :data:`LANGUAGES`).

    :param accept_language: The header value, e.g. ``"nl-NL,nl;q=0.9,en;q=0.8"``.
    :return: A supported language code; English when nothing matches.
    """
    ranked: list[tuple[float, int, str]] = []
    for i, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if q > 0:  # q=0 (or an unreadable q) means "not acceptable"
            ranked.append((-q, i, tag.strip().lower().split("-")[0]))
    for _, _, lang in sorted(ranked):
        if lang in LANGUAGES:
            return lang
    return DEFAULT_LANGUAGE


def feature_label(name: str, language: str = DEFAULT_LANGUAGE) -> str:
    """
    The human label of a skill feature (``content.generic_brand`` → "Unknown brand").

    :param name: The prefixed feature name.
    :param language: The shopper's language.
    :return: The label; a neutral fallback for a feature the catalogue doesn't know yet.
    """
    labels: dict[str, str] = copy_for(language)["features"]
    return labels.get(name) or copy_for(language)["labels"]["concerns"]


def label_flags(flags: list[dict[str, Any]], language: str = DEFAULT_LANGUAGE) -> list[dict[str, Any]]:
    """
    Add ``label``, ``kind`` and ``severity`` to the scorer's flags, so no client shows a feature name.

    ``kind`` is ``context`` for features that describe the page (see :data:`CONTEXT_FEATURES`), ``signal``
    otherwise; ``severity`` follows the flag's weight (its log-odds contribution).

    :param flags: The assessment's flags (``type``, ``evidence``, ``weight``).
    :param language: The shopper's language.
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
                "label": feature_label(str(flag.get("type")), language),
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
    language: str

    def f(self, name: str) -> float:
        return float(self.features.get(name, 0.0))

    def has(self, name: str) -> bool:
        return name in self.features

    def first(self, finding_type: str) -> Finding | None:
        found = self.findings.get(finding_type)
        return found[0] if found else None

    def phrase(self, key: str) -> str:
        return str(copy_for(self.language)[key])

    def age(self, days: int) -> str:
        units = copy_for(self.language)["age_units"]
        for limit, size, unit in ((14, 1, "day"), (60, 7, "week"), (730, 30, "month")):
            if days < limit:
                n = max(1, round(days / size))
                return f"{n} {units[unit][0 if n == 1 else 1]}"
        n = round(days / 365)
        return f"{n} {units['year'][0 if n == 1 else 1]}"

    def number(self, n: int) -> str:
        text = f"{n:,}"
        return text.replace(",", ".") if self.language == "nl" else text

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
    if e.f("content.generic_brand") >= settings.verdict_llm_signal_min or e.first("brand_name"):
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
    """Traces of the supplier behind the shop: dropshipping tools, wholesale links and photos."""
    if e.f("html_source_signals.dropship_app") >= 1:
        fired["dropshipping_tools"] = {}
    if e.f("html_source_signals.supplier_trace") >= 1:
        fired["supplier_links"] = {}
    if e.f("reverse_image.supplier_hits") >= 1:
        photo = e.first("supplier_photo")
        sites = photo.evidence.rsplit(" on ", 1)[-1].rstrip(".") if photo else e.phrase("wholesale_sites")
        fired["supplier_photos"] = {"sites": sites[:120]}
    if e.f("reverse_image.photo_predates_listing") >= 1:
        fired["photo_predates"] = {}


def _shop(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """A web shop's own identity: return policy, company details, VAT number, domain age (storefronts only)."""
    if not e.storefront:
        return
    if e.has("eu_registry.withdrawal_right") and e.f("eu_registry.withdrawal_right") < 1:
        fired["no_withdrawal"] = {}
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
    sold_by_brand = e.marketplace and e.facts.brand and e.facts.seller and e.has("listing.seller_not_brand")
    if sold_by_brand and e.f("listing.seller_not_brand") < 1:
        fired["sold_by_brand"] = {}
    if e.f("eu_registry.vat_valid") >= 1:
        fired["vat_registered"] = {}
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


def _reason(key: str, params: dict[str, str], language: str) -> Reason:
    copy = copy_for(language)["reasons"][key]
    return Reason(type=key, title=copy["title"], detail=copy["detail"].format(**params), severity=REASON_SEVERITY[key])


def _positive(key: str, params: dict[str, str], language: str) -> Positive:
    copy = copy_for(language)["positives"][key]
    return Positive(type=key, title=copy["title"], detail=copy["detail"].format(**params))


def build(
    score: int,
    *,
    features: dict[str, float],
    findings: list[Finding],
    facts: PageFacts,
    page_type: str,
    language: str = DEFAULT_LANGUAGE,
    headline: str | None = None,
) -> Verdict:
    """
    Build the shopper verdict for an analysed page.

    Reliable pages lead with what looks good and show at most one note; the other bands show up to three reasons,
    strongest first, and at most two positives. When the score is low but no single reason fired, an honest
    "several small signals" reason stands in.

    :param score: The 0–100 reliability score.
    :param features: The skill features from the graph state.
    :param findings: The skill findings.
    :param facts: The page facts.
    :param page_type: The classified page type.
    :param language: The shopper's language (see :func:`pick_language`).
    :param headline: The headline sentence; the catalogue's template for the band when not given.
    :return: The verdict.
    """
    language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    by_type: dict[str, list[Finding]] = {}
    for finding in findings:
        by_type.setdefault(finding.type, []).append(finding)
    evidence = _Evidence(features, by_type, facts, page_type, language)
    band = band_for(score)

    fired: dict[str, dict[str, str]] = {}
    for rules in (_pressure, _reviews, _product, _supply, _shop, _manipulation):
        rules(evidence, fired)
    for group in _GROUPS:
        for key in [k for k in group if k in fired][1:]:
            del fired[key]
    order = list(REASON_SEVERITY)
    keys = sorted(fired, key=lambda k: (_SEVERITY_RANK[REASON_SEVERITY[k]], order.index(k)))
    reasons = [_reason(k, fired[k], language) for k in keys]
    positives = [_positive(k, v, language) for k, v in _positive_params(evidence).items()]

    if band == "reliable":
        reasons = reasons[:1]
        positives = positives[:3] or [_positive("nothing_found", {}, language)]
    else:
        reasons = reasons[:3] or [_reason("overall", {}, language)]
        positives = positives[: 2 if band == "concerns" else 1]

    copy = copy_for(language)
    return Verdict(
        band=band,
        label=copy["labels"][band],
        headline=headline or copy["headlines"][band],
        reasons=reasons,
        positives=positives,
        language=language,
    )
