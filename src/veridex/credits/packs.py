"""
Credit packs and their prices, all derived from one setting: the price of a check (``price_per_check_usd``).

A pack of ``n`` checks costs:

- **by card** (through the Merchant of Record): the pack's value plus the provider's worst-case cut, rounded up to
  ``card_price_step_usd``: ``ceil_step((value + card_fee_flat) / (1 − card_fee_pct))``. At $0.20 a check with
  6.5% + $0.50 and a $0.50 step: 25 → $6, 50 → $11.50, 100 → $22. The provider adds the buyer's sales tax/VAT.
- **with crypto** (SOL or USDC): just the pack's value, ``n × price_per_check`` ($5, $10, $20), plus VAT where it
  applies (:mod:`veridex.credits.vat`).

The card price is *the* price and crypto is shown as the discount (EU rules forbid surcharging consumer card
payments; a discount for paying another way is fine). Either way the account is credited the pack's value, so a
check costs the same ``price_per_check_usd`` in credits whichever way the pack was bought. Changing that one setting
moves every price: analyses, the x402 deep tier, the packs on both routes, ``/v1/pricing`` and the checkout page.
"""

from dataclasses import dataclass
from decimal import Decimal

from veridex.config import Settings

USD_MICROS = 1_000_000


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
    per_check = settings.analysis_price_usd_micros
    return [Pack(n, n * per_check, card_price(n * per_check, settings)) for n in settings.packs]


def pack(settings: Settings, checks: int) -> Pack | None:
    """
    The pack of ``checks`` checks, if one is on sale.

    :param settings: The service settings.
    :param checks: The pack size.
    :return: The pack, or ``None``.
    """
    return next((p for p in packs(settings) if p.checks == checks), None)


def card_credits(settings: Settings, amount_cents: int) -> int:
    """
    The credits a card payment buys: the value of the pack it paid for.

    A payment that matches no pack's card price (a price changed while a checkout was open, or a provider-side
    discount) buys the whole checks its amount covers after the provider's cut, so it never credits more than a
    pack would.

    :param settings: The service settings.
    :param amount_cents: The amount paid, before tax, in US cents.
    :return: Micro-dollars to credit (zero if it covers no check).
    """
    paid = amount_cents * USD_MICROS // 100
    for p in packs(settings):
        if p.card_usd_micros == paid:
            return p.value_usd_micros
    keep = Decimal(1) - settings.card_fee_pct / 100
    covered = int(Decimal(paid) * keep) - micros(settings.card_fee_flat_usd)
    per_check = settings.analysis_price_usd_micros
    return max(covered, 0) // per_check * per_check


__all__ = ["Pack", "card_credits", "card_price", "micros", "pack", "packs"]
