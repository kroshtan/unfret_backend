"""The nickname word filter: offensive words refused, look-alike digits included; ordinary nicknames pass."""

import pytest

from unfret import crowd, nicknames


@pytest.mark.parametrize("handle", ["shit_happens", "SH1T", "f-u-c-k", "b1tch_x", "N4zi_fan", "kanker_x", "tyfus"])
def test_offensive_nicknames_are_refused(handle: str) -> None:
    assert nicknames.offensive(handle)
    with pytest.raises(crowd.LabelError):
        crowd.valid_handle(handle)


@pytest.mark.parametrize("handle", ["lamp_fan", "deal-checker", "Grandma_Jo", "skillful", "spicy_tuna", "Sussex"])
def test_ordinary_nicknames_pass(handle: str) -> None:
    assert not nicknames.offensive(handle)
    assert crowd.valid_handle(handle) == handle
