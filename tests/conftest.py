import os
import shutil
from collections.abc import Iterator
from typing import NoReturn

import pytest

from unfret import scoring
from unfret.reports import second_opinion
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
    # A retrained scorer configured on this machine (UNFRET_MODEL_PATH in the environment or .env) never reaches a
    # test: tests that read an artifact read the baseline.
    monkeypatch.setattr(scoring.settings, "model_path", "")
    monkeypatch.setattr(scoring.settings, "model_quick_path", "")


@pytest.fixture(autouse=True)
def no_second_opinion_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test asks a real model for a report's second opinion (a provider key in .env would otherwise reach one)."""
    monkeypatch.setattr(scoring.settings, "report_second_opinion_model", "")
    yield
    second_opinion.use_model(None)


@pytest.fixture(autouse=True)
def no_photo_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test fetches product photos or asks a vision model about them unless it turns the photo check on."""
    monkeypatch.setattr(scoring.settings, "photo_claims", False)


@pytest.fixture(autouse=True)
def fresh_check_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts without remembered x402 answers and with an empty rate limit (both live in memory)."""
    from unfret.routes import check  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    monkeypatch.setattr(check, "_replays", {})
    monkeypatch.setattr(check, "_rate", RateLimit(check.settings.x402_check_per_minute))


@pytest.fixture(autouse=True)
def fresh_redeem_and_quote_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code redemptions and price quotes are limited per visitor network in memory: each test starts afresh."""
    from unfret.routes import credits, invites  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    monkeypatch.setattr(invites, "_attempts", RateLimit(invites.settings.redeem_per_ip_per_10min, window_s=600))
    monkeypatch.setattr(credits, "_quotes", RateLimit(credits.settings.quotes_per_ip_per_minute))


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


@pytest.fixture(autouse=True)
def fresh_analysis_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uncharged analyses are counted per account in memory: each test starts afresh."""
    from unfret.routes import analyze  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    monkeypatch.setattr(analyze, "_free_outcomes", RateLimit(analyze.settings.analyze_free_outcomes_per_hour, 3600))


@pytest.fixture
def node() -> None:
    """Tests that run the pages' JavaScript need node (CI has it: it runs the checkout action)."""
    if shutil.which("node") is None:
        unavailable("node not installed")


@pytest.fixture(autouse=True)
def fresh_thumbnail_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thumbnails remember failed photos and count requests per visitor network in memory: each test starts afresh."""
    from unfret.routes import images  # noqa: PLC0415 - keep the app out of import time for unrelated tests

    monkeypatch.setattr(images, "_failed", {})
    monkeypatch.setattr(images, "_requests", RateLimit(120))


@pytest.fixture(autouse=True)
def no_claim_audit_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test asks the claim audit's stronger model about a page unless it turns the audit on (with a fake model)."""
    monkeypatch.setattr(scoring.settings, "claim_audit", False)
