import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals import candidates as cand
from evals import llm_label as ll
from evals.corpus import Corpus, LabelRecord, QueueItem, is_llm


def _corpus(tmp_path: Path, n: int = 6) -> Corpus:
    corpus = Corpus(tmp_path)
    records = [
        LabelRecord(id=f"p{i}", source="live", origin_dataset="d", origin_url=f"https://s{i}.example/p")
        for i in range(n)
    ]
    corpus.add_many((r, "<html><title>t</title><body>text</body></html>") for r in records)
    (tmp_path / cand.QUEUE).write_text(
        "".join(QueueItem(id=f"p{i}", reason="r", priority=n - i).model_dump_json() + "\n" for i in range(n))
    )
    finding = {"skill": "claim_audit", "type": "claim_contradicted", "evidence": "e"}
    cand.write_results(corpus, {f"p{i}": {"id": f"p{i}", "findings": [finding], "features": {}} for i in range(n)})
    return corpus


def _call(page: str, label: str, confidence: float = 0.9, verdict: str = "correct") -> dict[str, Any]:
    answer = {
        "findings": [{"n": 1, "why": "w", "verdict": verdict}],
        "reason": "r",
        "label": label,
        "confidence": confidence,
    }
    return {
        "id": page,
        "answer": answer,
        "model": "m",
        "reasoning": "",
        "model_version": "m",
        "prompt_version": ll.PROMPT_VERSION,
    }


def test_cost_charges_unknown_models_at_the_dearest() -> None:
    assert ll.call_cost("glm-5.3", 1_000_000, 0) == Decimal("1.80")
    assert ll.call_cost("unknown", 0, 1_000_000) == ll.DEAREST[1]


def test_prompt_versions_keep_v1_and_later_ones_change_the_junk_test() -> None:
    assert ll.PROMPTS["llm-label-v1"] == ll.SYSTEM
    assert ll.APPLYING in ll.PROMPTS["llm-label-v2"] and ll.APPLYING not in ll.SYSTEM
    v3 = ll.PROMPTS["llm-label-v3"]
    assert "materiality" in v3 and "nobody believes" not in v3 and ll.APPLYING not in v3


def test_examples_come_from_the_corpus_and_leave_the_page_out(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 4)
    decisions = [("p0", "legit"), ("p1", "legit"), ("p2", "junk"), ("p3", "junk")]
    (tmp_path / cand.DECISIONS).write_text(
        "".join(json.dumps({"id": p, "label": lab, "blind": False}) + "\n" for p, lab in decisions)
    )
    (tmp_path / ll.EXAMPLES).parent.mkdir(parents=True, exist_ok=True)
    chosen = {"per_class": 1, "legit": ["p0", "p1"], "junk": ["p2", "p3"]}
    (tmp_path / ll.EXAMPLES).write_text(json.dumps({"llm-label-v3": chosen}))
    rows = cand.label_rows(corpus)
    block = ll.example_block(corpus, "p0", rows, "llm-label-v3")
    assert block.count("page label: legit") == 1 and block.count("page label: junk") == 1
    assert "s1.example" in block and "s0.example" not in block


def test_compare_counts_pages_blind_pages_and_findings(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 3)
    decisions = [("p0", "junk", False), ("p1", "legit", True), ("p2", "junk", True)]
    (tmp_path / cand.DECISIONS).write_text(
        "".join(json.dumps({"id": p, "label": lab, "blind": b}) + "\n" for p, lab, b in decisions)
    )
    (tmp_path / cand.FINDING_LABELS).write_text(
        "".join(json.dumps({"id": p, "evidence": "e", "verdict": "correct"}) + "\n" for p in ("p0", "p1"))
    )
    answers = {"p0": _call("p0", "legit"), "p1": _call("p1", "legit", verdict="wrong"), "p2": _call("p2", "junk")}
    report = ll.compare(corpus, answers, cand.label_rows(corpus))
    assert report["page_agreement"] == round(2 / 3, 3)
    assert report["blind_pages"] == 2 and report["blind_agreement"] == 1.0
    assert report["junk_agreement"] == 0.5 and report["legit_agreement"] == 1.0
    assert report["findings"] == 2 and report["finding_agreement"] == 0.5
    assert report["confusion"] == {"junk->junk": 1, "junk->legit": 1, "legit->legit": 1}


def test_test_set_is_never_labelled_and_humans_are_never_overwritten(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 6)
    corpus.update([corpus.labels()["p0"].model_copy(update={"label": "junk", "label_source": "human"})])
    reserved = ll.reserve_test_set(corpus, 2)
    assert len(reserved) == 2 and "p0" not in reserved
    assert ll.reserve_test_set(corpus, 5) == reserved  # made once, then kept
    todo = ll.to_label(corpus, 10)
    assert {r.id for r in todo} == {f"p{i}" for i in range(1, 6)} - set(reserved)
    rows = cand.label_rows(corpus)
    source = ll.source_of("m", "llm-label-v3")
    written = ll.store(corpus, [_call("p0", "legit"), *(_call(r.id, "junk", 0.5) for r in todo)], rows, source)
    assert written == len(todo)
    known = corpus.labels()
    assert known["p0"].label == "junk" and known["p0"].label_source == "human"
    assert all(known[r.id].label_source == "llm:m:llm-label-v3" for r in todo)
    reopened = Corpus(tmp_path).labels()  # the source survives a round trip through labels.jsonl
    assert all(is_llm(reopened[r.id].label_source) for r in todo)
    assert ll.to_label(corpus, 10) == []
    assert {i.id for i in cand.pages_to_label(corpus, "test")} == set(reserved)


def test_label_sources_accept_llm_labellers_only() -> None:
    assert LabelRecord(id="a", source="s", origin_dataset="d", label_source="llm:m:v3").label_source == "llm:m:v3"
    with pytest.raises(ValidationError):
        LabelRecord(id="a", source="s", origin_dataset="d", label_source="someone")
    assert is_llm("llm") and is_llm("llm:m:v3") and not is_llm("human") and not is_llm(None)


def test_review_queue_takes_unsure_pages_then_a_blind_audit(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 6)
    rows = cand.label_rows(corpus)
    calls = [_call(f"p{i}", "legit", 0.5 if i < 1 else 0.95) for i in range(6)]
    calls[1]["answer"]["findings"][0]["confidence"] = 0.4  # a sure page with an unsure finding verdict
    ll.store(corpus, calls, rows, ll.source_of("m", "v3"))
    items = ll.build_review_queue(corpus, unsure=0.7, audit=0.34)
    reasons = [i.reason.split(":")[0] for i in items]
    assert reasons == [ll.UNSURE, ll.UNSURE, ll.AUDIT, ll.AUDIT]
    assert {i.id for i in items[:2]} == {"p0", "p1"}
    assert [i.id for i in cand.pages_to_label(corpus, "review")] == [i.id for i in items]
