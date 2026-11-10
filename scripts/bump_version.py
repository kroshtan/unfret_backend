"""
Cut a release: set the package version and turn the CHANGELOG's ``## Unreleased`` into that version's section.

Usage: ``uv run scripts/bump_version.py 0.6.0`` (``make bump VERSION=0.6.0``). It only edits files: committing,
tagging and deploying stay deliberate, manual steps.
"""

import argparse
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/kroshtan/unfret_backend"


def bump(version: str, today: datetime.date, root: Path = ROOT) -> str:
    """
    Set ``version`` in pyproject.toml and date the CHANGELOG's Unreleased section with it.

    :param version: The new version, ``MAJOR.MINOR.PATCH``.
    :param today: The release date.
    :param root: The repository root.
    :return: The previous version.
    :raises ValueError: If the version is malformed, not newer, or there is nothing unreleased.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {version}")
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text()
    match = re.search(r'^version = "(\d+\.\d+\.\d+)"$', text, flags=re.MULTILINE)
    if match is None:
        raise ValueError("no version in pyproject.toml")
    previous = match.group(1)
    if tuple(map(int, version.split("."))) <= tuple(map(int, previous.split("."))):
        raise ValueError(f"{version} is not newer than {previous}")
    changelog = root / "CHANGELOG.md"
    log = changelog.read_text()
    if "\n## Unreleased\n" not in log:
        raise ValueError("CHANGELOG.md has no '## Unreleased' section")
    heading = f"## [{version}]({REPO}/compare/{previous}...{version}) ({today.isoformat()})"
    changelog.write_text(log.replace("\n## Unreleased\n", f"\n## Unreleased\n\n{heading}\n", 1))
    pyproject.write_text(text[: match.start(1)] + version + text[match.end(1) :])
    return previous


def main() -> None:
    """Parse the version and bump."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    try:
        previous = bump(args.version, datetime.datetime.now(tz=datetime.UTC).date())
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"{previous} -> {args.version}: pyproject.toml and CHANGELOG.md updated; run `uv lock`, then commit.")


if __name__ == "__main__":
    main()
