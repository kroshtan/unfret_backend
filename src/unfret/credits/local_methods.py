"""
Local payment methods: a country's own way to pay, offered next to the card button (:data:`LOCAL_METHODS`).

Polar's hosted checkout picks the payment methods it shows from the buyer's billing country and the checkout's
currency: with no country it shows cards only. A local method is therefore a checkout in the method's currency with
the billing country prefilled; the buyer can still change the country (or pay by card) on Polar's page. Nothing
else changes: the same products, prices, metadata, crediting and refunds as a card checkout.

To add a country, add one entry to :data:`LOCAL_METHODS` (keyed by the visitor's ISO 3166 alpha-2 country, as
Cloudflare's hint gives it). Before adding it, check by hand that Polar really offers that method for a checkout in
that currency with that billing country (create a checkout, set the country on Polar's page, look); an entry for a
method Polar doesn't offer there would only show a card form behind a button that promises something else.
"""

from dataclasses import dataclass

from unfret.config import Settings
from unfret.credits.packs import CardCurrency, euros_on


@dataclass(frozen=True)
class LocalMethod:
    """One country's local payment method at the provider."""

    id: str  # what clients send as ``payment_method`` (lower case, stable)
    label: str  # the method's name as the buyer knows it, for the button ("Pay €6.00 with <label>")
    billing_country: str  # prefilled as the checkout's billing country (ISO 3166 alpha-2): what makes Polar show it
    currency: CardCurrency  # the checkout's currency; the method is offered only while that currency is on


# The visitor's country → its local method. Verified at Polar: iDEAL | Wero shows for EUR with billing country NL.
LOCAL_METHODS: dict[str, LocalMethod] = {
    "NL": LocalMethod(id="wero", label="iDEAL | Wero", billing_country="NL", currency="eur"),
}

CARD = "card"


def offered(settings: Settings, method: LocalMethod) -> bool:
    """
    Whether a method can be used now: its currency is on (euro methods need ``card_prices_eur``).

    :param settings: The service settings.
    :param method: The method.
    :return: True if a checkout for it can be created.
    """
    return method.currency == "usd" or euros_on(settings)


def local_method(settings: Settings, country: str | None) -> LocalMethod | None:
    """
    The local payment method offered to a visitor from ``country``, if any (and while its currency is on).

    :param settings: The service settings.
    :param country: The visitor's likely country (Cloudflare's hint), or ``None``.
    :return: The method, or ``None``: card only.
    """
    method = LOCAL_METHODS.get(country or "")
    return method if method and offered(settings, method) else None


def method_by_id(method_id: str) -> LocalMethod | None:
    """
    The local method with this id, whichever country it is listed for.

    :param method_id: E.g. ``wero``.
    :return: The method, or ``None`` if no entry has that id.
    """
    return next((m for m in LOCAL_METHODS.values() if m.id == method_id), None)


def method_ids() -> list[str]:
    """
    Every value ``payment_method`` accepts: ``card`` and each local method's id.

    :return: The ids, ``card`` first.
    """
    return [CARD, *dict.fromkeys(m.id for m in LOCAL_METHODS.values())]


__all__ = ["CARD", "LOCAL_METHODS", "LocalMethod", "local_method", "method_by_id", "method_ids", "offered"]
