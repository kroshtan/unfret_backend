"""The image seed command: the hash is checked before anything is read, and seed rows match Amazon listings."""

from pathlib import Path

import pytest

from unfret import image_seed, image_store
from unfret.facts import PageFacts


async def test_a_file_with_the_wrong_hash_is_refused_before_anything_is_read(tmp_path: Path) -> None:
    seed = tmp_path / "seed.npz"
    seed.write_bytes(b"not the published seed")
    with pytest.raises(ValueError, match="expected SHA-256"):
        await image_seed.import_seed(None, seed, "abo", expected_sha256="00" * 32)


def test_the_command_reports_a_hash_mismatch_without_a_traceback(tmp_path: Path) -> None:
    seed = tmp_path / "seed.npz"
    seed.write_bytes(b"x")
    with pytest.raises(SystemExit, match="expected SHA-256"):
        image_seed.main(["import", str(seed), "--source", "abo", "--sha256", "11" * 32])


def test_unknown_sources_are_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        image_seed.main(["remove", "--source", "temu"])


def test_sha256_of_a_file(tmp_path: Path) -> None:
    seed = tmp_path / "seed.npz"
    seed.write_bytes(b"abc")
    assert image_seed.sha256_of(seed) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_seed_rows_share_the_listing_key_of_the_amazon_page() -> None:
    page = image_store.listing("https://www.amazon.de/dp/B06X9STHNG", PageFacts(platform="amazon"))
    assert page is not None and page.listing_key == image_seed.listing_key("b06x9sthng")
