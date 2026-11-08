"""The shopper verdict: bands, plain-language reasons, coherence, languages and the copy catalogue."""

import json
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from veridex import shopper
from veridex.facts import PageFacts
from veridex.graph import SKILLS
from veridex.graph.state import Finding

# Every reason at once (on a storefront), to check every piece of copy renders cleanly.
ALL_REASON_FEATURES = {
    "dark_patterns.timer_resets": 1.0,
    "html_source_signals.timer_app": 1.0,
    "dark_patterns.timer": 1.0,
    "dark_patterns.preticked_addons": 1.0,
    "dark_patterns.checkout_markup_pct": 0.38,
    "dark_patterns.fee_mentions": 2.0,
    "reviews.j_shaped": 1.0,
    "reviews.duplicate_share": 0.4,
    "reviews.off_topic_share": 0.5,
    "html_source_signals.review_import_app": 1.0,
    "content.title_word_soup": 0.9,
    "content.generic_brand": 0.9,
    "content.generic_product": 0.9,
    "content.implausible_claims": 0.8,
    "content.scam_copy": 0.8,
    "listing.discount_pct": 0.85,
    "listing.seller_not_brand": 1.0,
    "listing.ships_from_china": 1.0,
    "html_source_signals.dropship_app": 1.0,
    "html_source_signals.supplier_trace": 1.0,
    "reverse_image.supplier_hits": 2.0,
    "reverse_image.photo_predates_listing": 1.0,
    "eu_registry.withdrawal_right": 0.0,
    "eu_registry.imprint": 0.0,
    "content.missing_trust_info": 0.9,
    "eu_registry.vat_present": 1.0,
    "eu_registry.vat_valid": 0.0,
    "domain_age.known": 1.0,
    "domain_age.under_6_months": 1.0,
    "domain_age.age_years": 0.1,
    "domain_age.term_years": 1.0,
    "injection.instruction_hidden": 1.0,
    "injection.instruction_visible": 1.0,
}
ALL_FINDINGS = [
    Finding(skill="html_source_signals", type="timer_app", evidence="Hurrify countdown timer"),
    Finding(skill="dark_patterns", type="preticked_addon", evidence='Pre-ticked paid add-on: "Shipping protection".'),
    Finding(skill="reviews", type="review_velocity", evidence="4,800 ratings since 2026-10-01 (120.0 per day)."),
    Finding(skill="content", type="implausible_claim", evidence="99% Minder bacterien"),
    Finding(skill="reverse_image", type="supplier_photo", evidence="The main photo also appears on aliexpress.com."),
]
FACTS = PageFacts(
    title="INSOON Dinosaurus Museum Book Nook bouwspeelgoedset, boekenend bouwstenen kit, jongens en meisjes",
    brand="INSOON",
    seller="HUAXINGDA Direct",
    ships_from="Shenzhen, China",
    rating_count=4800,
    date_first_available=datetime.now(tz=UTC).date() - timedelta(days=42),
)
JARGON = re.compile(
    r"\b(?:content|listing|reviews|html_source_signals|dark_patterns|injection|eu_registry|domain_age"
    r"|reverse_image|classify)\.[a-z_]+|=\s*-?\d"
)


def build(score: int, features: dict[str, float], page_type: str = "storefront", **kw: object) -> shopper.Verdict:
    return shopper.build(
        score,
        features=features,
        findings=kw.pop("findings", []),  # type: ignore[arg-type]
        facts=kw.pop("facts", PageFacts()),  # type: ignore[arg-type]
        page_type=page_type,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("score", "band"), [(100, "reliable"), (80, "reliable"), (79, "concerns"), (50, "concerns"), (49, "careful"),
                        (25, "careful"), (24, "junk_or_scam"), (0, "junk_or_scam")]
)  # fmt: skip
def test_bands(score: int, band: str) -> None:
    assert shopper.band_for(score) == band


@pytest.mark.parametrize("language", shopper.LANGUAGES)
def test_catalogue_is_complete(language: str) -> None:
    copy = json.loads((shopper.COPY_DIR / f"{language}.json").read_text(encoding="utf-8"))
    assert set(copy["reasons"]) == set(shopper.REASON_SEVERITY)
    assert set(copy["positives"]) == set(shopper.POSITIVE_KEYS)
    for entry in [*copy["reasons"].values(), *copy["positives"].values()]:
        assert entry["title"].strip() and entry["detail"].strip()
        assert "{" not in entry["title"]  # titles never carry page content
    bands = {"reliable", "concerns", "careful", "junk_or_scam"}
    assert set(copy["labels"]) == bands and set(copy["headlines"]) == bands
    assert set(copy["age_units"]) == {"day", "week", "month", "year"}


@pytest.mark.parametrize("language", shopper.LANGUAGES)
@pytest.mark.parametrize("page_type", ["storefront", "aggregate_listing"])
def test_every_reason_renders_without_jargon(language: str, page_type: str) -> None:
    verdict = build(10, ALL_REASON_FEATURES, page_type, findings=ALL_FINDINGS, facts=FACTS, language=language)
    for key in shopper.REASON_SEVERITY:
        reason = shopper._reason(key, _params(key, language), language)
        assert not JARGON.search(reason.title + " " + reason.detail), (key, reason)
    for reason in verdict.reasons:
        assert not JARGON.search(f"{reason.title} {reason.detail}"), reason
    assert not JARGON.search(verdict.headline + verdict.label)
    assert len(verdict.reasons) <= 3


def _params(key: str, language: str) -> dict[str, str]:
    # Fill every placeholder a reason may use.
    return {
        "app": "Hurrify",
        "label": "“Shipping protection”",
        "pct": "40%",
        "count": "4,800",
        "age": "6 weeks",
        "brand": "“INSOON”",
        "claim": "“99%”",
        "seller": "“HUAXINGDA”",
        "place": "China",
        "sites": "aliexpress.com",
    }


def test_strongest_reasons_first_and_groups_collapse() -> None:
    verdict = build(10, ALL_REASON_FEATURES, findings=ALL_FINDINGS, facts=FACTS)
    severities = [r.severity for r in verdict.reasons]
    assert severities == sorted(severities, key=lambda s: shopper._SEVERITY_RANK[s])
    types = {r.type for r in verdict.reasons}
    assert not {"timer_resets", "countdown"} <= types  # one countdown story, not two


def test_reliable_leads_with_positives_and_at_most_one_note() -> None:
    features = {"content.brand_recognized": 1.0, "dark_patterns.preticked_addons": 1.0, "content.scam_copy": 0.8}
    verdict = build(92, features, facts=PageFacts(brand="Philips"))
    assert verdict.band == "reliable"
    assert len(verdict.reasons) <= 1
    assert verdict.positives and verdict.positives[0].type == "known_brand"


def test_reliable_without_anything_says_nothing_was_found() -> None:
    verdict = build(95, {})
    assert ([p.type for p in verdict.positives], verdict.reasons) == (["nothing_found"], [])


def test_low_score_without_a_single_reason_is_honest() -> None:
    verdict = build(30, {"listing.title_length": 80.0})
    assert [r.type for r in verdict.reasons] == ["overall"]


def test_structural_features_never_surface() -> None:
    features = {
        "listing.is_marketplace": 1.0,
        "listing.is_shopify": 0.0,
        "reviews.verified_share": 1.0,
        "reviews.has_reviews": 1.0,
        "listing.log_rating_count": 3.2,
    }
    for score in (95, 60, 30, 5):
        verdict = build(score, features, "aggregate_listing")
        assert all(r.type == "overall" for r in verdict.reasons)


def test_users_amazon_result_is_coherent() -> None:
    # The result a user got: score 95 on an Amazon listing with a deal countdown (the platform's, so no timer
    # features any more), marketplace trust info scored by the old prompt, and structural features.
    features = {
        "listing.is_marketplace": 1.0,
        "content.missing_trust_info": 0.8,
        "listing.is_shopify": 0.0,
        "reviews.verified_share": 1.0,
        "content.implausible_claims": 0.7,
    }
    findings = [Finding(skill="content", type="implausible_claim", evidence="99% Minder bacterien")]
    verdict = build(95, features, "aggregate_listing", findings=findings, language="nl")
    assert verdict.band == "reliable" and verdict.label == "Ziet er betrouwbaar uit"
    assert [r.type for r in verdict.reasons] == ["implausible_claims"]  # one note, no trust or timer reason
    assert verdict.positives


def test_no_trust_reason_on_marketplaces() -> None:
    features = {"content.missing_trust_info": 0.9, "eu_registry.withdrawal_right": 0.0}
    assert not build(40, features, "aggregate_listing").reasons[0].type.startswith(("missing", "no_"))
    assert build(40, features, "storefront").reasons[0].type == "no_withdrawal"


def test_word_soup_and_generic_brand_surface_from_the_llm_judgement() -> None:
    features = {"content.title_word_soup": 0.85, "content.generic_brand": 0.8}
    verdict = build(45, features, "aggregate_listing", facts=FACTS, language="nl")
    titles = [r.title for r in verdict.reasons]
    assert titles == ["Titel vol zoekwoorden", "Onbekend merk"]
    assert "“INSOON”" in verdict.reasons[1].detail


def test_unknown_brand_is_only_said_on_marketplaces() -> None:
    # Half the honest small shops in the corpus are "not a brand the LLM knows": not a reason on their own pages.
    features = {"content.generic_brand": 1.0}
    assert "generic_brand" in [r.type for r in build(45, features, "aggregate_listing", facts=FACTS).reasons]
    assert "generic_brand" not in [r.type for r in build(45, features, "storefront", facts=FACTS).reasons]


def test_sold_by_brand_is_good_news_only_for_a_real_brand() -> None:
    own = FACTS.model_copy(update={"seller": "INSOON"})
    sold_by_itself = {"listing.seller_not_brand": 0.0}
    assert "sold_by_brand" in [p.type for p in build(60, sold_by_itself, "aggregate_listing", facts=own).positives]
    # A white-label brand selling under its own made-up name is no reassurance next to "Unknown brand".
    generic = build(60, {**sold_by_itself, "content.generic_brand": 0.8}, "aggregate_listing", facts=own)
    assert "sold_by_brand" not in [p.type for p in generic.positives]


def test_review_velocity_reads_naturally() -> None:
    verdict = build(20, {}, "aggregate_listing", findings=ALL_FINDINGS, facts=FACTS, language="nl")
    reason = next(r for r in verdict.reasons if r.type == "review_velocity")
    assert reason.detail == "4.800 beoordelingen voor een product dat pas 6 weken te koop staat."
    en = build(20, {}, "aggregate_listing", findings=ALL_FINDINGS, facts=FACTS)
    assert next(r for r in en.reasons if r.type == "review_velocity").detail.startswith("4,800 ratings")


@pytest.mark.parametrize(
    ("header", "language"),
    [
        ("nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7", "nl"),
        ("en-GB,en;q=0.9,nl;q=0.8", "en"),
        ("de-DE,de;q=0.9,nl;q=0.5", "nl"),
        ("fr-FR", "en"),
        ("en;q=0.2, nl;q=0.8", "nl"),
        ("", "en"),
        (None, "en"),
        ("nl;q=abc", "en"),
    ],
)
def test_accept_language(header: str | None, language: str) -> None:
    assert shopper.pick_language(header) == language


def test_labels_and_headline_follow_the_language() -> None:
    nl = build(10, {"dark_patterns.timer_resets": 1.0}, language="nl")
    assert (nl.label, nl.reasons[0].title) == ("Waarschijnlijk rommel of oplichting", "Neppe afteltimer")
    assert nl.headline == shopper.copy_for("nl")["headlines"]["junk_or_scam"]
    assert build(10, {}, language="xx").language == "en"


def test_age_wording() -> None:
    e = shopper._Evidence({}, {}, PageFacts(), "storefront", "nl")
    assert [e.age(d) for d in (1, 9, 42, 200, 1100)] == ["1 dag", "9 dagen", "6 weken", "7 maanden", "3 jaar"]
    assert date.today()  # keep the import honest for type checkers


@pytest.mark.parametrize("language", shopper.LANGUAGES)
def test_every_feature_has_a_human_label(language: str) -> None:
    labels = shopper.copy_for(language)["features"]
    declared = {f"{s.name}.{f}" for s in SKILLS for f in s.features}
    assert declared <= set(labels), sorted(declared - set(labels))
    assert all(label and not JARGON.search(label) for label in labels.values())
    assert declared >= shopper.CONTEXT_FEATURES


def test_flags_get_labels_and_context_reads_neutral() -> None:
    flags = [
        {"type": "content.title_word_soup", "evidence": "", "weight": 1.3},
        {"type": "listing.is_marketplace", "evidence": "", "weight": 0.9},
        {"type": "eu_registry.imprint", "evidence": "", "weight": 0.5},
        {"type": "reviews.duplicate_share", "evidence": "40% repeat", "weight": 0.2},
    ]
    labelled = shopper.label_flags(flags, "nl")
    assert [f["label"] for f in labelled] == [
        "Titel vol zoekwoorden",
        "Aanbod op een marktplaats",
        "Bedrijfsgegevens vermeld",
        "Gekopieerde reviews",
    ]
    assert [(f["kind"], f["severity"]) for f in labelled] == [
        ("signal", "high"),
        ("context", "low"),
        ("context", "low"),
        ("signal", "low"),
    ]
    assert labelled[3]["evidence"] == "40% repeat"
