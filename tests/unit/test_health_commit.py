"""/v1/health reports the commit the image was built from (Dockerfile ``GIT_SHA`` → ``UNFRET_GIT_SHA``)."""

from pathlib import Path

import pytest

from unfret.config import Settings, settings
from unfret.routes import probes

SHA = "d441b63fead49c98ea51530d3701a58718522703"
ROOT = Path(__file__).resolve().parents[2]


def test_liveness_reports_the_build_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "git_sha", f" {SHA}\n")
    assert probes.health().commit == SHA


def test_no_commit_outside_a_built_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "git_sha", "")
    body = probes.health().model_dump()
    assert body["commit"] is None
    assert body["status"] == "healthy"


def test_the_setting_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNFRET_GIT_SHA", SHA)
    assert Settings(_env_file=None).git_sha == SHA  # type: ignore[call-arg]


def test_the_image_and_the_deploys_pass_the_commit_on() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG GIT_SHA" in dockerfile
    assert "ENV UNFRET_GIT_SHA=$GIT_SHA" in dockerfile
    assert "GIT_SHA:" in (ROOT / "config" / "deploy.yml").read_text()
    assert "GIT_SHA: ${{ forgejo.sha }}" in (ROOT / ".forgejo" / "workflows" / "ci.yaml").read_text()
