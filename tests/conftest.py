import os
import shutil
from collections.abc import Iterator
from typing import NoReturn

import pytest

from unfret import scoring
from unfret.visitors import RateLimit


def unavailable(reason: str) -> NoReturn:
    """
    Skip a test whose prerequisite is missing locally; in CI (``CI`` set) a missing prerequisite is a failure.

    :param reason: What is missing.
    """
    if os.getenv("CI"):
        pytest.fail(f"{reason} (CI must run every test)")
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def no_shipped_scorer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use the LLM-judge path unless they pass a model explicitly; the shipped artifact changes over time."""
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: None)


@pytest.fixture(autouse=True)
def fresh_check_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts without remembered x402 answers and with an empty rate limit (both live in memory)."""
    from unfret.routes import check  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    monkeypatch.setattr(check, "_replays", {})
    monkeypatch.setattr(check, "_rate", RateLimit(check.settings.x402_check_per_minute))


@pytest.fixture(autouse=True)
def restore_app_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Whatever a test sets on the app (pool, graphs, x402, credits, fiat) is put back afterwards, so none leaks."""
    from unfret.main import app  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    before = dict(vars(app))
    yield
    # Undo the monkeypatches first: one made after a fixture set an attribute would put that value back.
    monkeypatch.undo()
    for name in set(vars(app)) - set(before):
        delattr(app, name)
    for name, value in before.items():
        if vars(app).get(name) is not value:
            setattr(app, name, value)


@pytest.fixture
def node() -> None:
    """Tests that run the pages' JavaScript need node (CI has it: it runs the checkout action)."""
    if shutil.which("node") is None:
        unavailable("node not installed")
