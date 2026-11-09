"""Session tokens with odd bytes (#11) and the per-visitor limits by network (L2)."""

import pytest

from veridex.credits import siws
from veridex.credits.pairing import PairingStore
from veridex.visitors import DailyLimit

SECRET = "s" * 32


@pytest.mark.parametrize("token", ["é.é", "abc.ü", "x." + "∑" * 64, "."])
def test_non_ascii_tokens_are_refused_not_crashed_on(token: str) -> None:
    with pytest.raises(siws.SignInError):
        siws.check_token(SECRET, token)


def test_a_non_ascii_nonce_is_refused() -> None:
    nonce, _ = siws.issue_nonce(SECRET, 300)
    with pytest.raises(siws.SignInError):
        siws.check_nonce(SECRET, nonce[:-1] + "é")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_pairings_per_visitor_count_an_ipv6_64_once() -> None:
    store = PairingStore(per_ip=2, clock=Clock())
    assert store.create("2001:db8:1:2::1") and store.create("2001:db8:1:2::ffff")
    assert store.create("2001:db8:1:2:abcd::1") is None  # same /64
    assert store.create("2001:db8:1:3::1")


def test_expired_pairings_dont_fill_the_store() -> None:
    clock = Clock()
    store = PairingStore(ttl_s=60, max_pairings=2, per_ip=100, clock=clock)
    assert store.create("1.1.1.1") and store.create("1.1.1.2")
    assert store.create("1.1.1.3") is None  # full
    clock.now += 61  # expired, though still kept for the grace period
    assert store.create("1.1.1.4")


def test_daily_limits_count_an_ipv6_64_once() -> None:
    limiter = DailyLimit(per_ip_per_day=1, daily_cap=10, clock=Clock())
    assert limiter.allow("2001:db8::1")
    assert not limiter.allow("2001:db8::2")
    assert limiter.allow("2001:db9::1")


def test_daily_limit_per_visitor_and_daily_cap() -> None:
    now = [1_800_000_000.0]
    limiter = DailyLimit(per_ip_per_day=2, daily_cap=3, clock=lambda: now[0])
    assert limiter.allow("1.1.1.1") and limiter.allow("1.1.1.1")
    assert not limiter.allow("1.1.1.1")
    assert limiter.allow("2.2.2.2")
    assert not limiter.allow("3.3.3.3")  # daily cap
    now[0] += 86_400 + 1
    assert limiter.allow("1.1.1.1") and limiter.allow("3.3.3.3")
    assert all(len(k) == 24 for k in limiter._hits)  # salted hashes, never the addresses
