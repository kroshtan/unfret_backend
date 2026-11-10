"""
A fixed, seeded train/test split, recorded per item as ``split`` in ``labels.jsonl``.

The corpus is also training data for the feature scorer, so the test set must measure something real:

- Only items whose label came from people or was independently confirmed (``dataset``, ``agreed``, ``human``)
  can be in **test**. Items labelled purely by a rule always go to **train**, because a scorer would learn the
  rule back and test metrics on them would be meaningless.
- Within each label × source stratum, eligible items are ordered by a seeded hash of their id and the first
  ``test_fraction`` go to test, so the split is stratified, reproducible, and independent of file order.
- Unlabelled items get no split.

Re-run after labels change (``make split``); the result depends only on the labels and the seed.

Usage: ``uv run python -m evals.split [--test-fraction 0.3] [--seed veridex-2026]``.
"""

import argparse
import hashlib
from collections import Counter, defaultdict

import structlog

from evals.corpus import Corpus, LabelRecord, dump_json

logger = structlog.get_logger("unfret.evals")

TEST_ELIGIBLE: frozenset[str] = frozenset({"dataset", "agreed", "human"})
# From before the rename to Unfret, kept: another seed would reshuffle the test set the shipped scorers were
# measured on.
DEFAULT_SEED = "veridex-2026"


def _order(seed: str, record_id: str) -> str:
    return hashlib.sha256(f"{seed}:{record_id}".encode()).hexdigest()


def assign_splits(records: list[LabelRecord], test_fraction: float, seed: str) -> list[LabelRecord]:
    """
    Assign every labelled record to train or test.

    :param records: All corpus records.
    :param test_fraction: Share of each stratum's test-eligible items that go to test.
    :param seed: Seed for the ordering hash.
    :return: The records with ``split`` set (``None`` for unlabelled ones).
    """
    strata: dict[tuple[str, str], list[LabelRecord]] = defaultdict(list)
    out: dict[str, LabelRecord] = {}
    for r in records:
        if r.label is None:
            out[r.id] = r.model_copy(update={"split": None})
        elif r.label_source in TEST_ELIGIBLE:
            strata[(r.label, r.source)].append(r)
        else:
            out[r.id] = r.model_copy(update={"split": "train"})
    for members in strata.values():
        members.sort(key=lambda r: _order(seed, r.id))
        n_test = round(test_fraction * len(members))
        for i, r in enumerate(members):
            out[r.id] = r.model_copy(update={"split": "test" if i < n_test else "train"})
    return [out[r.id] for r in records]


def split_counts(records: list[LabelRecord]) -> dict[str, int]:
    """Counts per ``split/label/label_source/source``, for reports."""
    counts = Counter(f"{r.split}/{r.label}/{r.label_source}/{r.source}" for r in records)
    return dict(sorted(counts.items()))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--test-fraction", type=float, default=0.3)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    args = parser.parse_args()
    corpus = Corpus()
    with corpus.locked():
        records = assign_splits(list(corpus.labels().values()), args.test_fraction, args.seed)
        corpus.write_labels(records)
    counts = split_counts(records)
    (corpus.root / "reports").mkdir(exist_ok=True)
    dump_json(
        corpus.root / "reports" / "split.json", {"seed": args.seed, "test_fraction": args.test_fraction, **counts}
    )
    logger.info("split_done", **Counter(str(r.split) for r in records))


if __name__ == "__main__":
    main()
