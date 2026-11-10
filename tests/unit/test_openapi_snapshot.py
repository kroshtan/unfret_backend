"""
The API's OpenAPI document is pinned, so a change to the public contract shows up in review.

After an intended change: ``UNFRET_UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_openapi_snapshot.py`` and commit
``tests/snapshots/openapi.json`` (and note it in ``docs/api-contract-v1.md``).
"""

import json
import os
from pathlib import Path

from unfret import __version__
from unfret.main import app

SNAPSHOT = Path(__file__).parents[1] / "snapshots" / "openapi.json"


def current() -> str:
    # Releases change the version (the document's, and defaults such as the health probe's); the contract doesn't.
    text = json.dumps(app.openapi(), indent=1, sort_keys=True) + "\n"
    return text.replace(f'"{__version__}"', '"<version>"')


def test_the_public_contract_is_unchanged() -> None:
    if os.environ.get("UNFRET_UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(current())
    assert SNAPSHOT.exists(), "no snapshot yet: run with UNFRET_UPDATE_SNAPSHOTS=1"
    assert current() == SNAPSHOT.read_text(), (
        "The OpenAPI document changed. If that's intended, rerun with UNFRET_UPDATE_SNAPSHOTS=1, commit "
        "tests/snapshots/openapi.json and describe the change in docs/api-contract-v1.md."
    )
