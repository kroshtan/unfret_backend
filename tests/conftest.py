import pytest

from veridex import scoring
from veridex.visitors import RateLimit


@pytest.fixture(autouse=True)
def no_shipped_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use the LLM-judge path unless they pass a model explicitly; the shipped artifact changes over time."""
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: None)


@pytest.fixture(autouse=True)
def fresh_check_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts without remembered x402 answers and with an empty rate limit (both live in memory)."""
    from veridex.routes import check  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    monkeypatch.setattr(check, "_replays", {})
    monkeypatch.setattr(check, "_rate", RateLimit(check.settings.x402_check_per_minute))
