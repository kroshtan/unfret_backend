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
from evals.second_opinion import STRICT_DATASETS, reconcile, reset
from evals.shopcheck import looks_like_shop
from evals.sources.amazon2023 import (
    JUNK_RULE_FEATURES,
    Product,
    assess,
    backfill,
    features_from_notes,
    render,
    rule_features_for,
)
from evals.sources.coi_shops import read_list, warning_dates, window
from evals.sources.mendeley import read_rows
from evals.sources.us_shops import (
    is_product_page,
    old_enough,
    page_links,
    platform_of,
    product_path,
    seeds_from_overpass,
    seeds_from_wikidata,
)
from evals.split import assign_splits
from unfret.facts import parse_page


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


# ── split and training support ──────────────────────────────────────────────


def test_rule_labels_never_enter_test_and_split_is_stratified() -> None:
    records = (
        [_record(id=f"d{i}", label="scam", label_source="dataset") for i in range(10)]
        + [_record(id=f"a{i}", source="synthetic", label="junk", label_source="agreed") for i in range(10)]
        + [_record(id=f"r{i}", source="synthetic", label="junk", label_source="rule") for i in range(10)]
        + [_record(id="u", source="synthetic")]
    )
    split = {r.id: r.split for r in assign_splits(records, 0.3, "seed")}
    assert all(split[f"r{i}"] == "train" for i in range(10))
    assert sum(split[f"d{i}"] == "test" for i in range(10)) == 3
    assert sum(split[f"a{i}"] == "test" for i in range(10)) == 3
    assert split["u"] is None
    assert split == {r.id: r.split for r in assign_splits(list(reversed(records)), 0.3, "seed")}
    assert split != {r.id: r.split for r in assign_splits(records, 0.3, "other-seed")}


def test_rule_features_and_feature_backfill_from_notes(tmp_path: Path) -> None:
    assert "generated_brand_score" in rule_features_for("junk")
    assert "known_brand" in rule_features_for("legit")
    assert rule_features_for(None) == []
    assert features_from_notes('withheld: dispute | {"price": 9.99}') == {"price": 9.99}
    assert features_from_notes("not json") is None

    corpus = Corpus(tmp_path)
    record = _record(
        id="amz-1", source="synthetic", origin_dataset="amazon-reviews-2023", prior_label="junk", notes='{"price": 5}'
    )
    corpus.add_many([(record, "<html></html>")])
    assert backfill(corpus) == 1
    assert corpus.features() == {"amz-1": {"price": 5}}
    assert corpus.labels()["amz-1"].rule_features == list(JUNK_RULE_FEATURES)


def test_new_dataset_gets_its_own_stratum_and_moves_no_existing_page() -> None:
    old = [_record(id=f"m{i}", source="wayback", label="legit", label_source="agreed") for i in range(20)]
    before = {r.id: r.split for r in assign_splits(old, 0.3, "seed")}
    new = [
        _record(id=f"n{i}", source="wayback", origin_dataset="new", label="legit", label_source="agreed")
        for i in range(10)
    ]
    after = {r.id: r.split for r in assign_splits(old + new, 0.3, "seed")}
    assert all(after[k] == v for k, v in before.items())
    assert sum(after[f"n{i}"] == "test" for i in range(10)) == 3


# ── balancing sources ───────────────────────────────────────────────────────


def test_strict_datasets_drop_disagreements() -> None:
    dataset = next(iter(sorted(STRICT_DATASETS)))
    base = _record(
        source="wayback", origin_dataset=dataset, label="legit", label_source="dataset", prior_label="legit"
    )
    agreed, _ = reconcile(base.model_copy(update={"llm_label": "legit", "llm_confidence": 0.9, "llm_reason": "ok"}))
    assert agreed.label == "legit" and agreed.label_source == "agreed"
    for label, confidence in (("scam", 0.9), ("legit", 0.5), (None, 0.0)):
        record = base.model_copy(update={"llm_label": label, "llm_confidence": confidence, "llm_reason": "r"})
        dropped, item = reconcile(record)
        assert dropped.label is None and dropped.notes.startswith("dropped: ") and item is None


def test_us_shop_seeds_from_wikidata_and_osm() -> None:
    wd: dict[str, object] = {
        "results": {
            "bindings": [
                {
                    "c": {"value": "http://www.wikidata.org/entity/Q1"},
                    "site": {"value": "https://www.Shop.com/"},
                    "inc": {"value": "1990-01-01T00:00:00Z"},
                    "ind": {"value": "http://www.wikidata.org/entity/Q126793"},
                },
                {
                    "c": {"value": "http://www.wikidata.org/entity/Q1"},
                    "site": {"value": "https://shop.com"},
                    "inc": {"value": "1985-01-01T00:00:00Z"},
                },
                {"c": {"value": "http://www.wikidata.org/entity/Q2"}, "site": {"value": "https://facebook.com/x"}},
            ]
        }
    }
    seeds = seeds_from_wikidata(wd)
    assert [(s.domain, s.ref, s.founded) for s in seeds] == [("shop.com", "Q1", "1985")]
    osm: dict[str, object] = {
        "elements": [
            {"type": "node", "id": 5, "tags": {"shop": "bicycle", "website": "bikes.example"}},
            {"type": "node", "id": 6, "tags": {"shop": "supermarket", "website": "food.example"}},
            {"type": "node", "id": 7, "tags": {"shop": "books", "website": "https://www.etsy.com/shop/x"}},
        ]
    }
    assert [(s.domain, s.ref, s.segment) for s in seeds_from_overpass(osm)] == [("bikes.example", "node/5", "bicycle")]


def test_page_links_find_single_product_pages_on_the_same_site() -> None:
    html = """<a href="/products/blue-mug">mug</a> <a href="/collections/mugs/products/red-mug?variant=2">red</a>
    <a href="https://www.shop.com/product/green-mug/">green</a> <a href="/products/blue-mug.json">json</a>
    <a href="/products/">all</a> <a href="/products/a/b">deep</a> <a href="https://other.com/products/x">elsewhere</a>
    <a href="/collections/mugs">mugs</a> <a href="mailto:hi@shop.com">mail</a>"""
    products, categories = page_links(html, "https://shop.com/")
    assert products == [
        "https://shop.com/products/blue-mug",
        "https://shop.com/products/red-mug",
        "https://www.shop.com/product/green-mug/",
    ]
    assert categories == ["https://shop.com/collections/mugs"]
    assert product_path("/p/widget-9") == "/p/widget-9" and product_path("/about") is None


def test_product_page_platform_and_age() -> None:
    html = (
        '<html><head><meta property="og:type" content="product"><title>Blue Mug</title>'
        '<script type="application/ld+json">{"@type": "Product", "name": "Blue Mug", "offers": '
        '{"@type": "Offer", "price": "18.00", "priceCurrency": "USD"}}</script>'
        '<link href="https://cdn.shopify.com/s/files/theme.css"></head><body><h1>Blue Mug</h1>'
        + "<p>A stoneware mug thrown by hand in our Vermont studio, glazed in deep blue.</p>" * 4
        + "<span>$18.00</span><button>Add to cart</button></body></html>"
    )
    facts, text = parse_page(html, "https://shop.com/products/blue-mug")
    assert is_product_page(html, facts, text)
    assert platform_of(html) == "shopify" and platform_of("<html></html>") == "custom"
    assert old_enough("20150301", "20230101", "") and old_enough("20150301", "20230101", "1990")
    assert not old_enough("20190101", "20230101", "")
    assert not old_enough("20150101", "20230101", "2020")


def test_coi_list_and_warning_dates() -> None:
    raw = (
        "fake-shop.cz;'Jako provozovatel webu není uveden nikdo, stránky jsou tedy zcela anonymní.'\r\n"
        "other.com;'Internetový obchod sice kontaktní údaj uvádí, nicméně se jej nepodařilo ověřit.'\r\n"
        "not a domain;'x'\r\n"
    ).encode("cp1250")
    assert read_list(raw) == {"fake-shop.cz": "anonymous operator", "other.com": "contact details not verifiable"}
    page = "<html><body><div>fake-shop.cz</div><p>Anonymní.</p><p>(10. 11. 2026)</p><div>other.com</div>"
    page += "<p>(03. 02. 2024)</p></body></html>"
    assert warning_dates(page) == {"fake-shop.cz": "20261110", "other.com": "20240203"}
    assert window("20240203") == ("20230203", "20240304")
