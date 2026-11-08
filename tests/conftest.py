import pytest

from veridex import scoring


@pytest.fixture(autouse=True)
def no_shipped_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use the LLM-judge path unless they pass a model explicitly; the shipped artifact changes over time."""
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: None)
