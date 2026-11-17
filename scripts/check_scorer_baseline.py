"""
Pre-commit hook (and CI, which runs every hook on every file): keep retrained scorers out of this repository.

The scorer in ``src/unfret/scoring/`` is the open baseline, what a self-hosted Unfret runs. Retrained scorers are
private (README, "The model"): ``make train`` writes them to the model store outside the repository. This hook fails
when a baseline artifact differs from the pinned one, or when any other JSON file in the repository is a scorer
artifact.

To replace the open baseline on purpose, update :data:`BASELINE_SHA256` in the same commit and say why in the
changelog.

Usage::

    python scripts/check_scorer_baseline.py [FILE ...]
"""

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE_SHA256 = {
    # v1-20261109-222323-C (v0.12.0)
    "src/unfret/scoring/model.json": "f8bc101acda4a7814788dd0462b317b2f8dcbc04873926f00bd8215b8d61ae9b",
    # v1-quick-20261109-222308-C (v0.12.0)
    "src/unfret/scoring/model-quick.json": "78133e862289af6299ad907ba9d5bb7955467145825d38295e3f227dbc80ea54",
}
# The keys every scorer artifact has (unfret.scoring.Model.from_dict).
ARTIFACT_KEYS = {"version", "features", "means", "scales", "coefficients", "intercept"}

HELP = (
    "Retrained scorers are private and never committed to this repository: `make train` writes them to the model "
    "store (<corpus>/models, see README, 'The model', and evals/release.py), and a server runs one with "
    "UNFRET_MODEL_PATH / UNFRET_MODEL_QUICK_PATH. To replace the open baseline on purpose, update BASELINE_SHA256 in "
    "scripts/check_scorer_baseline.py in the same commit."
)


def is_artifact(path: Path) -> bool:
    """
    Whether a file is a scorer artifact.

    :param path: A JSON file.
    :return: True when it parses as an object with every key of a scorer artifact.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, ValueError:
        return False
    return isinstance(data, dict) and data.keys() >= ARTIFACT_KEYS


def problems(files: list[str]) -> list[str]:
    """
    What is wrong with the given files, and with the baseline artifacts in any case.

    :param files: Paths relative to the repository root (as pre-commit passes them).
    :return: One message per problem.
    """
    found = []
    for name, digest in BASELINE_SHA256.items():
        path = REPO / name
        if not path.is_file():
            found.append(f"{name}: the open baseline scorer is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            found.append(f"{name}: differs from the open baseline scorer (v0.12.0)")
    for name in files:
        rel = Path(name).as_posix()
        if rel not in BASELINE_SHA256 and rel.endswith(".json") and is_artifact(REPO / rel):
            found.append(f"{rel}: a scorer artifact outside the open baseline")
    return found


def main() -> int:
    """
    Command-line entry point.

    :return: The exit status.
    """
    found = problems(sys.argv[1:])
    for message in found:
        print(f"check-scorer-baseline: {message}", file=sys.stderr)
    if found:
        print(HELP, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
