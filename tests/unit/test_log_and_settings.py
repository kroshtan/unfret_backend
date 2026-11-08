"""Logs carry no page or visitor; settings that contradict each other are refused at start-up."""

import pytest
from pydantic import SecretStr, ValidationError

from veridex.config import Settings
from veridex.log import redact, scrub


def test_urls_and_ips_never_reach_the_log() -> None:
    event = {
        "event": "check_page_fetch_failed",
        "url": "https://shop.test/p/1",
        "ip": "203.0.113.9",
        "detail": "GET https://shop.test/p/1?q=x from 203.0.113.9 and 2001:db8::1 failed",
        "issues": ["system transfer to Abc1"],
        "signature": "5ig",
    }
    out = redact(None, "info", event)
    assert "url" not in out and "ip" not in out
    assert out["detail"] == "GET <url> from <ip> and <ip> failed"
    assert out["issues"] == ["system transfer to Abc1"] and out["signature"] == "5ig"
    assert scrub("nothing here") == "nothing here"
    assert scrub("retry at 21:06:17") == "retry at 21:06:17"
    assert scrub("from fe80::1%eth0 and 2001:db8:0:0:1:2:3:4") == "from <ip>%eth0 and <ip>"


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"purchase_min_usd_micros": 1}, "purchase_min"),
        ({"credit_bonus_tiers": [(25_000_000, 20), (5_000_000, 10)]}, "ascend"),
        ({"quote_tolerance_bps": 5_000}, "quote_tolerance_bps"),
        ({"x402_price_quick_usd_micros": 0}, "positive"),
        ({"sweep_reserve_lamports": 200_000_000}, "sweep_reserve_lamports"),
    ],
)
def test_contradictory_settings_are_refused(overrides: dict[str, object], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        Settings(**overrides)  # type: ignore[arg-type]


def test_the_summary_has_no_secrets() -> None:
    summary = Settings(session_secret=SecretStr("s3cret-value"), admin_token=SecretStr("t0ken-value")).summary()
    assert summary["sign_in"] is True and summary["admin_token"] is True
    assert "s3cret-value" not in str(summary) and "t0ken-value" not in str(summary)
