"""
Credit packs and their prices, derived from the price of a check (``price_per_check_usd``) but for the card prices.

The card prices are set by hand, so they stay round.

A pack of ``n`` checks (25 and 100 by default) costs:

- **by card** (through the Merchant of Record): its price in ``card_prices_usd`` ($6 and $20 by default), before the
  provider's sales tax/VAT. A pack not named there costs its value plus the provider's worst-case cut, rounded up to
  ``card_price_step_usd``: ``ceil_step((value + card_fee_flat) / (1 − card_fee_pct))`` (:func:`card_price`; at
  $0.20 a check with 6.5% + $0.50 and a $0.50 step: 25 → $6, 100 → $22).
- **with crypto** (SOL or USDC): just the pack's value, ``n × price_per_check`` ($5, $20), plus VAT where it applies
  (:mod:`unfret.credits.vat`). Crypto is never dearer than the card price, and on the 100-pack it is the same.

Visitors from the euro area pay by card in euros instead (:func:`card_currency`): each pack's euro price is a
setting of its own (``card_prices_eur``, default €5.50 / €17.50), not converted from the dollar price, so it stays
round. A euro checkout is what shows "iDEAL | Wero" to Dutch buyers at the provider. The credits are the same: a
pack's value in dollars, whichever currency paid for it.

The card price is *the* price and crypto is shown as the discount where there is one (EU rules forbid surcharging
consumer card payments; a discount for paying another way is fine). Either way the account is credited the pack's
value, so a check costs the same ``price_per_check_usd`` in credits whichever way the pack was bought. Changing that
one setting moves the analyses, the x402 deep tier, the crypto prices, ``/v1/pricing`` and the checkout page; the
card prices follow it only for packs without a price of their own.

Packs taken off sale (the 50-pack, until November 2026) keep everything they credited: balances are in dollars, and
gift codes already issued stay redeemable.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from unfret.config import Settings

USD_MICROS = 1_000_000

CardCurrency = Literal["usd", "eur"]
CARD_CURRENCIES: tuple[CardCurrency, ...] = ("usd", "eur")

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


@dataclass(frozen=True)
class Pack:
    """A pack of checks and its price on each route."""

    checks: int
    value_usd_micros: int  # credited to the account, and the crypto price before VAT
    card_usd_micros: int  # the price by card, before the provider's sales tax/VAT
    card_eur_cents: int | None = None  # the price by card in euros (cents, before VAT); None: dollars only

    def card_cents(self, currency: str) -> int | None:
        """
        The card price in the currency's cents.

        :param currency: ``usd`` or ``eur``.
        :return: Cents, or ``None`` if the pack has no price in that currency.
        """
        if currency == "usd":
            return self.card_usd_micros // (USD_MICROS // 100)
        return self.card_eur_cents if currency == "eur" else None


def card_price(value_usd_micros: int, settings: Settings) -> int:
    """
    The card price of a pack worth ``value_usd_micros``: enough to cover the provider's cut, rounded up to the step.

    :param value_usd_micros: The pack's value.
    :param settings: The service settings.
    :return: Micro-dollars, a whole multiple of ``card_price_step_usd``.
    """
    step = micros(settings.card_price_step_usd)
    keep = Decimal(1) - settings.card_fee_pct / 100
    needed = (Decimal(value_usd_micros) + micros(settings.card_fee_flat_usd)) / keep
    return -(-int(needed.to_integral_value(rounding="ROUND_CEILING")) // step) * step


def packs(settings: Settings) -> list[Pack]:
    """
    Every pack on sale.

    :param settings: The service settings.
    :return: The packs, smallest first.
    """
    per_check, usd, eur = settings.analysis_price_usd_micros, settings.card_usd_cents, settings.card_eur_cents
    cent = USD_MICROS // 100
    return [
        Pack(
            n,
            n * per_check,
            usd[n] * cent if n in usd else card_price(n * per_check, settings),
            eur.get(n),
        )
        for n in settings.packs
    ]


def euros_on(settings: Settings) -> bool:
    """
    Whether card payments in euros are offered: every pack has a euro price.

    :param settings: The service settings.
    :return: True when ``card_prices_eur`` prices every pack.
    """
    return all(p.card_eur_cents for p in packs(settings))


def card_currency(settings: Settings, country: str | None) -> CardCurrency:
    """
    The currency a visitor pays in by card: euros from the euro area (when offered), dollars everywhere else.

    :param settings: The service settings.
    :param country: The visitor's likely country (Cloudflare's hint), or ``None``.
    :return: ``eur`` or ``usd``.
    """
    return "eur" if country in EURO_AREA and euros_on(settings) else "usd"


def format_eur(cents: int) -> str:
    """
    Euro cents as a price.

    :param cents: The amount.
    :return: E.g. ``"€5.50"``.
    """
    return f"€{cents // 100}.{cents % 100:02d}"


def pack(settings: Settings, checks: int) -> Pack | None:
    """
    The pack of ``checks`` checks, if one is on sale.

    :param settings: The service settings.
    :param checks: The pack size.
    :return: The pack, or ``None``.
    """
    return next((p for p in packs(settings) if p.checks == checks), None)


def pack_for_card(settings: Settings, amount_cents: int, currency: str = "usd") -> Pack | None:
    """
    The pack whose card price a payment paid exactly.

    :param settings: The service settings.
    :param amount_cents: The amount paid, before tax, in the currency's cents.
    :param currency: The payment's currency (``usd`` or ``eur``).
    :return: The pack, or ``None`` if the amount matches none (or the currency isn't one we sell in).
    """
    return next((p for p in packs(settings) if p.card_cents(currency) == amount_cents), None)


def card_credits(settings: Settings, amount_cents: int, currency: str = "usd") -> int:
    """
    The credits a card payment buys: the value of the pack it paid for.

    A dollar payment that matches no pack's card price (a price changed while a checkout was open, or a
    provider-side discount) buys the whole checks its amount covers after the provider's cut, so it never credits
    more than a pack would. A euro payment must match a pack's euro price exactly: there is no exchange rate here to
    value any other amount, so it buys nothing (and is left for a person to look at).

    :param settings: The service settings.
    :param amount_cents: The amount paid, before tax, in the currency's cents.
    :param currency: The payment's currency (``usd`` or ``eur``).
    :return: Micro-dollars to credit (zero if it covers no check).
    """
    if (matched := pack_for_card(settings, amount_cents, currency)) is not None:
        return matched.value_usd_micros
    if currency != "usd":
        return 0
    paid = amount_cents * USD_MICROS // 100
    keep = Decimal(1) - settings.card_fee_pct / 100
    covered = int(Decimal(paid) * keep) - micros(settings.card_fee_flat_usd)
    per_check = settings.analysis_price_usd_micros
    return max(covered, 0) // per_check * per_check


__all__ = [
    "CARD_CURRENCIES",
    "EURO_AREA",
    "CardCurrency",
    "Pack",
    "card_credits",
    "card_currency",
    "card_price",
    "euros_on",
    "format_eur",
    "micros",
    "pack",
    "pack_for_card",
    "packs",
]
