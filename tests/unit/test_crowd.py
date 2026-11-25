import pytest

from unfret import crowd


def test_a_token_vouches_for_what_the_result_said_until_it_expires() -> None:
    token = crowd.issue_token("s", "abc", tier="deep", score=30, level=3, types=["a", "b", "a"], ttl_s=60, now=0)
    assert token is not None
    shown = crowd.check_token("s", token, now=30)
    assert (shown.input_hash, shown.tier, shown.score, shown.level, shown.types) == ("abc", "deep", 30, 3, ("a", "b"))
    with pytest.raises(crowd.LabelError):
        crowd.check_token("s", token, now=61)
    with pytest.raises(crowd.LabelError):
        crowd.check_token("t", token, now=30)
    assert crowd.issue_token("s", "abc", tier="deep", score=-1, level=0, types=[], ttl_s=60) is None


def test_a_token_from_before_the_merged_flow_is_refused() -> None:
    payload = "v2|0123|deep|4|a|60"
    token = crowd.base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + crowd._mac("s", payload)
    with pytest.raises(crowd.LabelError):
        crowd.check_token("s", token, now=0)


def test_handles_never_pose_as_unfret_or_name_a_scam() -> None:
    assert crowd.valid_handle("lamp_fan") == "lamp_fan"
    for bad in ("ab", "unfret_team", "ScamBuster", "has space"):
        with pytest.raises(crowd.LabelError):
            crowd.valid_handle(bad)
