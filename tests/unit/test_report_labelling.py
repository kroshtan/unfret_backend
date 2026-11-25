import json
from pathlib import Path

from evals.corpus import Corpus
from evals.reports import confident, corpus_entry, describe, preview, summary, to_corpus
from unfret.html_cleaner import clean_html

ITEM = {
    "id": 7,
    "page_key": "amazon.nl|asin:B0ABCDEFGH",
    "domain": "amazon.nl",
    "reason": "scam",
    "score": 82,
    "corroborations": 2,
    "created_at": "2026-11-09T10:00:00+00:00",
    "payload": {
        "url": "https://www.amazon.nl/x/dp/B0ABCDEFGH",
        "facts": {
            "platform": "amazon",
            "title": "Wool <sweater>",
            "json_ld": [{"@type": "Product", "name": "Wool sweater", "gtin13": "8711253012345"}],
        },
        "text": "Wool sweater, 85% off. Only today!",
        "flags": [],
        "explanation": "Looks legitimate.",
        "verdict": None,
    },
    "second_opinion": {
        "state": "done",
        "label": "scam",
        "confidence": 0.93,
        "reason": "Big-brand goods at 85% off from an unknown seller.",
        "model": "qwen3.5-397b-a17b",
        "agreement": "reporter",
        "gap": 42,
        "reward_checks": 5,
    },
}
UNSURE = {**ITEM, "id": 8, "second_opinion": {**ITEM["second_opinion"], "label": "unsure", "confidence": 0.95}}  # type: ignore[dict-item]
WAITING = {**ITEM, "id": 9, "second_opinion": {"state": "waiting", "agreement": "none"}}


def test_describe_shows_what_matters() -> None:
    screen = describe(ITEM, 1, 3)
    assert "model score: 82" in screen
    assert "+2 confirmations" in screen
    assert "85% off" in screen
    assert "second opinion: scam (0.93) · sides with reporter · " in screen
    assert "[Enter/a]ccept proposal" in screen
    assert "new to the corpus" in screen
    assert "in the corpus already as junk" in describe(ITEM, 1, 3, "junk")
    waiting = describe(WAITING, 1, 3)
    assert "second opinion: none (waiting)" in waiting
    assert "accept" not in waiting


def test_bulk_candidates_are_confident_proposals_only() -> None:
    assert [i["id"] for i in confident([ITEM, UNSURE, WAITING], 0.9)] == [7]
    assert confident([ITEM], 0.95) == []
    table = preview([ITEM])
    assert "amazon.nl" in table
    assert "score  82 → scam  0.93 reporter" in table
    assert "Big-brand goods" in table
    assert summary(
        {"open": 3, "cap": 1000, "second_opinions": {"done": 2, "waiting": 1}}, [ITEM, UNSURE, WAITING]
    ) == ("3 open (cap 1000); second opinions: done 2, waiting 1; sides with: none 1, reporter 2")


def test_a_confirmed_proposal_keeps_the_second_opinion_in_the_corpus_record() -> None:
    record, _ = corpus_entry(ITEM, "scam", bulk=True)
    assert (record.label, record.label_source) == ("scam", "human")
    assert (record.llm_label, record.llm_confidence) == ("scam", 0.93)
    assert record.llm_reason.startswith("Big-brand goods")
    assert json.loads(record.notes)["confirmed_in_bulk"] is True
    unsure, _ = corpus_entry(UNSURE, "legit")
    assert unsure.llm_label is None  # unsure is no label, as in the corpus's own second opinions
    assert "confirmed_in_bulk" not in json.loads(unsure.notes)


def test_a_labelled_report_lands_in_the_corpus_layout(tmp_path: Path) -> None:
    corpus = Corpus(tmp_path)

    record_id = to_corpus(corpus, ITEM, "scam")

    record = corpus.labels()[record_id]
    assert record_id.startswith("report-")
    assert (record.label, record.label_source, record.source) == ("scam", "human", "report")
    assert record.platform == "amazon"
    assert json.loads(record.notes) == {"model_score": 82, "reported_as": "scam"}
    page = corpus.read_page(record_id)
    assert "Wool sweater, 85% off" in clean_html(page)
    assert '"gtin13": "8711253012345"' in page  # the product JSON-LD survives for the extractor
    assert "<sweater>" not in page  # escaped
    facts = json.loads((corpus.pages_dir / f"{record_id}.facts.json").read_text())
    assert facts["title"] == "Wool <sweater>"
    assert to_corpus(corpus, ITEM, "junk") == record_id  # labelling again overwrites the same page
    assert corpus.labels()[record_id].label == "junk"


def test_a_reported_quick_result_lands_in_the_corpus_like_any_other(tmp_path: Path) -> None:
    # The corpus page is the same whichever tier was reported, so it trains both the deep and the quick scorer
    # (evals.train, with and without --quick); the record notes which tier's score was off.
    payload: dict[str, object] = {**ITEM["payload"], "tier": "quick"}  # type: ignore[dict-item]
    quick = {**ITEM, "page_key": "amazon.nl|asin:B0QUICK123", "payload": payload}
    corpus = Corpus(tmp_path)
    record_id = to_corpus(corpus, quick, "scam")
    record = corpus.labels()[record_id]
    assert json.loads(record.notes) == {"model_score": 82, "reported_as": "scam", "tier": "quick"}
    assert (record.label_source, record.source, record.split) == ("human", "report", None)  # split later, as all
    assert "quick check" in describe(quick, 1, 1) and "deep check" in describe(ITEM, 1, 1)
