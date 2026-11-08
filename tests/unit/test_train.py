"""The scorer training code, on synthetic data (the corpus itself stays private)."""

import random

from evals.corpus import Label, LabelRecord, Split
from evals.train import RULE_MIRRORS, build, evaluate, feature_names, fit


def record(i: int, label: Label, split: Split) -> LabelRecord:
    return LabelRecord(
        id=f"p{i}",
        label=label,
        label_source="agreed",
        confidence=1.0,
        platform="Amazon",
        page_type="aggregate_listing" if i % 2 else "storefront",
        source="synthetic",
        origin_dataset="test",
        origin_url="",
        origin_id=str(i),
        collected_at="2026",
        split=split,
    )


def test_feature_names_cover_offline_skills_only() -> None:
    names = feature_names()
    assert "listing.discount_pct" in names
    assert not any(n.startswith(("reverse_image.", "domain_age.")) for n in names)
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


def test_build_skips_records_without_features() -> None:
    data = build([record(1, "junk", "train"), record(2, "legit", "train")], {"p1": {"x": 1.0}}, ["x"])
    assert [r.id for r in data.records] == ["p1"]
    assert data.x.tolist() == [[1.0]]
    assert data.y.tolist() == [1]
