import datetime
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("bump_version", Path(__file__).parents[2] / "scripts/bump_version.py")
assert SPEC and SPEC.loader
bump_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump_version)


def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "unfret"\nversion = "0.5.0"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## Unreleased\n\n### Features\n\n* dollars\n")
    return tmp_path


def test_bump_dates_the_unreleased_section(tmp_path: Path) -> None:
    root = repo(tmp_path)
    assert bump_version.bump("0.6.0", datetime.date(2026, 11, 8), root) == "0.5.0"
    assert 'version = "0.6.0"' in (root / "pyproject.toml").read_text()
    log = (root / "CHANGELOG.md").read_text()
    heading = "## [0.6.0](https://github.com/kroshtan/unfret_backend/compare/0.5.0...0.6.0) (2026-11-08)"
    assert f"## Unreleased\n\n{heading}\n\n### Features" in log


@pytest.mark.parametrize("version", ["0.5.0", "0.4.9", "v0.6", "0.6"])
def test_bump_refuses_old_or_malformed_versions(tmp_path: Path, version: str) -> None:
    with pytest.raises(ValueError):
        bump_version.bump(version, datetime.date(2026, 11, 8), repo(tmp_path))
