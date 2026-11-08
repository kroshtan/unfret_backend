from collections import Counter
from pathlib import Path

import pytest
from langchain_core.outputs import Generation

from evals.corpus import Corpus, LabelRecord, QueueItem, page_id
from evals.features import (
    DAY_MS,
    duplicate_share,
    early_review_burst,
    generated_brand_score,
    keyword_stuffed,
    rating_shape,
)
from evals.label import apply_decision, describe
from evals.llm_cache import CacheMissError, SQLiteLLMCache
from evals.metrics import Outcome, binary_metrics, page_type_confusion, roc_auc, summarise, to_markdown
from evals.run import select
from evals.second_opinion import reconcile, reset
from evals.shopcheck import looks_like_shop
from evals.sources.amazon2023 import Product, assess, render
from evals.sources.mendeley import read_rows


def _record(**overrides: object) -> LabelRecord:
    base: dict[str, object] = {"id": "x-1", "source": "dataset", "origin_dataset": "ds"}
    return LabelRecord.model_validate({**base, **overrides})


# ── features ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("brand", "expected_min", "expected_max"),
    [("ZHUIBO", 0.6, 1.0), ("XIEERDUO", 0.6, 1.0), ("Neutrogena", 0.0, 0.0), ("Yes To", 0.0, 0.0), ("", 0.0, 0.0)],
)
def test_generated_brand_score(brand: str, expected_min: float, expected_max: float) -> None:
    score = generated_brand_score(brand, frozenset({"yes", "to"}), frozenset({"neutrogena"}))
    assert expected_min <= score <= expected_max


def test_duplicate_share_ignores_short_reviews() -> None:
    texts = [
        "Great!",
        "Great!",
        "This charger stopped working after two days.",
        "this charger STOPPED working after two days",
    ]
    assert duplicate_share(texts) == 1.0
    assert duplicate_share(["ok", "good"]) == 0.0


def test_rating_shape_detects_polarised_ratings() -> None:
    assert rating_shape([20, 2, 3, 5, 70]).j_shaped
    assert not rating_shape([2, 3, 10, 30, 55]).j_shaped
    assert rating_shape([0, 0, 0, 0, 0]).five == 0.0


def test_early_review_burst_counts_first_window() -> None:
    start = 1_600_000_000_000
    assert early_review_burst([start, start + DAY_MS, start + 400 * DAY_MS], window_days=180) == 2
    assert early_review_burst([]) == 0


def test_keyword_stuffed() -> None:
    assert keyword_stuffed(
        "Wireless Earbuds, Bluetooth 5.4 Headphones, 120H Playtime, Deep Bass, IPX7 Waterproof, " * 2
    )
    assert not keyword_stuffed("Neutrogena Hydro Boost Water Gel, 1.7 oz")


# ── amazon rules and rendering ──────────────────────────────────────────────


def _product(**overrides: object) -> Product:
    base: dict[str, object] = {
        "asin": "B0TEST",
        "category": "Amazon_Fashion",
        "title": "Plain T-shirt",
        "store": "Hanes",
        "price": 12.0,
        "average_rating": 4.5,
        "rating_number": 120,
        "features": ["Cotton"],
        "description": ["Soft."],
        "details": {"Date First Available": "March 1, 2019"},
        "image": "https://m.media-amazon.com/images/I/x.jpg",
    }
    return Product(**{**base, **overrides})  # type: ignore[arg-type]


def test_assess_labels_only_confident_extremes() -> None:
    legit = _product(histogram=[2, 3, 10, 30, 55], texts=["Fits well and washes nicely."] * 1)
    assert assess(legit, Counter(), frozenset()).label == "legit"

    junk = _product(
        store="ZHUIBO",
        title="Women Summer Dress, Casual Boho Floral, V Neck Sundress, Beach Party Maxi Dress, Plus Size, " * 2,
        histogram=[20, 2, 3, 5, 70],
        texts=["Beautiful dress, fits perfectly and love it."] * 10,
    )
    assert assess(junk, Counter(), frozenset()).label == "junk"

    unknown = _product(store="Some Small Label", histogram=[5, 5, 20, 30, 40])
    assert assess(unknown, Counter(), frozenset()).label is None


def test_render_contains_shopper_facts() -> None:
    page = render(
        _product(
            histogram=[1, 1, 1, 1, 6],
            sample=[
                {"rating": 5, "title": "Nice", "text": "Good <b>", "timestamp": 1_600_000_000_000, "verified": True}
            ],
        )
    )
    assert "Visit the Hanes Store" in page
    assert "Date First Available" in page
    assert "60%" in page
    assert "Good &lt;b&gt;" in page
    assert '"@type": "Product"' in page


# ── shop check, mendeley ────────────────────────────────────────────────────


def test_looks_like_shop() -> None:
    shop = "<html><body>" + "<p>Lovely handmade candles for every room in your home.</p>" * 5
    shop += "<span>$19.99</span><span>$24.99</span><button>Add to cart</button></body></html>"
    assert looks_like_shop(shop)
    assert not looks_like_shop("<html><body><p>Donate to our cause today.</p></body></html>")


def test_mendeley_read_rows(tmp_path: Path) -> None:
    csv = tmp_path / "shops.csv"
    csv.write_text(
        "Online shop URL,Label,Domain length\nhttps://a.shop,fraudulent,6\nhttps://b.com,legitimate,5\nhttps://c.com,other,5\n"
    )
    assert read_rows(csv) == [("https://a.shop", "scam"), ("https://b.com", "legit")]


# ── corpus ──────────────────────────────────────────────────────────────────


def test_corpus_roundtrip_and_update(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    rid = page_id("t", "https://example.test")
    corpus.add_many([(_record(id=rid, label="legit", label_source="dataset"), "<html>hi</html>")])
    assert corpus.read_page(rid) == "<html>hi</html>"
    corpus.update([corpus.labels()[rid].model_copy(update={"label": "scam"})])
    assert corpus.labels()[rid].label == "scam"
    corpus.write_queue([QueueItem(id=rid, reason="r", priority=0.1), QueueItem(id="other", reason="r", priority=0.9)])
    assert [i.id for i in corpus.queue()] == ["other", rid]


# ── second opinion ──────────────────────────────────────────────────────────


def test_reconcile_agreement_upgrades_label() -> None:
    record = _record(
        label="scam", label_source="dataset", prior_label="scam", llm_label="junk", llm_confidence=0.9, llm_reason="r"
    )
    updated, item = reconcile(record)
    assert (updated.label, updated.label_source, item) == ("scam", "agreed", None)


def test_reconcile_rule_disagreement_is_withheld_and_queued() -> None:
    record = _record(
        source="synthetic",
        label="junk",
        label_source="rule",
        prior_label="junk",
        llm_label="legit",
        llm_confidence=0.9,
        llm_reason="r",
    )
    updated, item = reconcile(record)
    assert updated.label is None
    assert item is not None and item.reason.startswith("disagreement")


def test_reconcile_dataset_disagreement_keeps_dataset_label() -> None:
    record = _record(
        label="scam",
        label_source="dataset",
        prior_label="scam",
        llm_label="legit",
        llm_confidence=0.95,
        llm_reason="r",
    )
    updated, item = reconcile(record)
    assert (updated.label, updated.label_source) == ("scam", "dataset")
    assert item is not None


def test_reconcile_unsure_or_unconfident_changes_nothing() -> None:
    record = _record(
        label="legit", label_source="rule", prior_label="legit", llm_label="junk", llm_confidence=0.4, llm_reason="r"
    )
    assert reconcile(record) == (record, None)


def test_reconcile_unlabelled_confident_is_queued_and_human_is_final() -> None:
    _, item = reconcile(_record(llm_label="junk", llm_confidence=0.9, llm_reason="r"))
    assert item is not None and item.reason.startswith("unlabelled")
    human = _record(
        label="legit", label_source="human", prior_label="junk", llm_label="junk", llm_confidence=0.9, llm_reason="r"
    )
    assert reconcile(human) == (human, None)


# ── label CLI ───────────────────────────────────────────────────────────────


def test_label_describe_and_apply() -> None:
    record = _record(prior_label="junk", llm_label="legit", llm_confidence=0.8, notes='{"duplicate_share": 0.2}')
    screen = describe(QueueItem(id="x-1", reason="disagreement: junk vs legit", priority=1), record, "word " * 500)
    assert "disagreement" in screen and "rule features" in screen and "[j]unk" in screen
    assert apply_decision(record, "junk").label_source == "human"


# ── metrics ─────────────────────────────────────────────────────────────────

OUTCOMES = [
    Outcome("a", "scam", 10, "storefront", "storefront", input_tokens=100),
    Outcome("b", "junk", 35, "aggregate_listing", "storefront"),
    Outcome("c", "legit", 80, "storefront", "storefront"),
    Outcome("d", "legit", 45, "storefront", "storefront"),
    Outcome("e", "legit", -1, "storefront", "non_product"),
    Outcome("f", "scam", None, "storefront", error="boom"),
]


def test_binary_metrics() -> None:
    m = binary_metrics(OUTCOMES, 40)
    assert (m.tp, m.fp, m.fn, m.tn) == (2, 0, 0, 2)
    m50 = binary_metrics(OUTCOMES, 50)
    assert (m50.tp, m50.fp) == (2, 1)
    assert m50.precision == pytest.approx(2 / 3)
    assert m50.recall == 1.0


def test_roc_auc() -> None:
    assert roc_auc(OUTCOMES) == 1.0
    assert roc_auc([Outcome("a", "scam", 50), Outcome("b", "legit", 50)]) == 0.5
    assert roc_auc([Outcome("a", "legit", 50)]) is None


def test_page_type_confusion_and_summary() -> None:
    assert page_type_confusion(OUTCOMES)["aggregate_listing"] == {"storefront": 1}
    report = summarise(OUTCOMES)
    assert (report["pages"], report["scored"], report["not_analysed"]) == (6, 4, 1)
    assert report["failures"] == [{"id": "f", "error": "boom"}]
    assert "| <40 |" in to_markdown(report)


def test_select_is_balanced_and_stable() -> None:
    records = [_record(id=f"s{i}", label="scam") for i in range(10)] + [
        _record(id=f"l{i}", label="legit") for i in range(3)
    ]
    picked = select(records, 6)
    assert len(picked) == 6
    assert sum(r.label == "legit" for r in picked) == 3
    assert [r.id for r in picked] == [r.id for r in select(records, 6)]


# ── llm cache ───────────────────────────────────────────────────────────────


def test_sqlite_llm_cache_roundtrip(tmp_path: Path) -> None:
    cache = SQLiteLLMCache(tmp_path / "c.sqlite")
    assert cache.lookup("p", "m") is None
    cache.update("p", "m", [Generation(text="hello")])
    hit = cache.lookup("p", "m")
    assert hit is not None and hit[0].text == "hello"
    assert cache.size() == 1
    cache.clear()
    assert cache.size() == 0


def test_reset_restores_prior_label_and_keeps_human() -> None:
    withheld = _record(
        source="synthetic",
        label=None,
        label_source=None,
        prior_label="junk",
        llm_label="legit",
        llm_confidence=0.9,
        llm_reason="r",
        notes="withheld: dispute | {}",
    )
    restored = reset(withheld)
    assert (restored.label, restored.label_source, restored.llm_reason, restored.notes) == ("junk", "rule", "", "{}")
    human = _record(label="legit", label_source="human", prior_label="junk", llm_reason="r")
    assert reset(human).label == "legit"


def test_strict_cache_raises_on_miss(tmp_path: Path) -> None:
    cache = SQLiteLLMCache(tmp_path / "c.sqlite", strict=True)
    with pytest.raises(CacheMissError):
        cache.lookup("p", "m")
