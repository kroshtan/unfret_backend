import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration

from evals import candidates as cand
from evals.corpus import Corpus, Label, LabelRecord
from evals.sources.deep_candidates import DATASET, category_of, rank


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/products/120-colors-dual-tip-alcohol-markers", "art_markers"),
        ("/nl/nl/p/powerbank-30000mah-met-kabels/123/", "power_banks"),
        ("65W GaN Charger 3-Port", "chargers"),
        ("Solar LED string lights 100ft", "led_lights"),
        ("/products/blue-cotton-shirt", "other"),
    ],
)
def test_category_of(text: str, expected: str) -> None:
    assert category_of(text) == expected


def test_rank_puts_stated_counts_and_categories_first() -> None:
    urls = [
        "https://shop.example/products/blue-cotton-shirt",
        "https://shop.example/products/led-lamp",
        "https://shop.example/products/52-pcs-marker-set",
    ]
    assert rank(urls) == [urls[2], urls[1], urls[0]]


def test_fired_reads_findings_and_features() -> None:
    findings = [
        {"skill": "claim_audit", "type": "claim_contradicted", "evidence": "x"},
        {"skill": "pricing", "type": "t", "evidence": "y"},
    ]
    features = {"photo_claims.count_mismatch": 1.0, "photo_claims.checked": 1.0, "pricing.discount": 1.0}
    assert cand.fired(features, findings) == ["claim_audit", "photo_claims"]
    assert cand.fired({"photo_claims.count_mismatch": 0.0}, []) == []


def test_cost_uses_each_models_price() -> None:
    usage = {cand.settings.claim_audit_model: {"input_tokens": 1_000_000, "output_tokens": 0}}
    assert cand.cost(usage) == cand.settings.claim_audit_input_eur_per_mtok
    assert cand.cost({}) == Decimal(0)


def _record(i: int, label: Label | None = None) -> LabelRecord:
    return LabelRecord(
        id=f"dfc-{i:03d}",
        label=label,
        source="live",
        origin_dataset=DATASET,
        origin_url=f"https://s{i}.example/p",
        notes=json.dumps({"pool": "search"}),
    )


def test_queue_order_and_finding_labels(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    corpus.add_many((_record(i, "legit" if i == 4 else None), "<html></html>") for i in range(6))
    finding = {"skill": "claim_audit", "type": "claim_contradicted", "evidence": "e"}
    rows: dict[str, dict[str, Any]] = {
        "dfc-000": {"score": 50, "confidence": 0.1, "findings": [finding]},
        "dfc-001": {"score": 10, "confidence": 0.9, "features": {"photo_claims.count_mismatch": 1.0}},
        "dfc-002": {"score": 80, "confidence": 0.9, "findings": [finding]},
        "dfc-003": {
            "score": 20,
            "confidence": 0.9,
            "findings": [{**finding, "skill": "domain_history", "type": "archive_copy"}],
        },
        "dfc-004": {"score": 50, "confidence": 0.1, "findings": [finding]},  # labelled
        "dfc-005": {"error": "boom"},
    }
    cand.write_results(corpus, {k: {"id": k, **v} for k, v in rows.items()})
    queue = cand.build_queue(corpus, seed=1, random_share=5)
    assert [i.id for i in queue] == ["dfc-000", "dfc-001", "dfc-002", "dfc-003"]
    assert queue[0].priority > 3 > queue[1].priority > queue[2].priority > 1 > queue[3].priority
    cand.save_finding_labels(corpus, corpus.labels()["dfc-000"], [(finding, "wrong")])
    saved = json.loads((tmp_path / cand.FINDING_LABELS).read_text())
    assert saved["verdict"] == "wrong" and saved["skill"] == "claim_audit" and saved["id"] == "dfc-000"


def test_ask_findings_defaults_to_unsure(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = iter(["c", "\r", "w", "x"])
    monkeypatch.setattr(cand, "read_key", lambda: next(keys))
    findings = [{"type": str(i)} for i in range(4)]
    assert [v for _, v in cand.ask_findings(findings)] == ["correct", "unsure", "wrong", "unsure"]


def test_pinned_page_from_elsewhere_goes_first(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    golden = LabelRecord(
        id="gold-1", label="legit", label_source="dataset", source="live", origin_dataset="golden", origin_url="u"
    )
    corpus.add_many([(golden, "<html></html>"), (_record(0), "<html></html>")])
    rows = {
        "gold-1": {"id": "gold-1", "score": 60, "confidence": 0.9, "features": {"claim_audit.count_off": 1.0}},
        "dfc-000": {"id": "dfc-000", "score": 50, "confidence": 0.1, "features": {"claim_audit.contradicted": 1.0}},
    }
    cand.write_results(corpus, rows)
    cand.pin(corpus, ["gold-1"], "is the count finding right?")
    queue = cand.build_queue(corpus, seed=1, random_share=0)
    assert [i.id for i in queue] == ["gold-1", "dfc-000"]
    assert queue[0].reason == "pinned: is the count finding right?"
    corpus.update([golden.model_copy(update={"label_source": "human"})])
    assert [i.id for i in cand.build_queue(corpus, seed=1, random_share=0)] == ["dfc-000"]


def test_relabel_tier_unlabelled_corpus_pages_and_nothing_dropped(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)
    records = [
        LabelRecord(id="mend-1", label="legit", label_source="agreed", source="wayback", origin_dataset="m"),
        LabelRecord(id="mend-2", label="scam", label_source="agreed", source="wayback", origin_dataset="m"),
        LabelRecord(id="coi-3", source="wayback", origin_dataset="c"),
        LabelRecord(id="coi-4", source="wayback", origin_dataset="c"),
        *(_record(i) for i in range(30)),
    ]
    corpus.add_many((r, "<html></html>") for r in records)
    fired_row = {"features": {"claim_audit.contradicted": 1.0}, "findings": []}
    quiet: dict[str, Any] = {"features": {"claim_audit.contradicted": 0.0}, "findings": []}
    (tmp_path / "reports" / "new-findings").mkdir(parents=True)
    rows = {"mend-1": fired_row, "mend-2": fired_row, "coi-3": fired_row, "coi-4": quiet}
    (tmp_path / "reports" / "new-findings" / "results.jsonl").write_text(
        "".join(json.dumps({"id": k, **v}) + "\n" for k, v in rows.items())
    )
    cand.write_results(
        corpus,
        {f"dfc-{i:03d}": {"id": f"dfc-{i:03d}", "score": 20, "confidence": 0.9} for i in range(30)},
    )
    queue = cand.build_queue(corpus, seed=1)
    ids = [i.id for i in queue]
    assert ids[0] == "mend-1" and queue[0].reason.startswith(cand.RELABEL)
    assert ids[1] == "coi-3"
    assert "mend-2" not in ids  # scam labels stand
    assert len(ids) == 2 + 1 + 30  # every unlabelled page is ranked, none dropped


def test_blind_is_about_one_in_five_and_fixed() -> None:
    share = sum(cand.blind(f"dfc-{i:05d}") for i in range(5000)) / 5000
    assert 0.17 < share < 0.23
    assert cand.blind("dfc-00001") == cand.blind("dfc-00001")


def test_blind_screen_hides_the_scorer(tmp_path: Path) -> None:
    record = _record(1)
    row = {"score": 42, "band": "careful", "confidence": 0.3, "new_findings": [], "findings": []}
    item = cand.QueueItem(id=record.id, reason="claim_audit fired; scorer unsure (0.30)", priority=3.7)
    html = "<html><title>t</title><body>text</body></html>"
    shown = cand.describe(record, row, item, n=1, total=1, html=html)
    hidden = cand.describe(record, row, item, n=1, total=1, html=html, hidden=True)
    assert "scorer: 42" in shown and "unsure" in shown
    assert "scorer" not in hidden.replace("the scorer's answer is hidden", "") and "unsure" not in hidden
    cand.decide(Corpus(tmp_path), record, "junk", hidden=True)
    logged = json.loads((tmp_path / cand.DECISIONS).read_text())
    assert logged["blind"] is True and logged["label"] == "junk" and logged["before"] is None


def test_miss_cost_reads_the_model_and_tokens_the_cache_stores() -> None:
    message = AIMessage(content="", usage_metadata={"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1})
    key = json.dumps({"kwargs": {"model_name": cand.settings.claim_audit_model}})
    assert cand.miss_cost(key, [ChatGeneration(message=message)]) == cand.settings.claim_audit_input_eur_per_mtok
    assert cand.miss_cost(key, []) == Decimal(0)
