"""
A product page's "was" price against the page's own price history: invented reference prices and endless discounts.

The price store (:mod:`unfret.price_store`) keeps one row per product page per day: the price shown and the struck
"was" price, nothing about who looked. Over weeks those rows show two tricks the page itself can't:

- ``was_price_unseen``: the "was" price shown today was never (or on barely any day) the price on the days the page
  was seen. A discount measured against a price nobody was charged.
- ``discount_always_on``: the page showed a "was" price above its price on (nearly) every day it was seen, over
  weeks. A sale that never ends is the normal price with a made-up reference next to it.

Both are findings, not verdicts: evidence for "manipulation" (level 1), and a large invented discount can hide that
the price itself is high, which the level model may weigh toward "overpaying". No band rules here.

**Short history says nothing.** Without enough distinct days over a long enough span, no finding is emitted (never
a guess): a page seen a few times, or only during one sale, can't show what it normally costs. The days and span
are settings (``was_price_min_days``, ``price_history_min_days``, ``discount_always_on_days``).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from unfret.config import settings
from unfret.price_store import Observation

# A day's price this close to the "was" price (rounding, a cent off) counts as being charged the "was" price.
_AT_WAS = 0.98
# "Barely": the "was" price was the price on at most this share of the days seen.
_BARELY = 0.1
# Today's discount must be at least this deep for the "was" price to matter (not a rounding difference).
_REAL_DISCOUNT = 1.03
# A day counts as discounted when its "was" price is at least this much above its price.
_DISCOUNTED = 1.01
# "Always on": a discount shown on at least this share of the days seen.
_ALWAYS = 0.9


@dataclass(frozen=True)
class Finding:
    """One finding of this check: its type and its evidence in words, numbers in a fixed order."""

    type: str
    evidence: str


@dataclass(frozen=True)
class Summary:
    """What the history says about today's "was" price; ``None`` shares mean the history is too short to say."""

    days: int  # distinct days seen before today, in today's currency
    span: int  # days since the first of them
    charged_share: float | None  # share of the days the "was" price was the price
    discounted_share: float | None  # share of the days a discount was shown


def _span(observed: Sequence[Observation], today: date) -> int:
    return (today - min(o.day for o in observed)).days if observed else 0


def summarise(observed: Sequence[Observation], today: date, price: float, was: float | None) -> Summary:
    """
    The history's numbers for today's price and "was" price.

    :param observed: The page's earlier days (one row each, today's currency).
    :param today: Today.
    :param price: Today's price.
    :param was: Today's "was" price, if the page shows one.
    :return: The summary; the shares are ``None`` when the history is too short (or there is no discount today).
    """
    days, span = len({o.day for o in observed}), _span(observed, today)
    enough = days >= settings.was_price_min_days
    discounted_today = was is not None and was > price * _REAL_DISCOUNT
    charged = (
        sum(o.price >= was * _AT_WAS for o in observed) / days
        if enough and discounted_today and was is not None and span >= settings.price_history_min_days
        else None
    )
    discounted = (
        sum(o.was_price is not None and o.was_price > o.price * _DISCOUNTED for o in observed) / days
        if enough and discounted_today
        else None
    )
    return Summary(days=days, span=span, charged_share=charged, discounted_share=discounted)


def assess(observed: Sequence[Observation], today: date, price: float, was: float | None) -> list[Finding]:
    """
    The findings the page's own price history supports, if any.

    :param observed: The page's earlier days (one row each, today's currency; from :class:`price_store.History`).
    :param today: Today.
    :param price: Today's price.
    :param was: Today's "was" price, if the page shows one.
    :return: ``was_price_unseen`` and/or ``discount_always_on``; empty when the history is too short or says
        nothing.
    """
    summary = summarise(observed, today, price, was)
    if was is None or not observed:
        return []
    found: list[Finding] = []
    off = round((was - price) / was * 100)
    if summary.charged_share is not None and summary.charged_share <= _BARELY:
        charged = round(summary.charged_share * summary.days)
        low, high = min(o.price for o in observed), max(o.price for o in observed)
        found.append(
            Finding(
                "was_price_unseen",
                f"Seen on {summary.days} days over {summary.span} days: price {low:g} to {high:g}; the 'was' price "
                f"{was:g} was the price on {charged} of them. Today: {price:g}, {off}% off.",
            )
        )
    if (
        summary.discounted_share is not None
        and summary.discounted_share >= _ALWAYS
        and summary.span >= settings.discount_always_on_days
    ):
        discounted = round(summary.discounted_share * summary.days)
        found.append(
            Finding(
                "discount_always_on",
                f"A 'was' price above the price on {discounted} of {summary.days} days seen over {summary.span} "
                f"days, and today: {off}% off.",
            )
        )
    return found
