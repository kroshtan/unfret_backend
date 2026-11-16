"""The new page classes' label collection: collectors' parsing (offline fixtures), strict reconcile, split, reports."""

import json
import random
from pathlib import Path

from evals.corpus import Corpus, LabelRecord, is_product, page_class
from evals.metrics import Outcome
from evals.run import per_class, per_class_markdown
from evals.second_opinion import class_report, reconcile, reconcile_strict, reset
from evals.sources import acm_traders, osm_trades, p2p_ads, patterns, service_pages
from evals.split import assign_splits
from unfret.facts import parse_page
from unfret.facts.vat_ids import check_digits_ok
from unfret.graph.platforms import platform_rule, service_site_signals
from unfret.html_cleaner import clean_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "labels"
_ALL_SCAM_P2P = list(p2p_ads._SCAM_PATTERNS)
_ALL_SCAM_SERVICE = list(service_pages._SCAM_PATTERNS)


def _record(**overrides: object) -> LabelRecord:
    base: dict[str, object] = {"id": "x-1", "source": "synthetic", "origin_dataset": "ds"}
    return LabelRecord.model_validate({**base, **overrides})


# ── patterns ────────────────────────────────────────────────────────────────


def test_warning_page_confirms_only_the_patterns_it_documents() -> None:
    text = clean_html((FIXTURES / "warning-page.html").read_text())
    found = patterns.confirmed_by(text, patterns.PATTERNS, patterns.MP_PAYMENT_FRAUD)
    assert found == ["off_platform_contact", "seller_payment_link", "platform_payment"]
    # The same text under an address no pattern cites confirms nothing.
    assert patterns.confirmed_by(text, patterns.PATTERNS, "https://example.org/") == []


def test_every_pattern_cites_a_source_and_has_evidence() -> None:
    for p in patterns.PATTERNS:
        assert p.sources and p.evidence, p.key
        assert all(s.startswith("https://") for s in p.sources)


def test_confirmed_patterns_reads_the_source_check(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    path = patterns.sources_path(corpus)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "https://a.example/": {"confirmed": ["off_platform_contact", "fake_local"]},
                "https://b.example/": {"confirmed": ["off_platform_contact"], "error": ""},
                "https://c.example/": {"confirmed": [], "error": "robots"},
            }
        )
    )
    assert patterns.confirmed_patterns(corpus, "p2p") == {
        "off_platform_contact": ["https://a.example/", "https://b.example/"]
    }
    assert patterns.confirmed_patterns(corpus, "service") == {"fake_local": ["https://a.example/"]}


# ── synthetic peer-to-peer ads ──────────────────────────────────────────────


def test_p2p_ads_are_parsed_as_platform_listings_without_a_seller_name() -> None:
    rng = random.Random(3)
    for label in ("scam", "legit") * 20:
        ad = p2p_ads.make_ad(rng, label, _ALL_SCAM_P2P)
        page = p2p_ads.render(ad)
        facts, text = parse_page(page, "")
        assert facts.platform == ad.platform.key
        assert facts.price == ad.price
        assert facts.seller == ""
        rule = platform_rule(facts, ad.url, text)
        assert rule is not None and rule[0] == "p2p_listing", ad.url
        if label == "scam":
            assert 2 <= len(ad.patterns) <= 4
            assert set(ad.patterns) <= set(_ALL_SCAM_P2P)
        else:
            assert ad.patterns == ["meet_in_person", "platform_payment", "seller_history"]


def test_p2p_scam_ads_use_only_confirmed_patterns_and_rentals_get_the_rental_pattern() -> None:
    rng = random.Random(9)
    allowed = ["off_platform_contact", "untraceable_prepayment", "pressure", "rental_no_viewing"]
    for _ in range(80):
        ad = p2p_ads.make_ad(rng, "scam", allowed)
        assert set(ad.patterns) <= set(allowed)
        if ad.item.rental:
            assert "rental_no_viewing" in ad.patterns
        else:
            assert "rental_no_viewing" not in ad.patterns


def test_p2p_record_is_a_marked_synthetic_rule_label() -> None:
    ad = p2p_ads.make_ad(random.Random(1), "scam", _ALL_SCAM_P2P)
    sources = {k: [f"https://src.example/{k}"] for k in ad.patterns}
    record = p2p_ads.record_for(ad, sources)
    notes = json.loads(record.notes)
    assert record.page_type == "p2p_listing" and page_class(record) == "p2p_listing"
    assert record.source == "synthetic" and notes["synthetic"] is True
    assert record.label == record.prior_label == "scam" and record.label_source == "rule"
    assert record.rule_features == ad.patterns
    assert notes["sources"] == sorted(sources[k][0] for k in ad.patterns)


def test_p2p_build_needs_confirmed_sources(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    path = patterns.sources_path(corpus)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"https://a.example/": {"confirmed": ["off_platform_contact"]}}))
    try:
        p2p_ads.build(corpus, 2, 1)
    except SystemExit as exc:
        assert "patterns" in str(exc)
    else:
        raise AssertionError("expected SystemExit")
    path.write_text(json.dumps({"https://a.example/": {"confirmed": _ALL_SCAM_P2P}}))
    assert p2p_ads.build(corpus, 3, 1) == 6
    labels = corpus.labels()
    assert sorted(str(r.label) for r in labels.values()) == ["legit"] * 3 + ["scam"] * 3


# ── synthetic service pages ─────────────────────────────────────────────────


def test_service_sites_read_as_service_sites_and_scam_sites_are_doorways() -> None:
    rng = random.Random(4)
    for label in ("scam", "legit") * 15:
        market = rng.choice(("nl", "us"))
        page = service_pages._site(rng, market, rng.choice(service_pages.TRADES), label, _ALL_SCAM_SERVICE)
        facts, text = parse_page(service_pages.render(page), page.url)
        assert service_site_signals(facts)
        if label == "scam":
            assert "fake_local" in page.patterns
            assert text.count(page.trade.nl if market == "nl" else page.trade.en.title()) > 10  # city pages
        else:
            assert ("KvK" in text) if market == "nl" else ("Licensed" in text)


def test_legit_dutch_service_sites_show_vat_numbers_with_valid_check_digits() -> None:
    rng = random.Random(13)
    shown = []
    for _ in range(60):
        page = service_pages._site(rng, "nl", rng.choice(service_pages.TRADES), "legit", _ALL_SCAM_SERVICE)
        facts, _ = parse_page(service_pages.render(page), page.url)
        shown += facts.vat_ids
    assert len(shown) >= 50
    assert all(check_digits_ok(v) for v in shown), [v for v in shown if not check_digits_ok(v)]


def test_service_listings_sit_on_their_platform() -> None:
    rng = random.Random(6)
    for label in ("scam", "legit") * 10:
        page = service_pages._listing(rng, rng.choice(("nl", "us")), service_pages.TRADES[0], label, _ALL_SCAM_SERVICE)
        facts, text = parse_page(service_pages.render(page), page.url)
        rule = platform_rule(facts, page.url, text)
        assert rule is not None and rule[0] == "service_listing", page.url
        record = service_pages.record_for(page, {})
        assert record.page_type == "service_listing" and json.loads(record.notes)["synthetic"] is True


# ── real pages: ACM decisions and OpenStreetMap ─────────────────────────────


def test_acm_pick_pages_takes_home_first_and_skips_policies_assets_and_queries() -> None:
    rows = [
        ["20200101000000", "https://www.slotenmakergerard.nl/slotenmaker-breda/"],
        ["20200101000001", "https://www.slotenmakergerard.nl/privacy-policy/"],
        ["20200101000002", "https://www.slotenmakergerard.nl/cdn-cgi/l/email-protection"],
        ["20200101000003", "https://www.slotenmakergerard.nl/?p=12"],
        ["20200101000004", "https://www.slotenmakergerard.nl/wp-content/logo.png"],
        ["20200101000005", "https://www.slotenmakergerard.nl"],
        ["20200101000006", "https://slotenmakergerard.nl/"],
        ["20200101000007", "https://www.slotenmakergerard.nl/contact/"],
        ["20200101000008", "https://www.slotenmakergerard.nl/blog/slotenmaker-noodgeval/"],
    ]
    picked = acm_traders.pick_pages(rows, 3)
    assert picked == [
        ("20200101000005", "https://www.slotenmakergerard.nl"),
        ("20200101000007", "https://www.slotenmakergerard.nl/contact/"),
        ("20200101000000", "https://www.slotenmakergerard.nl/slotenmaker-breda/"),
    ]
    assert len(acm_traders.pick_pages(rows, 10)) == 4


def test_acm_trade_page_check_rejects_parked_and_empty_pages() -> None:
    assert acm_traders.reads_as_trade_site("Slotenmaker Breda, 24 uur per dag bereikbaar. " * 20)
    assert not acm_traders.reads_as_trade_site("This domain is for sale. " * 40)
    assert not acm_traders.reads_as_trade_site("Slotenmaker")


def test_acm_sites_all_name_a_known_decision() -> None:
    domains = [d for d, _ in acm_traders.SITES]
    assert len(domains) == len(set(domains))
    assert {k for _, k in acm_traders.SITES} == set(acm_traders.DECISIONS)
    assert "slotenmaker-strijbos.nl" not in domains


def test_overpass_parsing_keeps_one_own_website_per_domain() -> None:
    data = json.loads((FIXTURES / "overpass-trades.json").read_text())
    trades = osm_trades.trades_from_overpass(data, "NL")
    assert [(t.domain, t.craft, t.osm_id) for t in trades] == [
        ("voorbeeld-installatie.nl", "plumber", "node/101"),
        ("sleutelsample.nl", "locksmith", "node/103"),
        ("sampleheating.com", "hvac", "relation/107"),
    ]
    assert all(t.country == "NL" for t in trades)


def test_domain_of_normalises_website_tags() -> None:
    assert osm_trades.domain_of("https://www.Example.nl/contact") == "example.nl"
    assert osm_trades.domain_of("example.com") == "example.com"
    assert osm_trades.domain_of("not a url") == ""
    assert "craft" in osm_trades.overpass_query('area["ISO3166-1"="NL"]')


# ── second opinion: new classes keep only agreements ────────────────────────


def _new(**overrides: object) -> LabelRecord:
    base: dict[str, object] = {
        "page_type": "p2p_listing",
        "label": "scam",
        "prior_label": "scam",
        "label_source": "rule",
        "confidence": 0.7,
        "notes": '{"synthetic": true}',
        "llm_reason": "WhatsApp only and payment up front.",
    }
    return _record(**{**base, **overrides})


def test_strict_reconcile_keeps_confident_agreement() -> None:
    record, item = reconcile(_new(llm_label="scam", llm_confidence=0.9))
    assert item is None
    assert record.label == "scam" and record.label_source == "agreed" and record.confidence == 0.9


def test_strict_reconcile_drops_disagreement_and_unsure_without_queueing() -> None:
    for llm_label, confidence in (("legit", 0.9), (None, 0.4), ("scam", 0.5)):
        record, item = reconcile(_new(llm_label=llm_label, llm_confidence=confidence, split="train"))
        assert item is None
        assert record.label is None and record.label_source is None and record.split is None
        assert record.notes.startswith("dropped: ")
        assert record.notes.endswith('| {"synthetic": true}')
        assert reconcile_strict(record) == record  # dropping twice changes nothing


def test_reset_restores_a_dropped_new_class_label() -> None:
    dropped, _ = reconcile(_new(llm_label="legit", llm_confidence=0.9))
    restored = reset(dropped)
    assert restored.label == "scam" and restored.label_source == "rule"
    assert restored.notes == '{"synthetic": true}' and restored.llm_label is None
    real, _ = reconcile(_new(source="wayback", label_source="dataset", llm_label="legit", llm_confidence=0.9))
    assert reset(real).label_source == "dataset"


def test_product_reconcile_is_unchanged_for_product_pages() -> None:
    record = _record(
        page_type="storefront",
        label="junk",
        prior_label="junk",
        label_source="rule",
        llm_label="legit",
        llm_confidence=0.9,
        llm_reason="Known brand.",
    )
    updated, item = reconcile(record)
    assert item is not None and updated.notes.startswith("withheld:")


def test_class_report_counts_kept_dropped_and_reasons() -> None:
    kept, _ = reconcile(_new(id="a", llm_label="scam", llm_confidence=0.9))
    dropped, _ = reconcile(_new(id="b", llm_label="legit", llm_confidence=0.8, llm_reason="Normal ad."))
    waiting = _new(id="c", llm_reason="")
    report = class_report([kept, dropped, waiting], 2)
    assert report["kept"] == {"scam/synthetic/ds": 1}
    assert report["dropped"] == {"scam/synthetic/ds": 1}
    assert report["without_opinion"] == {"scam/synthetic/ds": 1}
    assert report["opinions_requested_this_run"] == 2
    assert report["dropped_reasons"] == [{"id": "b", "reason": "scam (ds) vs LLM legit (0.80): Normal ad."}]


# ── corpus classes, split and per-class reports ─────────────────────────────


def test_page_class_groups_product_pages() -> None:
    assert page_class(_record(page_type="storefront")) == "product"
    assert page_class(_record(page_type="")) == "product"
    assert page_class(_record(page_type="aggregate_listing")) == "product"
    assert page_class(_record(page_type="service_site")) == "service_site"
    assert is_product(_record(page_type="storefront")) and not is_product(_record(page_type="p2p_listing"))


def test_adding_new_class_pages_never_moves_a_product_page() -> None:
    products = [
        _record(id=f"amz-{i}", page_type="aggregate_listing", label="junk", label_source="agreed") for i in range(40)
    ]
    newcomers = [
        _record(id=f"p2p-{i}", page_type="p2p_listing", label="scam", label_source="agreed") for i in range(40)
    ]
    alone = {r.id: r.split for r in assign_splits(products, 0.3, "seed")}
    mixed = {r.id: r.split for r in assign_splits(products + newcomers, 0.3, "seed")}
    assert {k: v for k, v in mixed.items() if k.startswith("amz-")} == alone
    assert sum(1 for k, v in mixed.items() if k.startswith("p2p-") and v == "test") == 12


def test_per_class_metrics_and_markdown() -> None:
    outcomes = [
        Outcome(id="a", label="scam", score=10, expected_page_type="p2p_listing"),
        Outcome(id="b", label="legit", score=80, expected_page_type="p2p_listing"),
        Outcome(id="c", label="scam", score=60, expected_page_type="service_site"),
    ]
    summaries = per_class(outcomes, {"a": "p2p_listing", "b": "p2p_listing", "c": "service_site"})
    assert set(summaries) == {"p2p_listing", "service_site"}
    assert summaries["p2p_listing"]["roc_auc"] == 1.0 and summaries["service_site"]["pages"] == 1
    markdown = per_class_markdown(summaries)
    assert "## Class p2p_listing" in markdown and "### Score per label" in markdown
