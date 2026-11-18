"""The scorer training code, on synthetic data (the corpus itself stays private)."""

import math
import random

import pytest

from evals.corpus import Label, LabelRecord, Split
from evals.train import (
    FEATURE_SIGNS,
    MAX_UNIT_LOGIT,
    MIN_SUPPORT,
    OFFLINE_FEATURES,
    RULE_MIRRORS,
    build,
    evaluate,
    feature_names,
    fit,
    source_leakage,
)


def record(i: int, label: Label, split: Split, source: str = "synthetic", page_type: str = "") -> LabelRecord:
    return LabelRecord(
        id=f"p{i}",
        label=label,
        label_source="agreed",
        confidence=1.0,
        platform="Amazon",
        page_type=page_type or ("aggregate_listing" if i % 2 else "storefront"),
        source=source,
        origin_dataset="test",
        origin_url="",
        origin_id=str(i),
        collected_at="2026",
        split=split,
    )


def test_feature_names_cover_offline_skills_only() -> None:
    names = feature_names()
    assert "listing.discount_pct" in names
    assert not any(n.startswith("domain_age.") for n in names)
    # Of the photo check, only its brand comparison, which a replay measures offline (evals.photo_brands).
    assert [n for n in names if n.startswith("reverse_image.")] == list(OFFLINE_FEATURES)
    assert {c for cols in RULE_MIRRORS.values() for c in cols} <= set(names)


def test_fit_separates_a_learnable_signal_and_calibrates() -> None:
    rng = random.Random(7)
    names = ["listing.discount_pct", "content.brand_recognized"]
    records, features = [], {}
    for i in range(240):
        label: Label = "junk" if i % 2 else "legit"
        records.append(record(i, label, "train" if i < 180 else "test"))
        unreliable = label == "junk"
        features[f"p{i}"] = {
            "listing.discount_pct": rng.uniform(0.5, 0.9) if unreliable else rng.uniform(0.0, 0.4),
            "content.brand_recognized": float(not unreliable and rng.random() < 0.8),
        }
    train = build([r for r in records if r.split == "train"], features, names)
    test = build([r for r in records if r.split == "test"], features, names)

    model = fit(train, names, "v-test")
    metrics = evaluate(model, test, names)

    assert metrics["roc_auc"] > 0.95
    assert metrics["brier"] < 0.1
    assert model.coefficients[0] > 0 > model.coefficients[1]  # discount → risk, recognised brand → reliable
    assert all(0.0 <= y <= 1.0 for _, y in model.calibration)


def test_failed_skills_train_at_the_mean_as_they_score() -> None:
    names = ["listing.discount_pct", "content.brand_recognized"]
    pattern = [
        {"listing.discount_pct": 0.0, "content.brand_recognized": 1.0},
        {"listing.discount_pct": 0.8},
        {"listing.discount_pct": 0.1, "content.brand_recognized": 1.0},
        {"listing.discount_pct": 0.9, "content.unavailable": 1.0},
    ]
    records = [record(i, "junk" if i % 2 else "legit", "train") for i in range(40)]
    features = {f"p{i}": pattern[i % 4] for i in range(40)}
    data = build(records, features, names)
    assert math.isnan(data.x[3][1]) and data.x[1][1] == 0.0  # failed ≠ ran and found nothing

    model = fit(data, names, "v-test")

    assert model.means[1] == pytest.approx(2 / 3)  # the failed page doesn't count towards the mean
    assert model.contributions({"listing.discount_pct": 0.5, "content.unavailable": 1.0})[1] == 0.0


def test_build_skips_records_without_features() -> None:
    data = build([record(1, "junk", "train"), record(2, "legit", "train")], {"p1": {"x": 1.0}}, ["x"])
    assert [r.id for r in data.records] == ["p1"]
    assert data.x.tolist() == [[1.0]]
    assert data.y.tolist() == [1]


def test_coefficients_keep_the_sign_their_feature_can_mean() -> None:
    # In the corpus legit shops show more countdown timers than scams; a timer must still never make a page safer.
    rng = random.Random(5)
    names = ["listing.discount_pct", "dark_patterns.timer"]
    assert FEATURE_SIGNS["dark_patterns.timer"] == 1
    records = [record(i, "scam" if i % 2 else "legit", "train") for i in range(200)]
    features = {
        r.id: {
            "listing.discount_pct": rng.uniform(0.4, 0.9) if r.label == "scam" else rng.uniform(0.0, 0.5),
            "dark_patterns.timer": float(rng.random() < (0.1 if r.label == "scam" else 0.6)),
        }
        for r in records
    }
    model = fit(build(records, features, names), names, "v-test")
    assert model.coefficients[0] > 0
    assert model.coefficients[1] == 0.0  # pinned at zero rather than turned into a trust signal

    unconstrained = fit(build(records, features, names), names, "v-test", signs={})
    assert unconstrained.coefficients[1] < 0


def test_features_seen_on_too_few_pages_get_no_weight() -> None:
    # Two columns set on the same single scam page (and nowhere else) once got coefficients of +68 and -68.
    rng = random.Random(9)
    names = ["listing.discount_pct", "listing.seller_not_brand", "us_identity.mailbox_only"]
    records = [record(i, "scam" if i % 2 else "legit", "train") for i in range(120)]
    features = {
        r.id: {"listing.discount_pct": rng.uniform(0.3, 0.9) if r.label == "scam" else rng.uniform(0.0, 0.6)}
        for r in records
    }
    features["p1"] |= {"listing.seller_not_brand": 1.0, "us_identity.mailbox_only": 1.0}
    for r in records[2::2][:40]:  # mailbox_only never measured on some pages: NaN, the column mean
        features[r.id]["us_identity.not_applicable"] = 1.0
    model = fit(build(records, features, names), names, "v-test", signs={})
    assert MIN_SUPPORT > 1
    assert model.coefficients[1:] == [0.0, 0.0]
    assert max(abs(c) for c in model.coefficients) < 5


def test_a_rare_feature_that_separates_the_labels_moves_the_odds_a_bounded_step() -> None:
    # A US street address on six legit pages and no scam page once got a step of -280 log-odds.
    rng = random.Random(5)
    names = ["listing.discount_pct", "us_identity.us_address"]
    records = [record(i, "scam" if i % 2 else "legit", "train") for i in range(400)]
    features = {
        r.id: {"listing.discount_pct": rng.uniform(0.3, 0.9) if r.label == "scam" else rng.uniform(0.0, 0.6)}
        for r in records
    }
    for r in [r for r in records if r.label == "legit"][:6]:
        features[r.id]["us_identity.us_address"] = 1.0
    model = fit(build(records, features, names), names, "v-test")
    step = model.coefficients[1] / model.scales[1]  # log-odds per unit of the feature
    assert -MAX_UNIT_LOGIT - 1e-6 <= step < 0


def test_source_leakage_spots_a_feature_that_names_the_source() -> None:
    rng = random.Random(11)
    names = ["listing.discount_pct", "listing.is_shopify", "us_identity.business_entity"]
    records = [
        record(i, "scam" if i // 2 % 2 else "legit", "test" if i % 5 < 2 else "train", source, "storefront")
        for i, source in enumerate(["dataset", "wayback"] * 60 + ["live", "wayback"] * 60)
    ]
    features = {}
    for r in records:
        if r.label == "legit" and r.source == "dataset":
            r.label = "scam"  # dataset is scam-only, live legit-only, as in the corpus
        if r.label == "scam" and r.source == "live":
            r.label = "legit"
        features[r.id] = {
            "listing.discount_pct": rng.uniform(0.3, 0.9) if r.label == "scam" else rng.uniform(0.0, 0.6),
            "listing.is_shopify": float(r.source == "dataset"),  # the source, not the shop
        }
        if r.source == "live":
            features[r.id]["us_identity.not_applicable"] = 1.0
    train = build([r for r in records if r.split == "train"], features, names)
    report = source_leakage(records, features, names, fit(train, names, "v-test"))
    by_source = {(row["label"], row["source"].split(" ")[0]): row for row in report["source"]}
    assert by_source[("scam", "dataset")]["auc"] > 0.95
    assert by_source[("scam", "dataset")]["top"][0][0] == "listing.is_shopify"
    assert report["applicability"]["us_identity"] > 0.6  # applies to every scam but not to the live legit shops
    assert "wayback" in report["within_source"]
    # Knowing only the source says a lot (dataset is all scam, live all legit), the origin dataset the same here.
    assert report["grouping"]["source"] > 0.7
    assert set(report["grouping"]) == {"source", "origin_dataset"}
