"""Top-ups: what an amount buys, the bonus tiers, and how it reads."""

from decimal import Decimal

import pytest

from unfret.config import Settings, settings
from unfret.credits import topup


@pytest.mark.parametrize(
    ("amount", "checks", "bonus"),
    [
        ("10", 100, 0),
        ("10.05", 100, 0),  # a part of a check buys nothing
        ("49.99", 499, 0),
        ("50", 550, 50),  # +10% from 50
        ("99.99", 1098, 99),
        ("100", 1150, 150),  # +15% from 100
        ("250", 2875, 375),
    ],
)
def test_checks_are_the_amount_over_the_price_plus_the_tiers_bonus(amount: str, checks: int, bonus: int) -> None:
    for currency in ("usd", "eur"):
        top = topup.topup(settings, Decimal(amount), currency)
        assert (top.checks, top.bonus_checks) == (checks, bonus)
        assert top.credited_usd_micros(settings) == checks * 100_000  # $0.10 a check in credits, whatever paid


def test_the_minimum_and_whole_cents() -> None:
    assert topup.valid(settings, Decimal(10)) and not topup.valid(settings, Decimal("9.99"))
    assert not topup.valid(settings, Decimal("10.001"))


def test_the_line_shows_the_vat_once_the_country_is_known() -> None:
    top = topup.topup(settings, Decimal(100), "eur")
    assert topup.label(top) == "€100.00 → 1,150 checks (incl. 150 bonus)"
    assert (
        topup.label(top, Decimal(21), "NL") == "€100.00 → 1,150 checks (incl. 150 bonus) · €121.00 incl. 21% VAT (NL)"
    )


def test_card_and_crypto_credits() -> None:
    assert topup.card_credits(settings, 10000, "eur") == 1150 * 100_000
    assert topup.card_credits(settings, 600, "gbp") == 0
    assert topup.crypto_credits(settings, 50_000_000) == 550 * 100_000


def test_the_bonus_tiers_are_a_setting() -> None:
    custom = Settings(_env_file=None, topup_bonus="20=5")  # type: ignore[call-arg]
    assert topup.topup(custom, Decimal(20)).bonus_checks == 10
    with pytest.raises(ValueError, match="topup_bonus"):
        Settings(_env_file=None, topup_bonus="lots")  # type: ignore[call-arg]


def test_euro_area_visitors_pay_in_euros() -> None:
    assert topup.card_currency(settings, "NL") == "eur" and topup.card_currency(settings, "US") == "usd"
