import json
from pathlib import Path

from evals.corpus import Corpus
from evals.reports import describe, to_corpus
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
}


def test_describe_shows_what_matters() -> None:
    screen = describe(ITEM, 1, 3)
    assert "model score: 82" in screen
    assert "+2 confirmations" in screen
    assert "85% off" in screen


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
