"""The operator's ``make codes list`` text."""

from unfret.credits.codes import describe

ACCOUNTS = {"via_code": 3, "via_code_bought": 1, "organic_bought": 2}
CODE = {"code": "FRIENDS", "checks": 5, "uses": 3, "max_uses": 40, "active": True}


def test_list_says_no_daily_cap_when_it_is_off() -> None:
    text = describe({"codes": [CODE], "today": {"checks_given": 15, "cap": None}, "accounts": ACCOUNTS})
    assert "today: 15 free checks given (UTC day), no daily cap" in text
    assert "FRIENDS" in text
    assert "not refunded in full or disputed" in text  # what a buyer is


def test_list_shows_the_daily_cap_when_set() -> None:
    text = describe({"codes": [], "today": {"checks_given": 15, "cap": 100}, "accounts": ACCOUNTS})
    assert "today: 15 of 100 free checks given (UTC day)" in text
    assert "(no codes yet)" in text
