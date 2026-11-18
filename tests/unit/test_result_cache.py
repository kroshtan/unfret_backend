"""The result cache's note of who paid for a listing: per account, for the hour, and naming neither."""

import pytest

from unfret import result_cache


@pytest.fixture(autouse=True)
def empty() -> None:
    result_cache.clear()


def test_a_paid_listing_is_noted_per_account() -> None:
    result_cache.mark_paid("key:abc", "https://shop.test/sweater?utm_source=mail")

    assert result_cache.paid_for("key:abc", "https://SHOP.test/sweater/#reviews")  # the same listing
    assert not result_cache.paid_for("key:other", "https://shop.test/sweater")
    assert not result_cache.paid_for("key:abc", "https://shop.test/scarf")


def test_the_note_expires_and_needs_a_url() -> None:
    result_cache.mark_paid("key:abc", "https://shop.test/sweater", ttl_s=-1)
    result_cache.mark_paid("key:abc", "  ")

    assert not result_cache.paid_for("key:abc", "https://shop.test/sweater")
    assert not result_cache.paid_for("key:abc", "")
    assert not result_cache._paid


def test_the_note_names_neither_the_account_nor_the_page() -> None:
    # Not hex, so the random key's digest can't contain it by chance (as "abc" sometimes did).
    result_cache.mark_paid("key:pass-xyz", "https://shop.test/sweater")

    (key,) = result_cache._paid
    assert "xyz" not in key and "shop" not in key and len(key) == 64
