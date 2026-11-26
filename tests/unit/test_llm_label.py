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


def _level_call(page: str, level: int, confidence: float = 0.9, verdict: str = "correct") -> dict[str, Any]:
    call = _call(page, "legit", confidence, verdict)
    answer = {k: v for k, v in call["answer"].items() if k not in ("label", "reason")}
    because = [f"{level}: capacity given twice, differently"]
    return call | {"answer": answer | {"level": level, "because": because}, "prompt_version": "llm-label-v4"}


def test_v4_asks_the_overall_level_with_combining_and_protection() -> None:
    v4 = ll.PROMPTS[ll.PROMPT_VERSION]
    assert ll.PROMPT_VERSION in ll.LEVEL_PROMPTS and "llm-label-v3" not in ll.LEVEL_PROMPTS
    assert "OVERALL" in v4 and "combine" in v4 and "lowers it by one" in v4 and "raises it by one" in v4
    assert "Because:" in v4 and "materiality" in v4.lower()
    answer = ll.LevelLabel(findings=[], because=["0: nothing a buyer would mind"], level=0, confidence=0.8)
    assert ll.label_of(answer.model_dump()) == "legit" and ll.answer_text(answer.model_dump()) == "level 0"
    with pytest.raises(ValidationError):
        ll.LevelLabel(findings=[], because=[], level=5, confidence=0.8)


def test_level_prompt_says_what_findings_point_at_and_older_prompts_stay_byte_for_byte(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 3)
    rows = cand.label_rows(corpus)
    record = corpus.labels()["p0"]
    facts_v4, _, _ = ll.page_brief(corpus, record, rows["p0"], with_levels=True)
    facts_v3, _, _ = ll.page_brief(corpus, record, rows["p0"])
    assert "claim_contradicted (evidence for level 3): e" in facts_v4
    assert "evidence for level" not in facts_v3 and "claim_contradicted: e" in facts_v3


def test_level_examples_are_his_earliest_level_labels_and_never_the_page(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 5)
    decisions = [
        {"id": "p0", "label": "legit", "level": 0, "blind": False},
        {"id": "p1", "label": "junk", "level": 3, "blind": False},
        {"id": "p2", "label": "junk", "level": 3, "blind": False},  # a second level 3: not taken
        {"id": "p3", "label": "junk", "level": 2, "blind": True},  # blind: the honest check, never an example
        {"id": "p4", "label": "junk", "level": 1, "blind": False},
        {"id": "p4", "label": "junk", "level": 2, "blind": False},  # changed his mind: no longer a level 1 example
    ]
    (tmp_path / cand.DECISIONS).write_text("".join(json.dumps(d) + "\n" for d in decisions))
    rows = cand.label_rows(corpus)
    assert ll.level_examples(corpus, rows, "llm-label-v4") == ["p0", "p1"]
    block = ll.level_example_block(corpus, "p1", rows, "llm-label-v4")
    assert "overall level: 0" in block and "overall level: 3" not in block and "s0.example" in block


def test_level_examples_fall_back_to_finding_verdicts_before_any_level_label(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 4)
    (tmp_path / cand.DECISIONS).write_text(json.dumps({"id": "p0", "label": "legit", "blind": False}) + "\n")
    (tmp_path / cand.FINDING_LABELS).write_text(json.dumps({"id": "p2", "evidence": "e", "verdict": "wrong"}) + "\n")
    (tmp_path / ll.EXAMPLES).parent.mkdir(parents=True, exist_ok=True)
    chosen = {"per_class": 1, "legit": ["p0"], "junk": ["p2"]}
    (tmp_path / ll.EXAMPLES).write_text(json.dumps({"llm-label-v3": chosen}))
    block = ll.level_example_block(corpus, "p3", cand.label_rows(corpus), "llm-label-v4")
    assert "finding verdicts only" in block and "finding 1: wrong" in block
    assert "overall level" not in block and "page label" not in block


def test_level_labels_go_to_the_label_store_with_the_legacy_label_following(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 3)
    corpus.update([corpus.labels()["p0"].model_copy(update={"label": "junk", "label_source": "human", "level": 2})])
    rows = cand.label_rows(corpus)
    source = ll.source_of("m", "llm-label-v4")
    calls = [_level_call("p0", 4), _level_call("p1", 4), _level_call("p2", 0, verdict="wrong")]
    assert ll.store(corpus, calls, rows, source) == 2
    known = Corpus(tmp_path).labels()
    assert (known["p0"].level, known["p0"].level_source) == (2, None)  # a human label is never overwritten
    assert (known["p1"].level, known["p1"].level_source, known["p1"].label) == (4, source, "scam")
    assert (known["p2"].level, known["p2"].label, known["p2"].label_source) == (0, "legit", source)
    stored = {r["id"]: r for r in ll.read_jsonl(tmp_path / ll.LLM_LABELS)}
    assert stored["p1"]["level"] == 4 and stored["p1"]["because"] == ["4: capacity given twice, differently"]
    assert stored["p2"]["findings"][0]["verdict"] == "wrong"  # per-finding verdicts kept
    assert stored["p1"]["reason"] == "4: capacity given twice, differently"
    items = ll.build_review_queue(corpus, unsure=0.95, audit=0)
    assert all("LLM says level" in i.reason for i in items)


def test_level_agreement_against_his_levels_and_his_old_labels_range(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 4)
    decisions = [
        {"id": "p0", "label": "junk", "level": 3, "blind": False},
        {"id": "p1", "label": "junk", "level": 2, "blind": False},
        {"id": "p2", "label": "legit", "blind": False},  # old label: 0 to 2
        {"id": "p3", "label": "scam", "blind": True},  # old label: 4
    ]
    (tmp_path / cand.DECISIONS).write_text("".join(json.dumps(d) + "\n" for d in decisions))
    answers = {"p0": _level_call("p0", 3), "p1": _level_call("p1", 3), "p2": _level_call("p2", 1)}
    answers["p3"] = _level_call("p3", 2)
    report = ll.compare(corpus, answers, cand.label_rows(corpus))
    assert report["human_level_pages"] == 2 and report["level_exact"] == 0.5 and report["level_within_one"] == 1.0
    assert report["level_in_human_range"] == 0.5  # p1 is one off; p3: 2 is outside scam's 4
    assert report["any_concern_agreement"] == 1.0  # p2's old legit label doesn't settle it
    assert report["level_confusion"] == {"2->3": 1, "3->3": 1, "legit->1": 1, "scam->2": 1}
    assert report["confusion"]["scam->junk"] == 1  # the legacy comparison still runs, on the derived label
    assert ll.level_agreement({}, {}) == {}


def test_relevel_gives_older_llm_labels_a_level_and_nothing_else_changes(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, 4)
    rows = cand.label_rows(corpus)
    ll.store(corpus, [_call("p0", "junk"), _call("p1", "legit")], rows, ll.source_of("m", "llm-label-v3"))
    ll.store(corpus, [_level_call("p2", 1)], rows, ll.source_of("m", "llm-label-v4"))
    assert [r.id for r in ll.to_label(corpus, 10)] == ["p3"]
    assert [r.id for r in ll.to_label(corpus, 10, relevel=True)] == ["p0", "p1", "p3"]
    ll.store(corpus, [_level_call("p0", 2)], rows, ll.source_of("m", "llm-label-v4"))
    assert corpus.labels()["p0"].level == 2
    assert [r.id for r in ll.to_label(corpus, 10, relevel=True)] == ["p1", "p3"]


def test_deep_label_serves_proposals_and_enter_accepts_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus(tmp_path, 3)
    rows = cand.label_rows(corpus)
    ll.store(corpus, [_call("p0", "junk")], rows, ll.source_of("m", "llm-label-v3"))  # no level: not served
    ll.store(corpus, [_level_call("p1", 3), _level_call("p2", 1)], rows, ll.source_of("m", "llm-label-v4"))
    assert [i.id for i in cand.pages_to_label(corpus, "queue")] == ["p1", "p2"]
    monkeypatch.setattr(cand, "blind", lambda _page: False)
    keys = iter(["\r", "\r", "2", "w"])  # p1: accept level 3, accept "correct"; p2: correct to 2, finding wrong
    monkeypatch.setattr(cand, "read_key", lambda: next(keys))
    cand.label(corpus, batch=5)
    known = corpus.labels()
    assert (known["p1"].level, known["p1"].level_source, known["p1"].label_source) == (3, "human", "human")
    assert (known["p2"].level, known["p2"].label) == (2, "junk")
    decisions = [json.loads(x) for x in (tmp_path / cand.DECISIONS).read_text().splitlines()]
    assert [(d["level"], d["proposed_level"]) for d in decisions] == [(3, 3), (2, 1)]
    verdicts = [json.loads(x)["verdict"] for x in (tmp_path / cand.FINDING_LABELS).read_text().splitlines()]
    assert verdicts == ["correct", "wrong"]
    assert cand.pages_to_label(corpus, "queue") == []


def test_a_blind_proposal_page_hides_the_llm_and_enter_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus(tmp_path, 1)
    ll.store(corpus, [_level_call("p0", 3)], cand.label_rows(corpus), ll.source_of("m", "llm-label-v4"))
    monkeypatch.setattr(cand, "blind", lambda _page: True)
    screens: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **_k: screens.append(" ".join(map(str, a))))
    monkeypatch.setattr(cand, "read_key", lambda: "\r")
    cand.label(corpus, batch=5)
    assert not any("proposes level" in s or "accept the LLM" in s for s in screens)
    assert corpus.labels()["p0"].level_source == ll.source_of("m", "llm-label-v4")  # skipped, still a proposal
