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
    "brand_photo_reused": "high",
    "rebranded_photo": "medium",
    "review_velocity": "high",
    "review_shape": "medium",
    "duplicate_reviews": "medium",
    "off_topic_reviews": "medium",
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
    "seller_lookalike": "medium",
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
    "safety_gate_alert": "high",
    "irreversible_payment_only": "high",
    "founded_contradicted": "high",
    "far_below_market": "high",
    "origin_contradicted": "medium",
    "copied_reviews_elsewhere": "medium",
    "brand_no_presence": "medium",
    "store_without_address": "low",
    "shared_address": "low",
    "hidden_ai_text": "high",
    "ai_text": "medium",
    "overall": "low",
    # Peer-to-peer ads (unfret.shopper_p2p), in their own tie-break order.
    "p2p_advance_payment": "high",
    "p2p_untraceable_payment": "high",
    "p2p_protection_refused": "high",
    "p2p_seller_abroad": "high",
    "p2p_photo_in_other_ads": "high",
    "p2p_price_far_below": "medium",
    "p2p_bank_transfer": "medium",
    "p2p_shipping_only": "medium",
    "p2p_new_account": "medium",
    "p2p_listing_burst": "medium",
    "p2p_duplicate_text": "medium",
    "p2p_shared_contact": "medium",
    "p2p_contact_outside": "medium",
    "p2p_llm_off_platform": "medium",
    "p2p_low_rating": "medium",
    "p2p_photo_on_shops": "low",
    "p2p_no_ratings": "low",
    "p2p_slow_responses": "low",
    "p2p_little_known": "low",
}
POSITIVE_KEYS = (
    "known_brand",
    "sold_by_brand",
    "vat_registered",
    "us_business",
    "established_domain",
    "trust_info_present",
    "natural_reviews",
    "protected_payment",
    "nothing_found",
    "p2p_established",
    "p2p_well_rated",
    "p2p_verified",
    "p2p_protection",
    "p2p_pickup",
    "p2p_normal_price",
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
        "eu_registry.legal_pages_checked",
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
# Not enough on its own for the quick tier: a countdown that doesn't restart is an ordinary promotion (Black Friday
# banners) as often as pressure; one that restarts on reload is "timer_resets", which is.
_QUICK_NOT_ENOUGH_ALONE = frozenset((*ABSENCE_REASONS, "countdown"))
# Reasons that tell the same story; only the first (strongest) of each group is shown.
_GROUPS = (
    ("timer_resets", "fake_urgency_app", "countdown"),
    ("brand_no_presence", "generic_brand", "generic_product"),
    ("seller_lookalike", "seller_not_brand"),
    ("brand_photo_reused", "rebranded_photo", "photo_reused"),
    ABSENCE_REASONS,
    ("hidden_ai_text", "ai_text"),
    ("was_never_charged", "implausible_discount", "big_discount"),
    ("returns_abroad", "return_address_withheld"),
)
_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# A fee on top of the price ("service fee", "Bearbeitungsgebühr"): "costs revealed later". Shipping costs worked out
# at checkout ("shipping calculated at checkout", "verzendkosten", "zzgl. Versand") are how almost every shop works,
# and said far more often by legitimate shops than by scams, so they aren't a reason on their own.
_EXTRA_FEE_RE = re.compile(
    r"(?:service|handling|processing|booking|convenience|admin(?:istration)?) fee|servicekosten|administratiekosten"
    r"|bearbeitungsgebühr|servicegebühr|frais de (?:service|dossier|traitement)|gastos de gestión|spese di gestione",
    re.IGNORECASE,
)
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
    source: str | None = Field(
        default=None, description='Where the evidence comes from, in words: "Domain registry (RDAP)", "The page", …'
    )


class Positive(BaseModel):
    """Something that looks good, for a shopper."""

    type: str
    title: str
    detail: str
    source: str | None = Field(default=None, description="Where the evidence comes from, in words.")


CheckStatus = Literal["checked", "concern", "not_checked"]


class CheckItem(BaseModel):
    """One line of the result card's "What we checked": an area, and whether it was checked."""

    key: str = Field(
        description="who, product, reviews, prices, tricks (shops); seller, price, photos, payment, text "
        "(ads); company, location, reviews, terms, photos (service providers)."
    )
    label: str = Field(description='The area in words: "Who\'s behind the shop", "Reviews", …')
    status: CheckStatus = Field(
        description="checked (nothing to note), concern (a reason above is about it) or not_checked (see note)."
    )
    note: str = Field(default="", description="Why it wasn't checked; empty otherwise.")
    checks: int | None = Field(
        default=None,
        ge=0,
        description="How many of the verdict's checks_run looked at this area (a check serving two areas counts in "
        "the first); the areas' counts add up to checks_run.",
    )


class NextStep(BaseModel):
    """The one thing the card suggests doing next."""

    type: str = Field(description="What the step is about (the reason it follows from, or the band).")
    text: str


class Incomplete(BaseModel):
    """Part of the check didn't finish (a key step failed or ran out of time): the verdict rests on less evidence."""

    steps: list[str] = Field(
        description='The steps that didn\'t finish, by name ("content": the AI read of the page).'
    )
    text: str = Field(description="One line for the shopper: what didn't finish, what that means, what it cost.")


class SameProduct(BaseModel):
    """The same product photo on another shop or listing (Unfret's image index): shown first, with both photos."""

    title: str = Field(description='"Looks like the same product, sold elsewhere".')
    detail: str = Field(description="Where, and for how much when Unfret has seen its price.")
    where: str = Field(description='The other shop\'s domain, or "Amazon" for an Amazon listing.')
    domain: str | None = Field(default=None, description="The other shop's registrable domain, when it is a shop.")
    others: int = Field(default=0, ge=0, description="How many more shops show the same photo.")
    price: float | None = Field(default=None, description="The other listing's price when Unfret has seen it.")
    currency: str | None = Field(default=None, description="ISO 4217 code of price.")
    price_text: str | None = Field(default=None, description='The price in words: "$12.99".')
    price_seen_on: str | None = Field(default=None, description="The day Unfret saw that price (YYYY-MM-DD).")
    brands: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="Other brand names the same photo is sold under there (names only), when Unfret knows them.",
    )
    image: str = Field(description="Thumbnail URL of the other listing's photo (this server's image proxy).")
    own_image: str | None = Field(
        default=None, description="Thumbnail URL of this page's own photo, when the index keeps it."
    )


DeepCheckReason = Literal["on_the_fence", "unknown_seller", "serious_concerns", "known_seller", "nothing_to_add"]


class DeepCheckAdd(BaseModel):
    """One thing a deep check would look at on this page that the quick check left out."""

    key: str = Field(
        description="shop_age, photo_elsewhere, reviews, brand, price_history, vat_register (shops and marketplace "
        "listings); seller_profile, ad_photos, ad_price (ads); provider_profile, service_terms (service platforms)."
    )
    area: str = Field(description="The checklist area it belongs to (a CheckItem key).")
    label: str = Field(description='In words, to follow "A deep check also looks at": "how old this shop is".')


class DeepCheck(BaseModel):
    """
    On a quick result only: whether a deep check of the same page is worth suggesting, and what it would add.

    Worked out from the quick result alone; it never changes the quick verdict.
    """

    suggested: bool
    reason: DeepCheckReason = Field(
        description="Why (not): on_the_fence (Worth a look, Think twice), unknown_seller (Looks fine, but the quick "
        "check can't see who is behind the page); serious_concerns (the advice is already to buy elsewhere), "
        "known_seller (a big marketplace's own or the brand's listing), nothing_to_add."
    )
    quiet: bool = Field(default=False, description="Show it quietly (Looks fine): as a note, not a call to action.")
    adds: list[DeepCheckAdd] = Field(default_factory=list, max_length=3, description="What a deep check adds here.")
    text: str = Field(default="", description='"A deep check also looks at …" (empty when not suggested).')
    cost: str | None = Field(
        default=None,
        description='What a deep check would cost this caller, in words: "Uses 1 of your 3 free deep checks.", "Uses '
        '1 check (you have 12).", "Deep checks come in packs, from $6.00 for 25."; on /v1/check paid with x402, the '
        "deep tier's price.",
    )
    action: Literal["run_deep", "get_checks"] | None = Field(
        default=None, description="run_deep: the caller can run it now (tier deep); get_checks: it needs checks first."
    )
    action_label: str | None = Field(default=None, description='"Run a deep check" or "Get checks".')


class Verdict(BaseModel):
    """The shopper-facing result of an analysis."""

    band: Band
    label: str = Field(description="The band in words.")
    headline: str = Field(description="One plain sentence explaining the verdict.")
    reasons: list[Reason] = Field(default_factory=list, max_length=3)
    positives: list[Positive] = Field(default_factory=list, max_length=3)
    language: str = Field(default=LANGUAGE, description="Always en (kept for clients that read it).")
    checklist: list[CheckItem] = Field(
        default_factory=list, description="What was checked, area by area (the result card's checklist)."
    )
    checks_run: int | None = Field(
        default=None, ge=0, description="How many separate checks ran on this page and had something to check."
    )
    next_step: NextStep | None = Field(default=None, description="One suggested next step.")
    same_product: SameProduct | None = Field(
        default=None, description="A likely same product on another shop, shown first (with both photos)."
    )
    deep_check: DeepCheck | None = Field(
        default=None,
        description="Quick results only: whether a deep check of this page is worth it and what it would add.",
    )
    incomplete: Incomplete | None = Field(
        default=None,
        description="Part of the check didn't finish, so the verdict may be milder than it should be (the result's "
        "assessment is degraded).",
    )


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
    The headline without an LLM: the verdict's (up to two) strongest concerns, else the band's sentence.

    Every client shows the verdict's label right above the headline, so the headline never repeats it: "Fake
    countdown and unknown brand.", not "Serious concerns: fake countdown and unknown brand.". A reliable page names
    its one note ("One thing to note: …") rather than saying nothing stood out.

    :param band: The verdict band.
    :param fired: The keys of the reasons that fired, strongest first.
    :param reasons: The verdict's reasons (same order).
    :param copy: The copy catalogue.
    :return: The headline.
    """
    sentence: str = copy["headlines"][band]
    if band == "reliable" and fired and reasons and reasons[0].title:
        # A reliable page that still has a note: "nothing worrying" would contradict it.
        reason = reasons[0].title[0].lower() + reasons[0].title[1:]
        return str(copy["headline_reliable_note"].format(label=copy["labels"][band], reason=reason))
    if band == "reliable" or not fired:
        return sentence
    titles = [r.title[0].lower() + r.title[1:] for r in reasons[: len(fired)] if r.title]
    for count in (2, 1):
        if len(titles) >= count:
            joined = f" {copy['and']} ".join(titles[:count])
            joined = joined[0].upper() + joined[1:]
            headline = str(copy["headline_reasons"].format(label=copy["labels"][band], reasons=joined))
            if len(headline.split()) <= HEADLINE_MAX_WORDS:
                return headline
    return sentence


def without_label(text: str, label: str) -> str:
    """
    A headline without the verdict's label in front ("Serious concerns: fake countdown." → "Fake countdown.").

    Clients show the label right above the headline; repeating it reads as a stutter.

    :param text: The headline.
    :param label: The verdict's label.
    :return: The headline, its first letter capitalised, or ``text`` unchanged if it doesn't start with the label.
    """
    match = re.match(rf"\s*{re.escape(label)}\s*(?:[:,.;!—–-]\s*(?:but\s+)?)?", text, flags=re.IGNORECASE)
    rest = text[match.end() :] if match else ""
    if not match or len(rest.split()) < 2:
        return text
    return rest[0].upper() + rest[1:]


def fit_headline(text: str, label: str | None = None) -> str | None:
    """
    A written headline, if it is one sentence of at most :data:`HEADLINE_MAX_WORDS` words.

    :param text: The LLM's sentence.
    :param label: The verdict's label, taken off the front if the sentence starts with it (:func:`without_label`).
    :return: The sentence, stripped, or ``None`` when it is empty or too long (the template is used instead).
    """
    text = " ".join(text.split())
    if label:
        text = without_label(text, label)
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
    if (
        e.storefront
        and e.f("dark_patterns.fee_mentions") >= 1
        and any(_EXTRA_FEE_RE.search(f.evidence) for f in e.findings.get("fee", []))
    ):
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
    # Reviews pooled from other products are a marketplace trick (variations merged, listings hijacked), and the
    # similarity measure is calibrated on marketplace reviews. A shop's own reviews of its product often talk about
    # delivery, size and service instead, so there it takes a majority of clear outliers (see the reviews skill).
    off_topic = e.f("reviews.off_topic_share")
    if off_topic >= (0.3 if e.marketplace else 0.5):
        fired["off_topic_reviews"] = {"pct": _pct(off_topic)}
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
    # A name that merely looks generated (a retailer's own label in capitals) is said on a shop's own page only
    # with other signs of a resold supplier product: the quick tier has no content read to know the brand.
    looks_generated = e.first("brand_name") is not None and (e.marketplace or _resale_context(e, brand_name=False))
    if (llm_says_generic or looks_generated) and not _brand_exists(e):
        fired["generic_brand"] = {"brand": _quote(e.facts.brand) if e.facts.brand else e.phrase("this_brand")}
    if e.f("content.generic_product") >= 0.7:
        fired["generic_product"] = {}
    claim = e.first("implausible_claim")
    if e.f("content.implausible_claims") >= 0.6 and claim:
        fired["implausible_claims"] = {"claim": _quote(claim.evidence)}
    if e.marketplace and e.f("listing.seller_not_brand") >= 1 and e.facts.seller:
        fired["seller_not_brand"] = {"seller": _quote(e.facts.seller)}
        # A seller named almost like a brand known elsewhere ("Nikee" for Nike): a stronger sign than another seller.
        if e.first("seller_lookalike") and _brand_known_elsewhere(e) and e.facts.brand:
            fired["seller_lookalike"] = {"seller": _quote(e.facts.seller), "brand": _quote(e.facts.brand)}
    if e.f("listing.ships_from_china") >= 1:
        fired["ships_from_far"] = {"place": e.facts.ships_from[:60] or "China"}


# A brand whose own domain is at least this old has existed for years: not a name made up for one listing.
BRAND_DOMAIN_MIN_YEARS = 5


def _brand_known_elsewhere(e: _Evidence) -> bool:
    """Outside evidence (not the LLM's say-so) that the brand exists: Wikidata, or an own domain for years."""
    return e.f("brand_presence.wikidata") >= 1 or e.f("brand_presence.own_domain_age_years") >= BRAND_DOMAIN_MIN_YEARS


def _brand_exists(e: _Evidence) -> bool:
    """
    Evidence that the brand is real, which outweighs a name that merely looks generated ("HKLIVING").

    The content read knows the brand, Wikidata has it as a business or brand, or its own ``.com``/``.nl`` domain has
    been registered for :data:`BRAND_DOMAIN_MIN_YEARS` years or more (brand_presence).
    """
    return (
        e.f("content.brand_recognized") >= 1
        or e.f("brand_presence.wikidata") >= 1
        or e.f("brand_presence.own_domain_age_years") >= BRAND_DOMAIN_MIN_YEARS
    )


def _supply(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Traces of the supplier behind the shop: dropshipping tools and wholesale links."""
    if e.f("html_source_signals.dropship_app") >= 1:
        fired["dropshipping_tools"] = {}
    if e.f("html_source_signals.supplier_trace") >= 1:
        fired["supplier_links"] = {}
    # The same photo on other shops is normal for brands and their resellers: a warning only for an unknown brand
    # (neither the content read nor Wikidata knows it) with other signs of a resold supplier product.
    photo = e.first("photo_on_other_shops")
    unknown = e.f("content.brand_recognized") < 1 and e.f("brand_presence.wikidata") < 1
    if photo and e.f("reverse_image.other_shops") >= 1 and unknown and _resale_context(e):
        count = int(e.f("reverse_image.other_shops"))
        fired["photo_reused"] = {
            "count": e.number(count),
            "shops": e.phrase("shop_one" if count == 1 else "shop_many"),
            "names": _listed(photo.evidence),
            "thumbnails": " ".join(photo.thumbnails),
        }


def _photo_brands(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """
    The same photo under other brand names (white-label goods), or a known brand's photo under another brand's name.

    Unlike a reused photo, no other sign is needed: two brand names on one photo is the sign. Never for a brand that
    exists beyond the page (:func:`_brand_exists`) or on the brand's own site, whose photo its resellers show under
    their own labels.
    """
    if _brand_exists(e) or e.f("brand_presence.shop_is_brand") >= 1:
        return
    copied = e.first("brand_photo_reused")
    if copied and e.f("reverse_image.brand_photo_reused") >= 1 and (m := _BRANDS_RE.search(copied.evidence)):
        fired["brand_photo_reused"] = {
            "catalogue_brand": _quote(m.group("known")),
            "brand": _quote(m.group("page")),
            "thumbnails": " ".join(copied.thumbnails),
        }
    relabelled = e.first("photo_other_brands")
    if relabelled and (count := int(e.f("reverse_image.other_brands"))) >= 1:
        many = e.phrase("other_brand_many").format(count=e.number(count)) if count > 1 else e.phrase("other_brand_one")
        fired["rebranded_photo"] = {
            "many": many,
            "names": _listed(relabelled.evidence),
            "thumbnails": " ".join(relabelled.thumbnails),
        }


_BRANDS_RE = re.compile(
    r'of the brand "(?P<known>[^"]{1,120})", a known brand; this page names the brand "(?P<page>[^"]{1,120})"'
)


def _resale_context(e: _Evidence, *, brand_name: bool = True) -> bool:
    """Signs that a product is a resold supplier item rather than a brand's own (``brand_name``: its name counts)."""
    return (
        e.f("domain_age.under_6_months") >= 1
        or e.f("content.generic_product") >= 0.5
        or e.f("content.title_word_soup") >= 0.5
        or (brand_name and e.f("listing.generated_brand") >= 0.5)
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
    if e.has("eu_registry.imprint") and e.f("eu_registry.imprint") < 1 and _imprint_really_missing(e):
        checked = e.f("eu_registry.legal_pages_checked") >= 1
        fired["no_imprint"] = {"where": e.phrase("imprint_where_legal_pages" if checked else "imprint_where_page")}
    # The LLM's "little shop information" reads the page text without its footer; when the page's legal information
    # shows both a return policy and company details ("Shop details present"), that impression is wrong.
    # And a shop that has been around for a decade and shows its return policy is not short of shop information
    # when its legal pages couldn't be read this time (fonq.nl: imprint from its legal pages, which can time out).
    vouched_for = e.f("eu_registry.withdrawal_right") >= 1 and _long_established(e)
    if e.f("content.missing_trust_info") >= 0.6 and not _trust_info_present(e) and not vouched_for:
        fired["missing_trust_info"] = {}
    # Not in VIES, or a number that can't exist (eu_registry gives a made-up one no credit as a VAT number).
    if e.has("eu_registry.vat_valid") and e.f("eu_registry.vat_valid") < 1:
        fired["invalid_vat"] = {}
    if e.f("domain_age.under_6_months") >= 1:
        fired["new_domain"] = {"age": e.age(max(round(e.f("domain_age.age_years") * 365), 1))}
    # Registered for at most a year in all (registration to expiry), so the shop is itself less than a year old. A
    # term of 0 means the registry doesn't publish an expiry date (.nl doesn't); an established domain renewing year
    # by year is normal and never says this.
    term = e.f("domain_age.term_years")
    if e.f("domain_age.known") >= 1 and 0 < term <= 1.1 and e.f("domain_age.age_years") < ESTABLISHED_DOMAIN_YEARS:
        fired["short_registration"] = {}
    _policies(e, fired)


# A web shop whose address has existed this long is "established" (the positive), and never "registered for a year".
ESTABLISHED_DOMAIN_YEARS = 3


def _trust_info_present(e: _Evidence) -> bool:
    """The page's legal information shows a return policy and company details (eu_registry)."""
    return e.f("eu_registry.withdrawal_right") >= 1 and e.f("eu_registry.imprint") >= 1


def _vouched_shop(e: _Evidence) -> bool:
    """A web shop with a track record: its address has existed for years, or it shows its return policy and company."""
    established = e.f("domain_age.age_years") >= ESTABLISHED_DOMAIN_YEARS
    return e.storefront and (established or _trust_info_present(e))


# A shop address older than this is no fly-by-night shop: its company is not in doubt for want of an imprint.
LONG_ESTABLISHED_YEARS = 10


def _long_established(e: _Evidence) -> bool:
    """The shop's domain has existed for a decade, or it is the brand's own site and the brand is on Wikidata."""
    own_site = e.f("brand_presence.shop_is_brand") >= 1 and e.f("brand_presence.wikidata") >= 1
    return e.f("domain_age.age_years") >= LONG_ESTABLISHED_YEARS or own_site


def _imprint_really_missing(e: _Evidence) -> bool:
    """
    Whether "No company details" is a fair thing to say, beyond "not on this page".

    Established shops keep their company details on a separate contact or legal page, not on every product page. So
    a page that links to such a page counts as missing them only when eu_registry read those pages too and found
    nothing there either. A VAT number that the EU register knows identifies the company anyway, and so does a
    long-established shop (see :func:`_long_established`) whose legal pages just don't name it in their HTML (ikea.nl
    renders them with scripts).
    """
    if e.f("eu_registry.vat_valid") >= 1 or _long_established(e):
        return False
    links_to_legal = bool({"contact", "terms", "imprint"} & set(e.facts.policy_links))
    return not links_to_legal or e.f("eu_registry.legal_pages_checked") >= 1


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


_SAYS_RE = re.compile(r'^Says "(?P<claim>[^"]{1,150})"; (?P<fact>.*)$')


def _claims(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """What a web shop says about itself against the facts: founding year, origin, a physical store."""
    if not e.storefront:
        return
    founded = e.first("founded_contradicted")
    if e.f("domain_age.founded_contradicted") >= 1 and founded and (m := _SAYS_RE.match(founded.evidence)):
        days = int((_NUMBERS_RE.findall(m.group("fact")) or ["1"])[0])
        fired["founded_contradicted"] = {"claim": _quote(m.group("claim")), "age": e.age(max(days, 1))}
    origin = e.first("origin_contradicted")
    if e.f("claims.origin_contradicted") >= 1 and origin and (m := _SAYS_RE.match(origin.evidence)):
        place = m.group("fact").rpartition(" is in ")[2].rstrip(".")
        fired["origin_contradicted"] = {"claim": _quote(m.group("claim")), "place": place[:80]}
    store = e.first("store_without_address")
    if e.f("claims.store_without_address") >= 1 and store and (m := _SAYS_RE.match(store.evidence)):
        fired["store_without_address"] = {"claim": _quote(m.group("claim"))}


def _payment_and_market(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Irreversible payment only, a price far below other shops', reviews copied from other shops, shared address."""
    only = e.first("irreversible_payment_only")
    if e.storefront and e.f("payment_methods.irreversible_only") >= 1 and only:
        methods = only.evidence.removeprefix("Payment methods mentioned: ").partition(";")[0]
        fired["irreversible_payment_only"] = {"methods": methods[:80]}
    if e.f("market_price.far_below") >= 1 and e.first("far_below_market"):
        count = int(e.f("market_price.other_shops"))
        fired["far_below_market"] = {
            "pct": _pct(e.f("market_price.below_market")),
            "count": e.number(count),
            "shops": e.phrase("shop_one" if count == 1 else "shop_many"),
        }
    copied, share = e.f("review_copies.copied"), e.f("review_copies.copied_share")
    unknown_brand = e.f("content.brand_recognized") < 1 and e.f("brand_presence.wikidata") < 1
    if e.storefront and copied >= 2 and share >= 0.2 and unknown_brand and _resale_context(e):
        count = int(e.f("review_copies.other_shops"))
        fired["copied_reviews_elsewhere"] = {
            "copied": e.number(int(copied)),
            "checked": e.number(int(e.f("review_copies.checked"))),
            "count": e.number(count),
            "shops": e.phrase("shop_one" if count == 1 else "shop_many"),
        }
    if e.storefront and (shared := int(e.f("shop_links.shared_address_shops"))) >= SHARED_ADDRESS_MIN:
        fired["shared_address"] = {"count": e.number(shared)}


# This many unrelated shops at one postal address is a virtual office or a mail-forwarding service.
SHARED_ADDRESS_MIN = 5


def _product_facts(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """A Safety Gate alert for the product, and a brand that exists nowhere but here."""
    alert = e.first("safety_gate_alert")
    if alert and (e.f("safety_gate.barcode_match") >= 1 or e.f("safety_gate.model_match") >= 1):
        fired["safety_gate_alert"] = {"alert": alert.evidence.removeprefix("Safety Gate alert ")[:400]}
    # Like "unknown brand": only on marketplace listings, and only with other signs of white-label goods.
    # The content read knowing the brand contradicts "no presence": then neither is said (_positive_params).
    known = e.f("content.brand_recognized") >= 1
    if e.marketplace and e.f("brand_presence.no_presence") >= 1 and not known and _resale_context(e):
        fired["brand_no_presence"] = {"brand": _quote(e.facts.brand) if e.facts.brand else e.phrase("this_brand")}


def _manipulation(e: _Evidence, fired: dict[str, dict[str, str]]) -> None:
    """Text aimed at AI tools, hidden or visible."""
    if e.f("injection.instruction_hidden") >= 1:
        fired["hidden_ai_text"] = {}
    # Only the quoted phrase the injection skill found ("ignore previous instructions", "dear AI"): the content read's
    # own yes/no on "addresses an AI" fires as often on honest shops (cookie banners, chat widgets) as on scams.
    if e.f("injection.instruction_visible") >= 1:
        fired["ai_text"] = {}


def _positive_params(e: _Evidence) -> dict[str, dict[str, str]]:
    fired: dict[str, dict[str, str]] = {}
    # Outside evidence that the brand exists (not the LLM's own say-so) outweighs a name that looks generated.
    outside = _brand_known_elsewhere(e)
    generic = (e.f("content.generic_brand") >= 0.4 or e.first("brand_name") is not None) and not outside
    known = e.f("content.brand_recognized") >= 1 or e.f("brand_presence.wikidata") >= 1
    # Lookups that found the brand nowhere contradict the content read's "known": a quiet result, not both claims
    # (brand_no_presence then isn't said either).
    if known and not generic and e.facts.brand and e.f("brand_presence.no_presence") < 1:
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
    if e.storefront and e.f("domain_age.age_years") >= ESTABLISHED_DOMAIN_YEARS:
        fired["established_domain"] = {"years": str(int(e.f("domain_age.age_years")))}
    if e.storefront and _trust_info_present(e):
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
    protected = e.first("protected_payment")
    if e.storefront and e.f("payment_methods.protected") >= 1 and e.f("payment_methods.irreversible_only") < 1:
        methods = protected.evidence.removeprefix("Payment methods mentioned: ").rstrip(".") if protected else ""
        fired["protected_payment"] = {"methods": methods[:80] or e.phrase("protected_methods")}
    return fired


def _with_reasons(
    band: Band, score: int, fired: set[str], confidence: float | None, *, concrete_only: bool, needs_reason: bool
) -> Band:
    """
    The band once the reasons are known: no warning on absences alone when unsure, no "Worth a look" at nothing.

    :param band: The band from the score.
    :param score: The 0–100 reliability score.
    :param fired: The reasons that fired.
    :param confidence: The scorer's confidence, or ``None``.
    :param concrete_only: The quick tier.
    :param needs_reason: "Worth a look" needs something to show (the scorer's say).
    :return: The band (see :func:`build`).
    """
    unsure = concrete_only or (confidence is not None and confidence < settings.verdict_absence_only_min_confidence)
    not_enough = _QUICK_NOT_ENOUGH_ALONE if concrete_only else frozenset(ABSENCE_REASONS)
    if band in {"careful", "junk_or_scam"} and unsure and fired <= not_enough:
        band = "concerns"  # nothing concrete to point at, and the scorer isn't sure: don't warn on absences alone
    if band == "concerns" and needs_reason and _nothing_to_show(score, fired, concrete_only=concrete_only):
        band = "reliable"  # "Worth a look" at what? Not without something to show the shopper
    return band


def _nothing_to_show(score: int, fired: set[str], *, concrete_only: bool) -> bool:
    """
    Whether a "Worth a look" page has nothing to show for it (see :func:`build`).

    :param score: The 0–100 reliability score.
    :param fired: The reasons that fired.
    :param concrete_only: The quick tier.
    :return: ``True`` when the page should read "Looks fine" instead.
    """
    if concrete_only:
        return score >= settings.verdict_careful_min and fired <= _QUICK_NOT_ENOUGH_ALONE
    return score >= settings.verdict_concerns_min and fired <= set(ABSENCE_REASONS)


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


def _evidence(
    features: dict[str, float], findings: list[Finding], facts: PageFacts, page_type: str, market: str
) -> _Evidence:
    by_type: dict[str, list[Finding]] = {}
    for finding in findings:
        by_type.setdefault(finding.type, []).append(finding)
    return _Evidence(features, by_type, facts, page_type, market)


def _fired(evidence: _Evidence) -> dict[str, dict[str, str]]:
    fired: dict[str, dict[str, str]] = {}
    rules_in_order = (
        _pressure,
        _reviews,
        _product,
        _supply,
        _photo_brands,
        _network,
        _shop,
        _claims,
        _payment_and_market,
        _product_facts,
        _manipulation,
    )
    for rules in rules_in_order:
        rules(evidence, fired)
    for group in _GROUPS:
        for key in [k for k in group if k in fired][1:]:
            del fired[key]
    return fired


def fired_reasons(
    *, features: dict[str, float], findings: list[Finding], facts: PageFacts, page_type: str, market: str = "eu"
) -> list[str]:
    """
    Every reason that fired on a product page, strongest first, before the verdict keeps at most three.

    The result card's checklist marks an area as a concern for any of them, not only the ones shown.

    :param features: The skill features.
    :param findings: The skill findings.
    :param facts: The page facts.
    :param page_type: The classified page type.
    :param market: The shop's market.
    :return: The reasons' keys.
    """
    fired = _fired(_evidence(features, findings, facts, page_type, market))
    order = list(REASON_SEVERITY)
    return sorted(fired, key=lambda k: (_SEVERITY_RANK[REASON_SEVERITY[k]], order.index(k)))


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
    needs_reason: bool = False,
) -> Verdict:
    """
    Build the shopper verdict for an analysed page.

    Reliable pages lead with what looks good and show at most one note; the other bands show up to three reasons,
    strongest first, and at most two positives. When the score is low but no single reason fired, an honest
    "several small signals" reason stands in.

    "Serious concerns" (``junk_or_scam``) only at high confidence: when the scorer set the score and its confidence
    is below ``verdict_serious_min_confidence`` (``verdict_serious_min_confidence_quick`` with ``concrete_only``, the
    quick tier's scorer), a junk_or_scam score is shown as "Think twice" (``careful``). The warning bands together are
    unchanged (the same pages are warned about); only the harshest word waits for a sure scorer.

    A shopper is only told "careful" (or worse) when there is something concrete to show for it, or the scorer is
    sure: when the only reasons are things missing from the page (:data:`ABSENCE_REASONS`, or none at all) and the
    scorer's confidence is below ``verdict_absence_only_min_confidence``, the band is "concerns" instead. With
    ``concrete_only`` (the quick tier, which reads too little to be sure) that holds however sure the scorer is.
    The other way round, a web shop showing a VAT number that can't exist (a ``vat_fake`` finding) is "careful" at
    best, however good the rest looks; so is a page selling a known brand's own photo under another brand's name
    (``brand_photo_reused``), and a page whose photo is sold under other brand names (``rebranded_photo``) is
    "concerns" at best. Neither makes a page "junk_or_scam" by itself: that band is the scorer's alone.

    With ``needs_reason`` (a scorer whose artifact says so, :attr:`unfret.scoring.Model.needs_reason`), "Worth a
    look" (``concerns``) also needs something to look at: a page the scorer puts there (a score from
    ``verdict_concerns_min``) with no reason, or only things missing from the page (:data:`ABSENCE_REASONS`, shown
    as its one note), is "Looks fine". With ``concrete_only`` a page that is "concerns" with no concrete reason (only
    :data:`_QUICK_NOT_ENOUGH_ALONE`) is "Looks fine" too, unless the scorer put it below ``verdict_careful_min``: the
    quick tier's scorer reads too little to tell a well-known shop from a fake one by itself (on the corpus's golden
    set of well-known sellers it called most pages "Worth a look").

    :param score: The 0–100 reliability score.
    :param features: The skill features from the graph state.
    :param findings: The skill findings.
    :param facts: The page facts.
    :param page_type: The classified page type.
    :param headline: The headline sentence; the catalogue's template for the band when not given.
    :param market: The shop's market (:func:`unfret.market.shop_market`), for reasons that depend on its law.
    :param confidence: The scorer's confidence (:class:`unfret.scoring.Assessment`); ``None`` without a scorer.
    :param concrete_only: Never warn without a concrete reason, whatever the scorer's confidence.
    :param needs_reason: "Worth a look" only with something to show for it (see above).
    :return: The verdict.
    """
    evidence = _evidence(features, findings, facts, page_type, market)
    band = band_for(score)
    sure = settings.verdict_serious_min_confidence_quick if concrete_only else settings.verdict_serious_min_confidence
    if band == "junk_or_scam" and confidence is not None and confidence < sure:
        band = "careful"  # "Serious concerns" only when the scorer is sure; otherwise "Think twice"

    fired = _fired(evidence)
    band = _with_reasons(band, score, set(fired), confidence, concrete_only=concrete_only, needs_reason=needs_reason)
    if band in {"reliable", "concerns"} and evidence.storefront and evidence.first("vat_fake"):
        band = "careful"  # a made-up VAT number is a false statement about who the seller is, whatever the score
    if band in {"reliable", "concerns"} and "brand_photo_reused" in fired:
        band = "careful"  # a known brand's own photo sold as another brand's: a false statement about the product
    if band == "reliable" and "rebranded_photo" in fired:
        band = "concerns"  # relabelled generic goods: worth a look and a price comparison, not a warning by itself
    if band == "reliable" and set(fired) == {"implausible_claims"} and _vouched_shop(evidence):
        # The one note on a page the scorer calls fine, from the content read's impression of a manufacturer's
        # measurable claim ("reduces microplastics by up to 90 %"), on an established shop: it contradicts the rest.
        del fired["implausible_claims"]
    order = list(REASON_SEVERITY)
    keys = sorted(fired, key=lambda k: (_SEVERITY_RANK[REASON_SEVERITY[k]], order.index(k)))
    reasons = [_reason(k, fired[k]) for k in keys]
    positives = [_positive(k, v) for k, v in _positive_params(evidence).items()]

    if band == "reliable":
        reasons = reasons[:1]
        # "No warning signs" only when there is truly nothing to note: next to a reason it would contradict it.
        positives = positives[:3] or ([] if reasons else [_positive("nothing_found", {})])
    else:
        reasons = reasons[:3] or [_reason("overall", {})]
        positives = positives[: 2 if band == "concerns" else 1]

    copy = catalogue()
    return Verdict(
        band=band,
        label=copy["labels"][band],
        headline=(without_label(headline, copy["labels"][band]) if headline else "")
        or template_headline(band, keys[:2], reasons, copy),
        reasons=reasons,
        positives=positives,
    )
