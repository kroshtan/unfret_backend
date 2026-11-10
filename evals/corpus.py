"""
The corpus on disk: ``pages/<id>.html``, ``labels.jsonl`` and ``review_queue.jsonl``.

Every page has exactly one record in ``labels.jsonl``. Records are rewritten in full on update, which is fine at
corpus sizes of a few thousand pages and keeps the files diff-friendly in the corpus's own git repository.
"""

import fcntl
import hashlib
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from evals.config import eval_settings

Label = Literal["legit", "junk", "scam"]
LabelSource = Literal["dataset", "rule", "llm", "agreed", "human"]
Split = Literal["train", "test"]
UNRELIABLE: frozenset[str] = frozenset({"junk", "scam"})


class LabelRecord(BaseModel):
    """Ground truth and provenance for one corpus page."""

    id: str
    label: Label | None = Field(default=None, description="None while no source is confident enough.")
    label_source: LabelSource | None = None
    confidence: float = 0.0
    platform: str = Field(default="", description="Marketplace name, or empty for a brand's own shop.")
    page_type: str = Field(default="", description="Expected page type: storefront or aggregate_listing.")
    source: str = Field(description="How the HTML was obtained: dataset, wayback or synthetic.")
    origin_dataset: str
    origin_url: str = ""
    origin_id: str = ""
    collected_at: str = ""
    notes: str = ""
    # Second opinion, kept alongside the final label so agreement can be recomputed.
    prior_label: Label | None = Field(default=None, description="The dataset or rule label before review.")
    llm_label: Label | None = None
    llm_confidence: float = 0.0
    llm_reason: str = ""
    # Training support for the feature scorer.
    split: Split | None = Field(default=None, description="Fixed, seeded train/test split; None while unlabelled.")
    rule_features: list[str] = Field(
        default_factory=list, description="Features a labelling rule used; exclude or check them when training."
    )


class QueueItem(BaseModel):
    """A page that needs a human decision, and why."""

    id: str
    reason: str
    priority: float


def page_id(prefix: str, key: str) -> str:
    """Stable page id from a source prefix and the page's origin URL or ASIN."""
    return f"{prefix}-{hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:12]}"


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Corpus:
    """Read and write access to a corpus directory."""

    def __init__(self, root: Path | None = None) -> None:
        """
        Open (and create if needed) a corpus directory.

        :param root: The corpus directory; defaults to ``UNFRET_CORPUS_DIR``.
        """
        self.root = root or eval_settings.corpus_dir
        self.pages_dir = self.root / "pages"
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        self.labels_path = self.root / "labels.jsonl"
        self.queue_path = self.root / "review_queue.jsonl"
        self._lock_path = self.root / ".labels.lock"
        self.features_path = self.root / "features.jsonl"

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold an exclusive lock on the label files, so concurrent source scripts don't overwrite each other."""
        with self._lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def page_path(self, record_id: str) -> Path:
        """Path of a page's HTML file."""
        return self.pages_dir / f"{record_id}.html"

    def read_page(self, record_id: str) -> str:
        """Return a page's HTML."""
        return self.page_path(record_id).read_text(encoding="utf-8", errors="replace")

    def labels(self) -> dict[str, LabelRecord]:
        """All label records, keyed by id."""
        if not self.labels_path.exists():
            return {}
        records = (LabelRecord.model_validate_json(line) for line in self.labels_path.read_text().splitlines())
        return {r.id: r for r in records}

    def write_labels(self, records: Iterable[LabelRecord]) -> None:
        """Replace ``labels.jsonl`` with ``records``, sorted by id."""
        lines = [r.model_dump_json() for r in sorted(records, key=lambda r: r.id)]
        tmp = self.labels_path.with_suffix(".tmp")
        tmp.write_text("".join(f"{line}\n" for line in lines))
        tmp.replace(self.labels_path)

    def add(self, record: LabelRecord, html: str) -> None:
        """
        Store a page and its record, replacing an earlier version with the same id.

        :param record: The page's label record.
        :param html: The page's HTML.
        """
        self.add_many([(record, html)])

    def add_many(self, items: Iterable[tuple[LabelRecord, str]]) -> int:
        """
        Store several pages with one rewrite of ``labels.jsonl``.

        :param items: Pairs of label record and HTML.
        :return: The number of pages stored.
        """
        pairs = list(items)
        for record, html in pairs:
            self.page_path(record.id).write_text(html, encoding="utf-8")
        with self.locked():
            records = self.labels()
            records.update((record.id, record) for record, _ in pairs)
            self.write_labels(records.values())
        return len(pairs)

    def update(self, records: Iterable[LabelRecord]) -> None:
        """
        Replace existing records (matched by id) under the lock, leaving all others untouched.

        :param records: The updated records.
        """
        with self.locked():
            current = self.labels()
            current.update((r.id, r) for r in records)
            self.write_labels(current.values())

    def features(self) -> dict[str, dict[str, object]]:
        """Computed per-item features (``features.jsonl``), keyed by id."""
        if not self.features_path.exists():
            return {}
        rows = (json.loads(line) for line in self.features_path.read_text().splitlines())
        return {str(row["id"]): row["features"] for row in rows}

    def write_features(self, features: dict[str, dict[str, object]]) -> None:
        """
        Merge ``features`` into ``features.jsonl`` (replacing items with the same id).

        :param features: Feature dicts keyed by item id.
        """
        with self.locked():
            merged = {**self.features(), **features}
            lines = (json.dumps({"id": k, "features": v}, sort_keys=True) for k, v in sorted(merged.items()))
            self.features_path.write_text("".join(f"{line}\n" for line in lines))

    def queue(self) -> list[QueueItem]:
        """The review queue, highest priority first."""
        if not self.queue_path.exists():
            return []
        items = [QueueItem.model_validate_json(line) for line in self.queue_path.read_text().splitlines()]
        return sorted(items, key=lambda i: -i.priority)

    def write_queue(self, items: Iterable[QueueItem]) -> None:
        """Replace the review queue."""
        lines = [i.model_dump_json() for i in sorted(items, key=lambda i: -i.priority)]
        self.queue_path.write_text("".join(f"{line}\n" for line in lines))


def dump_json(path: Path, data: object) -> None:
    """Write ``data`` as indented JSON."""
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
