"""The result card: a source for every finding, what was checked, the number of checks, one next step."""

import json
from typing import Any

import pytest

from unfret import card, public_result, result_cache, scoring, shares, shopper
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


def test_delivery_and_returns_are_with_the_prices_not_who_is_behind_the_shop() -> None:
    assert COPY["checklist"]["areas"]["shop"]["prices"] == "Prices, delivery and returns"
    assert "shop_policies" in card.AREAS["shop"]["prices"] and "shop_policies" not in card.AREAS["shop"]["who"]
    returns = ("no_withdrawal", "no_return_policy", "returns_abroad", "return_address_withheld", "restocking_fee")
    for reason in ("long_delivery", *returns):
        assert card.area_of(reason, "shop") == "prices", reason
    assert card.area_of("origin_doubt", "shop") == "who"  # where the shop says it is, not its returns


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
    assert out["same_product"] == SAME | {"brands": []}  # no other brand names known there
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
    empty = older | {
        "checklist": [], "checks_run": None, "next_step": None, "same_product": None, "incomplete": None,
    }  # fmt: skip
    assert shares.result_hash({"score": 90, "verdict": empty}) == shares.result_hash({"score": 90, "verdict": older})


def test_a_same_product_naming_a_forbidden_word_is_left_out_of_a_share() -> None:
    bad = SAME | {"where": "scam-deals.shop", "domain": "scam-deals.shop"}
    full = verdict() | {"same_product": bad}
    notes: list[str] = []
    shown = public_result.public_result({"score": 90, "verdict": full}, notes)["verdict"]
    assert "same_product" not in shown and notes == ["same product"]


def _degraded(features: dict[str, float]) -> bool:
    model = scoring.Model.from_dict(
        {"version": "t", "features": ["content.scam_copy"], "means": [0.0], "scales": [1.0], "coefficients": [1.0],
         "intercept": 0.0, "calibration": []}
    )  # fmt: skip
    assessment = scoring.score(features, model=model)
    assert assessment is not None
    return assessment.degraded


def test_a_result_whose_ai_read_did_not_finish_says_so() -> None:
    # The content read failed or ran out of time: the assessment is degraded, and the card says so in one line.
    features = CLEAN | {f"content.{UNAVAILABLE}": 1.0}
    out = card.complete(verdict("concerns", ("new_domain",)), state(features), available=SKILLS, applicable=SKILLS)
    assert out["incomplete"]["steps"] == ["content"]
    assert out["incomplete"]["text"] == (
        "Part of this check didn't finish (the AI read of the page), so this answer may be milder than it should be. "
        "It still counted as a check. Checking again after an hour may help."
    )
    shopper.Verdict.model_validate(out)
    assert _degraded(features)  # the same condition as the assessment's


@pytest.mark.parametrize(
    "features",
    [CLEAN, CLEAN | {f"reviews.{UNAVAILABLE}": 1.0}],  # another check that couldn't run doesn't degrade the result
    ids=["all finished", "a minor check failed"],
)
def test_a_result_with_every_key_step_finished_has_no_such_line(features: dict[str, float]) -> None:
    out = card.complete(verdict(), state(features), available=SKILLS, applicable=SKILLS)
    assert out["incomplete"] is None
    assert not _degraded(features)  # the same condition as the assessment's


def test_every_key_step_has_a_name_in_words() -> None:
    assert set(COPY["incomplete"]["steps"]) >= scoring.KEY_SKILLS


def test_the_line_says_how_long_a_repeat_gets_the_same_result() -> None:
    # "after an hour": the same input checked again within the hour gets this result back from the result cache.
    assert result_cache.RESULT_TTL_S == 3600
    assert "after an hour" in COPY["incomplete"]["after"]


def test_a_shared_result_says_a_step_did_not_finish_without_what_it_cost() -> None:
    features = CLEAN | {f"content.{UNAVAILABLE}": 1.0}
    full = card.complete(verdict("concerns", ("new_domain",)), state(features), available=SKILLS, applicable=SKILLS)
    shown = public_result.public_result({"score": 60, "verdict": full})["verdict"]
    assert shown["incomplete"] == {
        "steps": ["content"],
        "text": "Part of this check didn't finish (the AI read of the page), so this answer may be milder than it "
        "should be.",
    }
    # An unknown step (a newer server's) is left out rather than shown by its internal name.
    odd = full | {"incomplete": {"steps": ["something_new"], "text": "…"}}
    assert "incomplete" not in public_result.public_result({"score": 60, "verdict": odd})["verdict"]


def test_each_area_says_how_many_checks_looked_at_it_and_they_add_up() -> None:
    # The header's number is the sum of the areas' counts; a check serving two areas (dark patterns: prices and
    # hidden tricks) counts once, in the first.
    features = CLEAN | {"dark_patterns.timer": 1.0, "html_source_signals.dropship_app": 1.0}
    out = card.complete(verdict("careful", ("countdown",)), state(features), available=SKILLS, applicable=SKILLS)
    counts = {i["key"]: i["checks"] for i in out["checklist"]}
    assert sum(counts.values()) == out["checks_run"] == 10
    assert counts == {"who": 2, "product": 3, "reviews": 1, "prices": 2, "tricks": 2}
    assert next(i for i in out["checklist"] if i["key"] == "tricks")["status"] == "concern"


@pytest.mark.parametrize("page_type", ["storefront", "aggregate_listing", "p2p_listing", "service_site"])
def test_the_areas_add_up_to_the_checks_on_every_kind_of_page(page_type: str) -> None:
    # Every skill that measured something, on any kind of page: the counts never miss one or count one twice.
    features = {f"{s.name}.{f}": 1.0 for s in SKILLS for f in s.features}
    out = card.complete(verdict(), state(features, page_type), available=SKILLS, applicable=SKILLS)
    assert out["checks_run"] == sum(i["checks"] for i in out["checklist"]) > 0
    # Each skill of a listed area counts once (a marketplace listing has no "Who's behind the shop": its checks
    # aren't counted either).
    listed = {i["key"] for i in out["checklist"]}
    kind = card.kind_of(page_type)
    assert out["checks_run"] == len({s for key in listed for s in card.AREAS[kind][key]})


def test_a_shared_result_keeps_the_areas_counts() -> None:
    full = card.complete(verdict(), state(CLEAN), available=SKILLS, applicable=SKILLS)
    shown = public_result.public_result({"score": 90, "verdict": full})["verdict"]
    assert sum(i["checks"] for i in shown["checklist"]) == shown["checks_run"]


@pytest.mark.parametrize(
    ("kwargs", "sentence"),
    [
        ({"free_check": True, "free_left": 4}, "This check used one of your free checks; 4 are left."),
        ({"free_check": True, "free_left": 1}, "This check used one of your free checks; 1 is left."),
        ({"free_check": True, "free_left": 0}, "This check used one of your free checks; none are left."),
        ({"free_check": True}, "This check used one of your free checks."),  # a free check without a pass
        ({"bought_left": 23}, "This check used one of your checks; 23 are left."),
        ({}, "This check used one of your checks."),
        ({"tier": "quick", "charged": False}, "Quick checks are free."),
        ({"charged": False}, "You checked this page recently, so no new check was used."),
        ({"score": -1, "charged": False}, "This page couldn't be assessed, so no check was used."),
    ],
)
def test_each_charge_line_is_a_sentence(kwargs: dict[str, Any], sentence: str) -> None:
    args: dict[str, Any] = {"tier": "deep", "score": 70, "charged": True, "free_check": False} | kwargs
    assert card.charge_note(**args) == sentence
    assert sentence[0].isupper() and sentence.endswith(".") and len(sentence.split()) >= 4


# A deep check of a marketplace listing read on the phone (Android 0.1.0-beta.2, production 0.14.0): "Worth a look",
# "Several small signals" and every area ticked fine, the reviews not checked. No reason fired (the discount below
# "big", a little pushy copy), but the scorer weighed both: the band came from signs the screen didn't show.
LISTING_FEATURES = {
    "listing.is_marketplace": 1.0,
    "listing.discount_pct": 0.3,
    "listing.seller_not_brand": 0.0,
    "listing.title_length": 60.0,
    "content.scam_copy": 0.4,
    "content.generic_product": 0.2,
    "content.generic_brand": 0.0,
    "content.brand_recognized": 1.0,
    "reviews.has_reviews": 0.0,
    "review_copies.checked": 0.0,
    "pricing.reference_anchor": 0.0,
    "dark_patterns.timer": 0.0,
    "html_source_signals.timer_app": 0.0,
    "injection.hidden_chars": 0.0,
    "brand_presence.wikidata": 0.0,
    "safety_gate.alert": 0.0,
    "market_price.known": 0.0,
}
LISTING_FLAGS = [
    {"type": "listing.discount_pct", "evidence": "", "weight": 0.874},
    {"type": "reviews.verified_share", "evidence": "", "weight": 0.531},  # describes the page: no concern
    {"type": "listing.is_marketplace", "evidence": "", "weight": 0.386},  # describes the page: no concern
    {"type": "content.scam_copy", "evidence": "", "weight": 0.343},
]


def _card(features: dict[str, float], flags: list[dict[str, Any]], score: int, page_type: str) -> dict[str, Any]:
    signs = shopper.small_signs(flags)
    built = shopper.build(score, features=features, findings=[], facts=PageFacts(), page_type=page_type, signs=signs)
    judged = state(features, page_type, assessment={"flags": flags})
    return card.complete(built.model_dump(), judged, available=SKILLS, applicable=SKILLS)


def test_the_signs_that_set_the_band_are_on_the_screen() -> None:
    result = _card(LISTING_FEATURES, LISTING_FLAGS, 62, "aggregate_listing")

    assert result["band"] == "concerns"
    [reason] = result["reasons"]
    assert reason["title"] == "Two small signs"  # counted, not "several"
    assert reason["detail"] == (
        "No single big problem, but together these lowered the score: a sizeable discount and some pushy sales talk."
    )
    assert result["headline"] == "A sizeable discount and some pushy sales talk."
    status = {i["key"]: (i["status"], i.get("note")) for i in result["checklist"]}
    assert status["prices"] == ("concern", "A sizeable discount")
    assert status["tricks"] == ("concern", "Some pushy sales talk")
    assert status["reviews"][0] == "not_checked"
    # Every sign is a line of an area that isn't ticked fine.
    notes = " ".join(i.get("note") or "" for i in result["checklist"] if i["status"] == "concern").lower()
    for sign in shopper.small_signs(LISTING_FLAGS):
        assert shopper.sign_text(sign).lower() in notes


def test_one_small_sign_is_one_small_sign() -> None:
    flags = [{"type": "content.title_word_soup", "evidence": "", "weight": 0.4}]
    result = _card({**LISTING_FEATURES, "content.title_word_soup": 0.5}, flags, 70, "aggregate_listing")

    assert result["band"] == "concerns"
    assert [r["title"] for r in result["reasons"]] == ["One small sign"]
    assert result["reasons"][0]["detail"].startswith("Not a big problem by itself")
    assert "several" not in json.dumps(result).lower() and "a few" not in result["headline"].lower()
    assert {i["key"]: i["status"] for i in result["checklist"]}["product"] == "concern"


def test_signs_in_one_area_are_all_in_its_line() -> None:
    flags = [
        {"type": "content.implausible_claims", "evidence": "", "weight": 0.6},
        {"type": "content.generic_brand", "evidence": "", "weight": 0.5},
    ]
    result = _card(LISTING_FEATURES, flags, 40, "storefront")

    assert result["band"] == "careful" and result["reasons"][0]["title"] == "Two signs"  # "small" only at Worth a look
    product = next(i for i in result["checklist"] if i["key"] == "product")
    assert product["note"] == "Some hard-to-believe claims and a brand we don't know"


def test_a_sign_marks_its_area_next_to_a_reason_that_fired() -> None:
    features = {**LISTING_FEATURES, "reviews.has_reviews": 1.0, "reviews.j_shaped": 1.0}
    result = _card(features, LISTING_FLAGS, 55, "aggregate_listing")

    assert [r["type"] for r in result["reasons"]] == ["review_shape"]
    status = {i["key"]: i["status"] for i in result["checklist"]}
    assert status["reviews"] == status["prices"] == status["tricks"] == "concern"


def test_looks_fine_keeps_its_areas_and_its_one_note() -> None:
    result = _card(LISTING_FEATURES, LISTING_FLAGS, 90, "aggregate_listing")

    assert result["band"] == "reliable"
    assert all(i["status"] != "concern" for i in result["checklist"])


# Features that are good news when present: a scorer's small weight on one is no concern to show.
GOOD_NEWS = frozenset({"us_identity.business_entity", "us_identity.us_address", "us_identity.us_phone"})


def test_every_signal_a_shipped_scorer_weighs_has_words_and_an_area() -> None:
    words = COPY["small_signs"]["signs"]
    for path in (scoring.MODEL_PATH, scoring.MODEL_QUICK_PATH):
        model = scoring.Model.from_dict(json.loads(path.read_text()))
        for name, coefficient in zip(model.features, model.coefficients, strict=True):
            if name not in shopper.CONTEXT_FEATURES and name not in words:
                # A signal without words would set the band unseen: only features that describe the page, that never
                # raise the risk when measured (a negative or zero weight) or that are good news may go without.
                assert name in GOOD_NEWS or coefficient <= 0.0, name
    for name in words:
        assert card.area_of(shopper.SIGN_REASONS[name], "shop") is not None, name
    copy = COPY["small_signs"]
    shown = [*words.values(), *copy["title"].values(), *copy["title_small"].values(), *copy["detail"].values()]
    assert not public_result.FORBIDDEN_RE.search(json.dumps(shown))  # never "scam" or "fraud"


def test_progress_counts_the_checks_the_checklist_counts() -> None:
    features = {**CLEAN, "reviews.has_reviews": 0.0}
    _, checks_run = card.checklist(
        "shop", features, {}, available={s.name for s in SKILLS}, applicable={s.name for s in SKILLS}
    )
    by_skill: dict[str, dict[str, float]] = {}
    for name, value in features.items():
        by_skill.setdefault(name.split(".", 1)[0], {})[name] = value
    steps = ["preprocess", "classify", "triage", *by_skill]
    done = sum(card.is_check(step, by_skill.get(step, {}), "storefront") for step in steps)
    assert done == checks_run
    assert not card.is_check("reviews", {"reviews.has_reviews": 0.0}, "storefront")  # nothing to look at
    assert not card.is_check("content", {"content.scam_copy": 0.1}, "service_site")  # not on a company's checklist
