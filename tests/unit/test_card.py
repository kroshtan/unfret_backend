"""The result card: a source for every finding, what was checked, the number of checks, one next step."""

import json
from typing import Any

import pytest

from unfret import card, public_result, shares, shopper
from unfret.facts import PageFacts
from unfret.graph import SKILLS, services
from unfret.graph.skills import NOT_APPLICABLE, UNAVAILABLE
from unfret.x402.verdict import skills_for

COPY = shopper.catalogue()
ALL_REASONS = set(shopper.REASON_SEVERITY) | set(services.REASON_SEVERITY)
ALL_POSITIVES = set(shopper.POSITIVE_KEYS) | set(services.POSITIVE_KEYS)


def test_every_finding_has_a_source_in_words() -> None:
    assert set(card.REASON_SOURCES) >= ALL_REASONS, ALL_REASONS - set(card.REASON_SOURCES)
    assert set(card.POSITIVE_SOURCES) >= ALL_POSITIVES, ALL_POSITIVES - set(card.POSITIVE_SOURCES)
    used = set(card.REASON_SOURCES.values()) | set(card.POSITIVE_SOURCES.values())
    assert used <= set(COPY["sources"]) and set(COPY["sources"]) <= used | {"page"}
    assert COPY["sources"]["rdap"] == "Domain registry (RDAP)"


def test_every_finding_but_the_overall_one_belongs_to_an_area() -> None:
    general = {"overall", "service_little_known", "p2p_little_known"}
    kinds = {"shop": set(shopper.REASON_SEVERITY) - {k for k in shopper.REASON_SEVERITY if k.startswith("p2p_")},
             "ad": {k for k in shopper.REASON_SEVERITY if k.startswith("p2p_")},
             "service": set(services.REASON_SEVERITY)}  # fmt: skip
    for kind, keys in kinds.items():
        missing = {k for k in keys - general if card.area_of(k, kind) is None}
        assert not missing, (kind, missing)


def test_the_checklist_and_its_notes_are_in_the_catalogue() -> None:
    for kind, areas in card.AREAS.items():
        assert set(COPY["checklist"]["areas"][kind]) == set(areas)
    assert set(card.STEP_FOR_REASON.values()) <= set(COPY["next_steps"])
    for band in ("reliable", "concerns", "careful", "junk_or_scam"):
        assert {band, f"ad_{band}", f"service_{band}"} <= set(COPY["next_steps"])

    def words(node: Any) -> list[str]:
        return [w for v in node.values() for w in words(v)] if isinstance(node, dict) else [str(node)]

    text = json.dumps(words({"c": COPY["checklist"], "n": COPY["next_steps"], "s": COPY["sources"],
                             "p": COPY["same_product"]}))  # fmt: skip
    assert not public_result.FORBIDDEN_RE.search(text)  # never "scam" or "fraud"


def state(features: dict[str, float], page_type: str = "storefront", **extra: Any) -> Any:
    return {"features": features, "findings": [], "facts": PageFacts(), "page_type": page_type, "url": "", **extra}


def verdict(band: str = "reliable", reasons: tuple[str, ...] = (), positives: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "band": band,
        "label": COPY["labels"][band],
        "headline": "…",
        "reasons": [
            {"type": k, "title": COPY["reasons"][k]["title"], "detail": "…", "severity": "high"} for k in reasons
        ],
        "positives": [{"type": k, "title": COPY["positives"][k]["title"], "detail": "…"} for k in positives],
    }


CLEAN = {
    "eu_registry.vat_valid": 1.0,
    "eu_registry.imprint": 1.0,
    "domain_age.known": 1.0,
    "domain_age.age_years": 12.0,
    "listing.discount_pct": 0.0,
    "content.generic_brand": 0.0,
    "reviews.has_reviews": 1.0,
    "pricing.implausible_discount": 0.0,
    "dark_patterns.timer": 0.0,
    "html_source_signals.dropship_app": 0.0,
    "injection.instruction_hidden": 0.0,
    "reverse_image.searched": 1.0,
}


def test_a_clean_result_says_what_and_how_much_was_checked() -> None:
    out = card.complete(
        verdict(positives=("established_domain", "vat_registered")), state(CLEAN), available=SKILLS, applicable=SKILLS
    )
    assert [(i["key"], i["status"]) for i in out["checklist"]] == [
        ("who", "checked"), ("product", "checked"), ("reviews", "checked"), ("prices", "checked"),
        ("tricks", "checked"),
    ]  # fmt: skip
    assert out["checks_run"] == 10  # each skill that measured something, once
    assert [p["source"] for p in out["positives"]] == ["Domain registry (RDAP)", "EU VAT register (VIES)"]
    assert out["next_step"]["type"] == "reliable" and "buyer protection" in out["next_step"]["text"]
    assert out["same_product"] is None
    shopper.Verdict.model_validate(out)  # the API's own model


@pytest.mark.parametrize(
    ("features", "available", "note"),
    [
        ({"reviews.has_reviews": 0.0}, SKILLS, "The page shows no reviews."),
        ({f"reviews.{UNAVAILABLE}": 1.0}, SKILLS, "Couldn't be checked this time."),
        ({}, skills_for("quick", SKILLS), "Not part of a quick check."),
        ({}, SKILLS, "Nothing on the page to check."),
    ],
)
def test_an_area_not_checked_says_why(features: dict[str, float], available: Any, note: str) -> None:
    out = card.complete(verdict(), state(features), available=available, applicable=SKILLS)
    reviews = next(i for i in out["checklist"] if i["key"] == "reviews")
    assert (reviews["status"], reviews["note"]) == ("not_checked", note)


def test_a_finding_marks_its_area_and_names_its_source() -> None:
    features = CLEAN | {
        "domain_age.under_6_months": 1.0,
        "domain_age.age_years": 0.05,
        f"claims.{NOT_APPLICABLE}": 1.0,
    }
    out = card.complete(verdict("careful", ("new_domain",)), state(features), available=SKILLS, applicable=SKILLS)
    assert out["reasons"][0]["source"] == "Domain registry (RDAP)"
    who = next(i for i in out["checklist"] if i["key"] == "who")
    assert (who["status"], who["note"]) == ("concern", "Brand-new web shop")
    assert out["next_step"]["type"] == "shop_reviews"


def test_findings_beyond_the_three_shown_still_mark_their_area() -> None:
    # Reviews repeat each other, but three stronger findings fill the verdict: the reviews aren't ticked as fine.
    features = CLEAN | {"reviews.duplicate_share": 0.5, "dark_patterns.timer_resets": 1.0,
                        "dark_patterns.preticked_addons": 1.0, "pricing.implausible_discount": 1.0}  # fmt: skip
    shown = ("timer_resets", "preticked_addon", "implausible_discount")
    out = card.complete(verdict("careful", shown), state(features), available=SKILLS, applicable=SKILLS)
    reviews = next(i for i in out["checklist"] if i["key"] == "reviews")
    assert (reviews["status"], reviews["note"]) == ("concern", "Copy-paste reviews")


def test_serious_concerns_suggest_another_shop_unless_safety_comes_first() -> None:
    out = card.complete(verdict("junk_or_scam", ("new_domain",)), state({}), available=SKILLS, applicable=SKILLS)
    assert out["next_step"] == {"type": "junk_or_scam", "text": "Better to buy this from a shop you know."}
    out = card.complete(
        verdict("junk_or_scam", ("safety_gate_alert",)), state({}), available=SKILLS, applicable=SKILLS
    )
    assert out["next_step"]["type"] == "safety_alert"


SAME = {
    "title": "Looks like the same product, sold elsewhere for $12.99",
    "detail": "The main photo is the same photo as on cheap.shop. Unfret saw it there for $12.99 on November 7, 2026.",
    "where": "cheap.shop",
    "domain": "cheap.shop",
    "others": 0,
    "price": 12.99,
    "currency": "USD",
    "price_text": "$12.99",
    "price_seen_on": "2026-11-07",
    "image": "https://unfret.net/v1/images/1/thumbnail.jpg",
    "own_image": "https://unfret.net/v1/images/2/thumbnail.jpg",
}


def test_the_same_product_elsewhere_comes_with_the_card_on_product_pages_only() -> None:
    out = card.complete(verdict(), state(CLEAN, same_product=SAME), available=SKILLS, applicable=SKILLS)
    assert out["same_product"] == SAME
    assert out["next_step"]["type"] == "same_product"
    ad = card.complete(verdict(), state({}, "p2p_listing", same_product=SAME), available=SKILLS, applicable=SKILLS)
    assert ad["same_product"] is None


def test_ads_and_service_providers_get_their_own_checklist() -> None:
    ad = card.complete(verdict("concerns", ("p2p_advance_payment",)), state({"off_platform.advance_payment": 1.0},
                       "p2p_listing"), available=SKILLS, applicable=SKILLS)  # fmt: skip
    assert [i["label"] for i in ad["checklist"]] == [
        "The seller", "The price", "The photos", "How you'd pay", "The ad's text",
    ]  # fmt: skip
    assert next(i for i in ad["checklist"] if i["key"] == "payment")["status"] == "concern"
    assert ad["next_step"]["type"] == "p2p_pay_safely"
    service = card.complete(verdict("concerns", ("kvk_missing",)), state({}, "service_site"), available=SKILLS,
                            applicable=SKILLS)  # fmt: skip
    assert service["next_step"]["type"] == "service_concerns"
    assert service["reasons"][0]["source"] == "The page"


def test_a_shared_result_keeps_the_card_and_an_older_result_hashes_as_before() -> None:
    full = card.complete(verdict(positives=("established_domain",)), state(CLEAN, same_product=SAME),
                         available=SKILLS, applicable=SKILLS)  # fmt: skip
    shown = public_result.public_result({"score": 90, "verdict": full})["verdict"]
    assert shown["checks_run"] == 10 and shown["checklist"][0]["label"] == "Who's behind the shop"
    assert shown["next_step"]["type"] == "same_product"
    assert shown["same_product"]["price_text"] == "$12.99"
    assert shown["positives"][0]["source"] == "Domain registry (RDAP)"
    # A result from before the card (or a cached one) has none of it, and its share hash is unchanged.
    older = verdict(positives=("established_domain",))
    assert set(public_result.public_result({"score": 90, "verdict": older})["verdict"]) == {
        "band", "label", "symbol", "headline", "reasons", "positives",
    }  # fmt: skip
    empty = older | {"checklist": [], "checks_run": None, "next_step": None, "same_product": None}
    assert shares.result_hash({"score": 90, "verdict": empty}) == shares.result_hash({"score": 90, "verdict": older})


def test_a_same_product_naming_a_forbidden_word_is_left_out_of_a_share() -> None:
    bad = SAME | {"where": "scam-deals.shop", "domain": "scam-deals.shop"}
    full = verdict() | {"same_product": bad}
    notes: list[str] = []
    shown = public_result.public_result({"score": 90, "verdict": full}, notes)["verdict"]
    assert "same_product" not in shown and notes == ["same product"]
