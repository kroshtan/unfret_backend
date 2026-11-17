"""Retrained scorers stay out of the repository: the private model store, release notes, and the pre-commit guard."""

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from evals import release
from unfret import scoring

REPO = Path(__file__).resolve().parents[2]


def _guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_scorer_baseline", REPO / "scripts/check_scorer_baseline.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact(version: str) -> dict:
    data = json.loads(scoring.MODEL_PATH.read_text())
    return {**data, "version": version, "trained_on": {**data["trained_on"], "corpus_commit": "abc123"}}


def test_a_retrain_never_goes_into_the_source_tree(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source tree"):
        release.artifact_path("v2-x", REPO / "src" / "unfret" / "scoring")
    with pytest.raises(ValueError, match="not a model version"):
        release.artifact_path("../v2", tmp_path)
    assert release.artifact_path("v2-x", tmp_path) == tmp_path.resolve() / "v2-x" / "model.json"


def test_the_release_note_has_the_snapshot_the_metrics_and_the_replay(tmp_path: Path) -> None:
    path = release.artifact_path("v2-20261120-C", tmp_path)
    path.write_text(json.dumps(_artifact("v2-20261120-C")))

    note = release.write_note(path.parent).read_text()
    assert "# Scorer v2-20261120-C" in note and "corpus commit abc123" in note
    assert "ROC AUC" in note and "Not measured yet" in note

    summary = json.loads((REPO / "src/unfret/static/accuracy.json").read_text())
    (path.parent / "accuracy.json").write_text(json.dumps({**summary, "scorer_version": "v2-20261120-C"}))
    note = release.write_note(path.parent).read_text()
    assert "Not measured yet" not in note and "warnings right" in note

    (path.parent / "accuracy.json").write_text(json.dumps(summary))  # measured with the baseline
    with pytest.raises(ValueError, match="measured"):
        release.write_note(path.parent)


def test_current_names_released_versions_or_the_baseline(tmp_path: Path) -> None:
    for version in ("v2-20261120-C", "v2-quick-20261120-C"):
        path = release.artifact_path(version, tmp_path)
        path.write_text(json.dumps(_artifact(version)))
    with pytest.raises(ValueError, match="RELEASE.md"):
        release.set_current("v2-20261120-C", None, tmp_path)
    for version in ("v2-20261120-C", "v2-quick-20261120-C"):
        release.write_note(tmp_path / version)
    with pytest.raises(ValueError, match="quick tier"):
        release.set_current(None, "v2-20261120-C", tmp_path)

    current = json.loads(release.set_current("v2-20261120-C", "v2-quick-20261120-C", tmp_path).read_text())
    assert (current["model"], current["model_quick"]) == ("v2-20261120-C", "v2-quick-20261120-C")
    current = json.loads(release.set_current(None, None, tmp_path).read_text())
    assert (current["model"], current["model_quick"]) == (None, None)


def test_the_guard_accepts_the_baseline_and_refuses_a_retrain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _guard()
    assert guard.problems(["src/unfret/scoring/model.json", "src/unfret/static/accuracy.json"]) == []

    monkeypatch.setattr(guard, "REPO", tmp_path)
    scoring_dir = tmp_path / "src/unfret/scoring"
    scoring_dir.mkdir(parents=True)
    for name in ("model.json", "model-quick.json"):
        shutil.copy(scoring.MODEL_PATH.with_name(name), scoring_dir / name)
    (tmp_path / "model-v2.json").write_text(json.dumps(_artifact("v2")))
    (tmp_path / "other.json").write_text('{"version": "1"}')
    assert guard.problems(["model-v2.json", "other.json"]) == [
        "model-v2.json: a scorer artifact outside the open baseline"
    ]

    (scoring_dir / "model.json").write_text(json.dumps(_artifact("v2")))
    assert guard.problems([]) == ["src/unfret/scoring/model.json: differs from the open baseline scorer (v0.12.0)"]
