import pytest

from unfret import crowd


def test_the_same_page_from_different_links_has_one_key() -> None:
    a = crowd.page_key("s", "https://www.Shop.example/p/lamp/?utm_source=x&b=2&a=1#reviews")
    b = crowd.page_key("s", "http://shop.example/p/lamp?a=1&b=2&fbclid=y")
    assert a == b and len(a) == 32 and a != crowd.page_key("other secret", "https://shop.example/p/lamp?a=1&b=2")


def test_a_token_vouches_for_what_the_result_said_until_it_expires() -> None:
    token = crowd.issue_token(
        "s", "https://shop.example/p", tier="deep", level=2, types=["a", "b", "a"], ttl_s=60, now=0
    )
    assert token is not None
    shown = crowd.check_token("s", token, now=30)
    assert (shown.tier, shown.level, shown.types) == ("deep", 2, ("a", "b"))
    with pytest.raises(crowd.LabelError):
        crowd.check_token("s", token, now=61)
    with pytest.raises(crowd.LabelError):
        crowd.check_token("t", token, now=30)
    assert crowd.issue_token("s", None, tier="deep", level=2, types=[], ttl_s=60) is None


def test_handles_never_pose_as_unfret_or_name_a_scam() -> None:
    assert crowd.valid_handle("lamp_fan") == "lamp_fan"
    for bad in ("ab", "unfret_team", "ScamBuster", "has space"):
        with pytest.raises(crowd.LabelError):
            crowd.valid_handle(bad)
