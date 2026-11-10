"""
The image index with the real model on real catalogue photos (ABO, CC BY 4.0, see tests/fixtures/abo/README.md).

Downloads the pinned model once (about 24 MB) into ``~/.cache/unfret`` (or ``UNFRET_TEST_MODEL_DIR``). The full
measurement, 4,881 held-out queries against a 48,777-photo index, is in ``docs/image-index.json``; these tests check
that the shipped thresholds are the measured ones and still hold on photos of 40 different product types.
"""

import io
import json
import os
import random
from pathlib import Path

import pytest
from PIL import Image

from evals.image_index_eval import _edits
from unfret import image_index
from unfret.image_index import THRESHOLDS, Embedder, ImagePrint

FIXTURES = Path(__file__).parent.parent / "fixtures" / "abo"
REPORT = Path(__file__).parent.parent.parent / "docs" / "image-index.json"
MIN_RECALL = 0.5  # measured: 0.54 on these fixtures and on the 3,187 edited copies of the full run (the report)


@pytest.fixture(scope="module")
def model() -> Embedder:
    directory = Path(os.environ.get("UNFRET_TEST_MODEL_DIR", Path.home() / ".cache" / "unfret"))
    return Embedder(image_index.ensure_model(directory / "dinov2-small-int8.onnx"))


@pytest.fixture(scope="module")
def photos() -> list[Image.Image]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert len(manifest) == 40
    return [image_index.load((FIXTURES / m["file"]).read_bytes(), min_side=64) for m in manifest]


def prints(image: Image.Image, model: Embedder) -> ImagePrint:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return image_index.fingerprint(out.getvalue(), model, min_side=32)


def test_distinct_photos_never_match(model: Embedder, photos: list[Image.Image]) -> None:
    fps = [prints(p, model) for p in photos]
    false = [(i, j) for i in range(len(fps)) for j in range(i + 1, len(fps)) if image_index.same_photo(fps[i], fps[j])]
    assert false == []  # 780 pairs of different products


def test_edited_copies_still_match(model: Embedder, photos: list[Image.Image]) -> None:
    rng = random.Random(1)
    hits: dict[str, list[bool]] = {}
    for photo in photos:
        base = prints(photo, model)
        for name, edited in _edits(photo, rng).items():
            try:
                copy = prints(edited, model)
            except image_index.NotDiscriminativeError:
                continue  # a wide photo cropped into a banner: not indexed either
            hits.setdefault(name, []).append(image_index.same_photo(base, copy))
    recall = sum(sum(v) for v in hits.values()) / sum(len(v) for v in hits.values())
    per_edit = {k: round(sum(v) / len(v), 2) for k, v in hits.items()}
    print(f"recall {recall:.3f} per edit {per_edit}")  # noqa: T201 - shown in the CI log
    assert recall >= MIN_RECALL, per_edit


def test_shipped_thresholds_are_the_measured_ones() -> None:
    report = json.loads(REPORT.read_text())
    chosen = report["chosen"]
    assert (THRESHOLDS.phash, THRESHOLDS.dhash, THRESHOLDS.cosine) == (
        chosen["phash"],
        chosen["dhash"],
        chosen["cosine"],
    )
    assert report["queries"] >= 4_800 and report["index_size"] >= 48_000  # after the filters
    assert chosen["false_queries"] == 0
    assert report["per_query_fmr_95_upper"] < 0.001  # under 0.1% per query, at 95% confidence
