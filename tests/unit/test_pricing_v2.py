"""Pricing v2: every price derives from one setting; packs by card and by crypto; VAT rules for crypto purchases."""

from datetime import date
from decimal import Decimal

import pytest

from tests.unit.x402_helpers import StaticOracle
from unfret.config import Settings
from unfret.credits import vat
from unfret.credits.packs import (
    EURO_AREA,
    card_credits,
    card_currency,
    card_price,
    euros_on,
    format_eur,
    pack,
    pack_for_card,
    packs,
)
from unfret.credits.pricing import pricing
from unfret.prices import PriceBook
from unfret.x402.solana import USDC_MINTS


def test_the_packs_at_twenty_cents() -> None:
    s = Settings()
    assert [(p.checks, p.value_usd_micros, p.card_usd_micros) for p in packs(s)] == [
        (25, 5_000_000, 6_000_000),  # $5 with crypto, $6 by card
        (100, 20_000_000, 20_000_000),  # $20 either way
    ]
    assert pack(s, 25) is not None and pack(s, 100) is not None
    assert pack(s, 50) is None and pack(s, 10) is None  # the 50-pack is off sale


def test_card_prices_set_by_hand_and_derived_for_the_rest() -> None:
    assert Settings().card_usd_cents == {25: 600, 100: 2000}
    s = Settings(packs=[25, 50, 100], card_prices_usd="25=6,100=20.00", card_prices_eur="")
    # The 50-pack has no price of its own: value plus the provider's cut, rounded up to $0.50.
    assert [p.card_usd_micros for p in packs(s)] == [6_000_000, 11_500_000, 20_000_000]


@pytest.mark.parametrize("bad", ["25=6,25=7", "25=x", "25=0", "25=6.001", "100"])
def test_malformed_dollar_prices_are_refused_at_start_up(bad: str) -> None:
    with pytest.raises(ValueError, match="card_prices_usd"):
        Settings(card_prices_usd=bad)


def test_a_card_price_below_the_packs_value_is_refused() -> None:
    # Crypto must never be the dearer way to pay: $20 for 100 checks at $0.21 each would be ($21).
    with pytest.raises(ValueError, match=r"below the value of the packs \[100\]"):
        Settings(price_per_check_usd=Decimal("0.21"))


def test_the_derived_card_price_covers_the_providers_worst_case_cut() -> None:
    s = Settings(card_prices_usd="")
    assert [p.card_usd_micros for p in packs(s)] == [6_000_000, 22_000_000]
    for p in packs(s):
        kept = p.card_usd_micros * (1 - Decimal("0.065")) - 500_000
        assert kept >= p.value_usd_micros  # at 6.5% + $0.50, still at least the pack's value
        assert p.card_usd_micros % 500_000 == 0  # rounded up to $0.50
    assert card_price(5_000_000, Settings(card_fee_pct=Decimal(0), card_fee_flat_usd=Decimal(0))) == 5_000_000


def test_a_card_payment_buys_its_packs_value() -> None:
    s = Settings()
    assert [card_credits(s, cents) for cents in (600, 2000)] == [5_000_000, 20_000_000]
    # The old prices of the 100- and 50-packs aren't packs any more: only the whole checks they cover.
    assert card_credits(s, 2200) == 20_000_000 and card_credits(s, 1150) == 10_200_000
    # An amount that matches no pack buys only the whole checks it covers after the provider's cut.
    assert card_credits(s, 1000) == 8_800_000  # $10 → $8.85 kept → 44 checks
    assert card_credits(s, 50) == 0


def test_euro_card_prices_buy_the_same_packs() -> None:
    s = Settings()
    assert [(p.checks, p.card_eur_cents) for p in packs(s)] == [(25, 550), (100, 1750)]
    assert [card_credits(s, cents, "eur") for cents in (550, 1750)] == [5_000_000, 20_000_000]
    assert card_credits(s, 1000, "eur") == card_credits(s, 1900, "eur") == 0  # the old 50 and 100 euro prices
    assert [p.checks if p else None for p in (pack_for_card(s, 550, "eur"), pack_for_card(s, 550))] == [25, None]
    # No exchange rate here: a euro amount that is no pack's euro price buys nothing (a person looks at it).
    assert card_credits(s, 600, "eur") == card_credits(s, 2000, "eur") == 0
    assert card_credits(s, 550, "gbp") == 0 and pack_for_card(s, 600, "gbp") is None
    assert format_eur(550) == "€5.50" and format_eur(1750) == "€17.50" and format_eur(5) == "€0.05"


@pytest.mark.parametrize(
    ("country", "currency"),
    [("NL", "eur"), ("DE", "eur"), ("BG", "eur"), ("GR", "eur"), ("US", "usd"), ("DK", "usd"), ("GB", "usd")]
    + [(None, "usd")],
)
def test_the_euro_area_pays_by_card_in_euros(country: str | None, currency: str) -> None:
    assert card_currency(Settings(), country) == currency
    assert card_currency(Settings(card_prices_eur=""), country) == "usd"  # euro prices off: dollars for everyone
    assert len(EURO_AREA) == 21


def test_euro_prices_setting() -> None:
    s = Settings(card_prices_eur=" 25=6 , 50=10.5,100=20.00 ")
    assert s.card_eur_cents == {25: 600, 50: 1050, 100: 2000}  # a price for a pack off sale is ignored
    assert [p.card_eur_cents for p in packs(s)] == [600, 2000]
    assert Settings(card_prices_eur="").card_eur_cents == {} and not euros_on(Settings(card_prices_eur=""))
    assert euros_on(Settings())


@pytest.mark.parametrize(
    "bad",
    ["25=5.50", "100=17.50,200=30", "25=5.555,50=10,100=19", "25=0,50=10,100=19", "25=x,50=10,100=19"]
    + ["25=5.50,25=6,50=10,100=19", "25,50=10,100=19", "25=-1,50=10,100=19", "25=NaN,50=10,100=19"],
)
def test_malformed_or_partial_euro_prices_are_refused_at_start_up(bad: str) -> None:
    with pytest.raises(ValueError, match="card_prices_eur"):
        Settings(card_prices_eur=bad)


async def test_the_price_list_has_the_euro_card_prices_next_to_the_dollar_ones() -> None:
    s = Settings()
    doc = await pricing(s, None, PriceBook(s, StaticOracle("100")), USDC_MINTS["solana-devnet"])
    assert [(p["price_usd"], p["price_eur"], p["price_eur_cents"]) for p in doc["packs"]] == [
        ("$6.00", "€5.50", 550),
        ("$20.00", "€17.50", 1750),
    ]
    off = Settings(card_prices_eur="")
    doc = await pricing(off, None, PriceBook(off, StaticOracle("100")), USDC_MINTS["solana-devnet"])
    assert {(p["price_eur"], p["price_eur_cents"]) for p in doc["packs"]} == {(None, None)}


@pytest.mark.parametrize("price", ["0.20", "0.25", "0.10"])
async def test_one_setting_moves_every_price(price: str) -> None:
    s = Settings(price_per_check_usd=Decimal(price), card_prices_usd="")  # the card prices derived, too
    check = int(Decimal(price) * 1_000_000)
    book = PriceBook(s, StaticOracle("100"))
    assert s.analysis_price_usd_micros == s.x402_price_deep_usd_micros == check
    assert book.usd("analysis") == book.usd("x402_deep") == check
    assert book.usd("x402_quick") == 10_000  # the quick tier has its own price
    assert [p.value_usd_micros for p in packs(s)] == [25 * check, 100 * check]
    assert [p.card_usd_micros for p in packs(s)] == [card_price(n * check, s) for n in (25, 100)]
    doc = await pricing(s, None, book, USDC_MINTS["solana-devnet"])
    assert doc["price_per_check_usd_micros"] == doc["analysis_usd_micros"] == check
    assert doc["x402"]["deep_usd_micros"] == doc["usdc"]["x402"]["deep_units"] == check
    assert [p["crypto_usd_micros"] for p in doc["packs"]] == [25 * check, 100 * check]
    assert doc["sol"]["analysis_lamports"] == check * 10  # at $100/SOL


@pytest.mark.parametrize(
    ("value", "code"),
    [("nl", "NL"), (" De ", "DE"), ("EL", "GR"), ("XX", None), ("T1", None), ("N1", None), ("NLD", None), ("", None)],
)
def test_country_codes(value: str, code: str | None) -> None:
    assert vat.normalise_country(value) == code


def test_rates_under_the_threshold_and_per_country() -> None:
    nl = Settings()
    assert [vat.rate_bps(nl, c) for c in ("NL", "DE", "HU", "US", "GB", vat.UNKNOWN)] == [2100, 2100, 2100, 0, 0, 2100]
    each = Settings(vat_mode="per_country")
    assert [vat.rate_bps(each, c) for c in ("NL", "DE", "HU", "FI", "US", vat.UNKNOWN)] == [
        2100,
        1900,
        2700,
        2550,
        0,
        2100,
    ]
    assert len(vat.standard_rates()) == 27


def test_the_vendored_tables_are_complete_and_recent_enough_to_ship() -> None:
    assert (date.today() - vat.rates_checked_on()).days < 400  # refreshed by `make vat-rates-check` monthly


def test_cloudflares_country_only_from_cloudflares_edge() -> None:
    assert vat.edge_country({"x-edge-peer": "172.64.0.1", "cf-ipcountry": "be"}) == "BE"
    assert vat.edge_country({"x-edge-peer": "2606:4700::1", "cf-ipcountry": "US"}) == "US"
    assert vat.edge_country({"x-edge-peer": "203.0.113.5", "cf-ipcountry": "BE"}) is None  # not Cloudflare
    assert vat.edge_country({"cf-ipcountry": "BE"}) is None  # no edge peer: can't tell
    assert vat.edge_country({"x-edge-peer": "172.64.0.1", "cf-ipcountry": "XX"}) is None


def test_quotes_and_splits() -> None:
    s = Settings()
    quote = vat.price_quote(s, 25, 5_000_000, "BE", edge="NL", beneficiary=None)
    assert (quote.vat_rate_bps, quote.vat_usd_micros, quote.gross_usd_micros) == (2100, 1_050_000, 6_050_000)
    assert quote.country_mismatch and len(quote.id) == 20 and quote.memo == f"unfret:quote:{quote.id}"
    assert not vat.price_quote(s, 25, 5_000_000, "BE", edge=None, beneficiary=None).country_mismatch
    assert vat.split_gross(6_050_000, 2100) == (5_000_000, 1_050_000)
    assert vat.split_gross(1, 2100) == (0, 1)  # dust credits nothing
    # A payment that arrives a little short of its quote (SOL moved) is split at the quote's rate.
    sale = vat.sale_for(s, 6_000_000, quote)
    assert (sale.country, sale.country_mismatch, sale.net_usd_micros + sale.vat_usd_micros) == ("BE", True, 6_000_000)
    unknown = vat.sale_for(s, 6_050_000, None)
    assert (unknown.country, unknown.vat_rate_bps, unknown.net_usd_micros) == ("unknown", 2100, 5_000_000)


@pytest.mark.parametrize(
    ("quarter", "bounds"),
    [
        ("2026-Q1", (date(2026, 1, 1), date(2026, 3, 31))),
        ("2026-q4", (date(2026, 10, 1), date(2026, 12, 31))),
    ],
)
def test_quarters(quarter: str, bounds: tuple[date, date]) -> None:
    assert vat.quarter_bounds(quarter) == bounds


@pytest.mark.parametrize("bad", ["2026-Q5", "2026-Q0", "2026", "Q1-2026"])
def test_bad_quarters(bad: str) -> None:
    with pytest.raises(ValueError):
        vat.quarter_bounds(bad)
