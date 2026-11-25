"""
Topping up a pass: the buyer names an amount, the checks follow from it.

``checks = floor(amount ÷ price per check)``, the amount **before VAT**, plus a bonus for larger amounts
(``topup_bonus``: +10% from 50, +15% from 100, of the checks the amount buys). The price per check is
``price_per_check_usd`` in dollars and ``price_per_check_eur`` in euros (visitors from the euro area pay in euros:
:func:`card_currency`); the least amount is ``topup_min_amount`` (10). VAT is added on top at checkout: by the card
provider (Polar, the merchant of record) for card payments, as a quote's rate for crypto (:mod:`unfret.credits.vat`).
Payment fees are absorbed, never taken off the checks.

A pass's balance stays in micro-dollars: a top-up credits its checks at ``price_per_check_usd`` each, whatever
currency paid, so a check costs the same in credits however it was bought.
"""

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

from unfret.config import Settings

USD_MICROS = 1_000_000

CardCurrency = Literal["usd", "eur"]
CARD_CURRENCIES: tuple[CardCurrency, ...] = ("usd", "eur")
SYMBOL = {"usd": "$", "eur": "€"}

# The euro area (the EU members that use the euro; Bulgaria since 1 January 2026), as ISO 3166 alpha-2 codes.
EURO_AREA = frozenset(
    [
        "AT",
        "BE",
        "BG",
        "CY",
        "DE",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PT",
        "SI",
        "SK",
    ]  # 21 countries
)


def micros(dollars: Decimal) -> int:
    """
    Dollars as integer micro-dollars.

    :param dollars: The amount (at most six decimals).
    :return: Micro-dollars.
    """
    return int(dollars * USD_MICROS)


def bonus_tiers(settings: Settings) -> list[tuple[Decimal, int]]:
    """
    The bonus tiers, smallest amount first: ``"50=10,100=15"`` → ``[(50, 10), (100, 15)]``.

    :param settings: The service settings.
    :return: ``(from amount, percent)`` pairs.
    :raises ValueError: If the setting is malformed.
    """
    tiers: list[tuple[Decimal, int]] = []
    for part in filter(None, (p.strip() for p in settings.topup_bonus.split(","))):
        amount, sep, pct = part.partition("=")
        if not sep or not pct.strip().isdigit():
            raise ValueError("topup_bonus must read like 50=10,100=15")
        tiers.append((Decimal(amount.strip()), int(pct)))
    return sorted(tiers)


def price_per_check(settings: Settings, currency: str) -> Decimal:
    """
    A check's price in a currency, before VAT.

    :param settings: The service settings.
    :param currency: ``usd`` or ``eur``.
    :return: The price.
    """
    return settings.price_per_check_eur if currency == "eur" else settings.price_per_check_usd


@dataclass(frozen=True)
class Topup:
    """What an amount buys."""

    currency: CardCurrency
    amount: Decimal  # before VAT, in the currency
    base_checks: int
    bonus_pct: int
    bonus_checks: int

    @property
    def checks(self) -> int:
        """All the checks: the amount's and the bonus."""
        return self.base_checks + self.bonus_checks

    @property
    def amount_cents(self) -> int:
        """The amount before VAT in the currency's cents."""
        return int(self.amount * 100)

    def credited_usd_micros(self, settings: Settings) -> int:
        """
        What a pass is credited: its checks at the dollar price of a check.

        :param settings: The service settings.
        :return: Micro-dollars.
        """
        return self.checks * settings.analysis_price_usd_micros


def topup(settings: Settings, amount: Decimal, currency: str = "usd") -> Topup:
    """
    What an amount before VAT buys: its checks, and the bonus of the highest tier it reaches.

    :param settings: The service settings.
    :param amount: The amount before VAT, in the currency (cents at most; less than the minimum still computes).
    :param currency: ``usd`` or ``eur``.
    :return: The top-up.
    """
    cur: CardCurrency = "eur" if currency == "eur" else "usd"
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
    base = int((amount / price_per_check(settings, cur)).to_integral_value(rounding=ROUND_FLOOR)) if amount > 0 else 0
    pct = max((p for start, p in bonus_tiers(settings) if amount >= start), default=0)
    return Topup(cur, amount, base, pct, base * pct // 100)


def valid(settings: Settings, amount: Decimal) -> bool:
    """
    Whether an amount can be bought: at least the minimum, whole cents, at most a million.

    :param settings: The service settings.
    :param amount: The amount before VAT.
    :return: True if it can.
    """
    return settings.topup_min_amount <= amount <= 1_000_000 and amount == amount.quantize(Decimal("0.01"))


def card_currency(settings: Settings, country: str | None) -> CardCurrency:
    """
    The currency a visitor pays in by card: euros from the euro area (while offered), dollars everywhere else.

    :param settings: The service settings.
    :param country: The visitor's likely country (Cloudflare's hint), or ``None``.
    :return: ``eur`` or ``usd``.
    """
    return "eur" if country in EURO_AREA and settings.card_eur_enabled else "usd"


def format_money(amount: Decimal, currency: str) -> str:
    """
    An amount as a price: ``€100.00``, ``$12.50``.

    :param amount: The amount.
    :param currency: ``usd`` or ``eur``.
    :return: The text.
    """
    return f"{SYMBOL.get(currency, '$')}{amount.quantize(Decimal('0.01'))}"


def label(top: Topup, vat_pct: Decimal | None = None, country: str | None = None) -> str:
    """
    The top-up in one line: ``€100.00 → 1,150 checks · €121.00 incl. 21% VAT (NL)``.

    :param top: The top-up.
    :param vat_pct: The VAT rate added at checkout, once the country is known.
    :param country: The country, for the line.
    :return: The text.
    """
    text = f"{format_money(top.amount, top.currency)} → {top.checks:,} checks"
    if top.bonus_checks:
        text += f" (incl. {top.bonus_checks:,} bonus)"
    if vat_pct is not None:
        total = top.amount + (top.amount * vat_pct / 100).quantize(Decimal("0.01"))
        where = f" ({country})" if country else ""
        text += f" · {format_money(total, top.currency)} incl. {vat_pct.normalize():f}% VAT{where}"
    return text


def card_credits(settings: Settings, net_cents: int, currency: str) -> int:
    """
    What a card order credits: the checks its amount before tax buys, with its bonus.

    :param settings: The service settings.
    :param net_cents: The order's amount before tax, in the currency's cents.
    :param currency: ``usd`` or ``eur``.
    :return: Micro-dollars (zero for another currency).
    """
    if currency not in CARD_CURRENCIES:
        return 0
    return topup(settings, Decimal(net_cents) / 100, currency).credited_usd_micros(settings)


def crypto_credits(settings: Settings, net_usd_micros: int) -> int:
    """
    What a crypto payment credits: the checks its value before VAT buys (in dollars), with its bonus.

    :param settings: The service settings.
    :param net_usd_micros: The payment's value before VAT.
    :return: Micro-dollars.
    """
    return topup(settings, Decimal(max(net_usd_micros, 0)) / USD_MICROS, "usd").credited_usd_micros(settings)


__all__ = [
    "CARD_CURRENCIES",
    "EURO_AREA",
    "CardCurrency",
    "Topup",
    "bonus_tiers",
    "card_credits",
    "card_currency",
    "crypto_credits",
    "format_money",
    "label",
    "micros",
    "price_per_check",
    "topup",
    "valid",
]
