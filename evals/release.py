"""
Retrained scorers and their release notes, kept outside this repository.

The repository ships one scorer, the **open baseline** (``src/unfret/scoring/model.json`` and ``model-quick.json``).
Every retrain goes to a model store instead (``UNFRET_MODELS_DIR``, by default ``<corpus>/models``), one directory
per version::

    <store>/<version>/model.json      the artifact (a server runs it with UNFRET_MODEL_PATH or UNFRET_MODEL_QUICK_PATH)
    <store>/<version>/RELEASE.md      version, data snapshot, TEST metrics, and the replay's numbers once measured
    <store>/<version>/accuracy.json   the replay's aggregates (make accuracy with UNFRET_MODEL_PATH set to the model)
    <store>/current.json              the release a deploy runs: {"model": <version>, "model_quick": <version>}

``evals.train``, ``evals.markets`` and ``evals.us_market`` write there and never into ``src/``. ``null`` in
``current.json`` means the baseline. Usage::

    uv run python -m evals.release note <version>                  # (re)write a version's RELEASE.md
    uv run python -m evals.release current --model V [--quick V]   # point current.json at versions (omitted: baseline)
"""

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.config import eval_settings
from unfret.scoring import MODEL_PATH, MODEL_QUICK_PATH, Model

REPO = Path(__file__).resolve().parents[1]
SOURCE_TREE = REPO / "src"
ARTIFACT = "model.json"
NOTE = "RELEASE.md"
ACCURACY = "accuracy.json"
CURRENT = "current.json"


def store() -> Path:
    """
    Where retrained scorers go.

    :return: ``UNFRET_MODELS_DIR``, or ``<corpus>/models``.
    """
    return eval_settings.models_dir or eval_settings.corpus_dir / "models"


def artifact_path(version: str, root: Path | None = None) -> Path:
    """
    Where a retrained scorer's artifact goes, refusing any place in the source tree.

    :param version: The model's version (one directory per version).
    :param root: The model store; :func:`store` by default.
    :return: ``<store>/<version>/model.json``, its directory created.
    :raises ValueError: If the store is inside ``src/`` (retrained scorers never ship with the code) or the version
        isn't a plain directory name.
    """
    base = (root or store()).resolve()
    if base.is_relative_to(SOURCE_TREE):
        raise ValueError(f"{base} is in the source tree: retrained scorers are kept out of the repository (README)")
    if not version or "/" in version or version.startswith("."):
        raise ValueError(f"not a model version: {version!r}")
    path = base / version / ARTIFACT
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    return done.stdout.strip() if done.returncode == 0 else ""


def snapshot(corpus_root: Path) -> dict[str, Any]:
    """
    The data and code a model is trained from, for its artifact and release note.

    :param corpus_root: The corpus checkout.
    :return: The corpus commit and whether it had uncommitted changes, and this repository's commit.
    """
    return {
        "corpus_commit": _git(corpus_root, "rev-parse", "HEAD") or None,
        "corpus_dirty": bool(_git(corpus_root, "status", "--porcelain", "--untracked-files=no")),
        "code_commit": _git(REPO, "rev-parse", "HEAD") or None,
    }


def _pct(value: Any) -> str:
    return f"{100 * value:.1f}%" if isinstance(value, int | float) else "–"


def _metrics_lines(test: dict[str, Any]) -> list[str]:
    return [
        f"- ROC AUC {test.get('roc_auc', '–')}, Brier {test.get('brier', '–')}",
        *(
            f"- warned below {t['threshold']}: precision {_pct(t['precision'])}, recall {_pct(t['recall'])}, "
            f"F1 {t['f1']:.3f}"
            for t in test.get("thresholds", [])
        ),
    ]


def _replay_lines(summary: dict[str, Any]) -> list[str]:
    products = summary.get("groups", {}).get("products", {})
    lines = [
        (
            f"Measured {summary.get('generated')} with `make accuracy` (LLM `{summary.get('llm_model')}`, "
            f"recorded answers only; aggregates in `{ACCURACY}`)."
        ),
        "",
        (
            f"- product pages scored: {products.get('scored', '–')} (unreliable "
            f"{products.get('unreliable', '–')}, legit {products.get('legit', '–')}); ROC AUC "
            f"{products.get('roc_auc', '–')}"
        ),
        (
            f"- caught (Think twice or Serious concerns): {_pct(products.get('caught'))}; warnings right: "
            f"{_pct(products.get('warning_precision'))}; legit pages warned about: "
            f"{_pct(products.get('legit_warned'))}"
        ),
    ]
    for band, row in products.get("bands", {}).items():
        lines.append(f"- {band}: {row.get('pages')} pages, {_pct(row.get('unreliable_share'))} unreliable")
    for name in ("ads", "trades"):
        group = summary.get("groups", {}).get(name)
        if group:
            lines.append(
                f"- {name}: scored {group.get('scored', '–')}, caught {_pct(group.get('caught'))}, warnings right "
                f"{_pct(group.get('warning_precision'))}, legit warned {_pct(group.get('legit_warned'))}"
            )
    return lines


def write_note(release_dir: Path) -> Path:
    """
    Write a release's ``RELEASE.md`` from its artifact, and from its replay's aggregates once measured.

    :param release_dir: ``<store>/<version>``.
    :return: The note's path.
    :raises ValueError: If ``accuracy.json`` there was measured with another scorer.
    """
    artifact = json.loads((release_dir / ARTIFACT).read_text())
    version = artifact["version"]
    trained = artifact.get("trained_on", {})
    quick = "-quick-" in version
    baseline = json.loads((MODEL_QUICK_PATH if quick else MODEL_PATH).read_text())
    corpus = trained.get("corpus_commit") or "unknown"
    if trained.get("corpus_dirty"):
        corpus += " (with uncommitted changes)"
    lines = [
        f"# Scorer {version}",
        "",
        f"- Kind: {'quick tier (`UNFRET_MODEL_QUICK_PATH`)' if quick else 'full (`UNFRET_MODEL_PATH`)'}",
        f"- Features: {len(artifact['features'])}",
        (
            f"- Data snapshot: corpus commit {corpus}; features `{trained.get('features_file', '–')}`; "
            f"{trained.get('train_n', '–')} training pages; calibrated to a prior of {trained.get('prior', '–')}"
        ),
        f"- Code: backend commit {trained.get('code_commit') or 'unknown'}",
        "",
        "## TEST split (held out from training)",
        "",
        *_metrics_lines(artifact.get("test", {})),
        "",
        f"The open baseline ({baseline['version']}) on its own TEST split:",
        "",
        *_metrics_lines(baseline.get("test", {})),
        "",
        "## Replay of the whole corpus",
        "",
    ]
    accuracy = release_dir / ACCURACY
    if accuracy.exists():
        summary = json.loads(accuracy.read_text())
        if summary.get("scorer_version") != version:
            raise ValueError(f"{accuracy} measured {summary.get('scorer_version')}, not {version}")
        lines += _replay_lines(summary)
    else:
        lines.append(f"Not measured yet: `UNFRET_MODEL_PATH={release_dir / ARTIFACT} make accuracy`.")
    path = release_dir / NOTE
    path.write_text("\n".join(lines) + "\n")
    return path


def set_current(model: str | None, quick: str | None, root: Path | None = None) -> Path:
    """
    Point the store's ``current.json`` at the release a deploy runs.

    :param model: The full scorer's version, or ``None`` for the baseline.
    :param quick: The quick tier's version, or ``None`` for the baseline.
    :param root: The model store; :func:`store` by default.
    :return: The pointer's path.
    :raises ValueError: If a version's artifact is missing, isn't valid or is of the other kind.
    """
    base = root or store()
    for version, want_quick in ((model, False), (quick, True)):
        if version is None:
            continue
        data = json.loads((base / version / ARTIFACT).read_text())
        Model.from_dict(data)  # what the server will load
        if data["version"] != version or ("-quick-" in version) != want_quick:
            kind = "quick tier" if want_quick else "full"
            raise ValueError(f"{base / version / ARTIFACT} holds {data['version']}, not the {kind} scorer {version}")
        if not (base / version / NOTE).exists():
            raise ValueError(f"{version} has no {NOTE}: python -m evals.release note {version}")
    pointer = {"model": model, "model_quick": quick, "updated": datetime.now(UTC).isoformat(timespec="seconds")}
    path = base / CURRENT
    path.write_text(json.dumps(pointer, indent=1) + "\n")
    return path


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    note = commands.add_parser("note", help="(re)write a version's RELEASE.md")
    note.add_argument("version")
    current = commands.add_parser("current", help="point current.json at versions (omitted: the baseline)")
    current.add_argument("--model", default=None, help="the full scorer's version")
    current.add_argument("--quick", default=None, help="the quick tier's version")
    args = parser.parse_args()
    if args.command == "note":
        print(write_note(store() / args.version).read_text())  # noqa: T201 - command-line report
    else:
        print(set_current(args.model, args.quick).read_text())  # noqa: T201 - command-line report


if __name__ == "__main__":
    main()
