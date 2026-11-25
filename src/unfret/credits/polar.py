"""
Polar (polar.sh), the Merchant of Record for card top-ups of API keys: :class:`PolarProvider`.

Top-ups are one one-time Polar product (``polar_topup_product``) with a custom ("pay what you want") price in US
dollars and one in euros, both tax ``exclusive``: each checkout sets the amount the buyer named, before tax
(:mod:`unfret.credits.topup`), and Polar adds the buyer's sales tax/VAT on top. A checkout is created in USD, or in EUR
for visitors from the euro area, with discount codes off and our account id and this site's address
(``public_base_url``) in its metadata; nothing about the buyer is sent, except the billing country of a local payment
method (:mod:`unfret.credits.local_methods`): Polar shows iDEAL | Wero only for a checkout in EUR whose billing country
is the Netherlands, so that checkout has ``customer_billing_address: {"country": "NL"}`` prefilled. The API calls need
an Organization Access Token with the ``checkouts:write`` scope only.

Webhooks (Standard Webhooks, the ``whsec_`` secret shown when the endpoint is created in Polar) and what they become:

- ``order.paid`` → ``paid``, for the order's ``net_amount`` (after discounts, before sales tax/VAT: what buys the
  checks and what refunds are counted against). Credited only for the top-up product in USD or EUR; anything else
  (another product or currency, a zero amount) credits nothing and is logged as ``polar_order_not_credited`` for a
  person to look at.
  Polar's fee (``platform_fee_amount``, when in the order's currency) goes along (not used for crediting).
- ``order.refunded`` → ``refunded``, with the order's ``refunded_amount`` (the total refunded so far, before tax, in
  the order's currency; Polar reports the refunded tax separately in ``refunded_tax_amount``).
- ``refund.created`` / ``refund.updated`` → ``disputed`` when the refund carries a dispute Polar lost; other refunds
  are already covered by ``order.refunded``. Polar has no webhook of its own for disputes (chargebacks): a dispute it
  prevents by refunding arrives as an ordinary refund.

Everything else is ignored (acknowledged, so Polar doesn't retry it).
"""

import json
from collections.abc import Mapping
from typing import Any, Literal

import httpx
import structlog

from unfret.config import Settings
from unfret.credits.fiat import (
    FiatCheckout,
    FiatEvent,
    FiatProviderError,
    FiatWebhookError,
    verify_standard_webhook,
)
from unfret.credits.topup import CardCurrency, Topup

logger = structlog.get_logger("unfret")

BASE_URLS: dict[str, str] = {"sandbox": "https://sandbox-api.polar.sh", "production": "https://api.polar.sh"}


class PolarProvider:
    """Polar's hosted checkout and order webhooks (see the module docstring)."""

    name = "polar"
    # Polar puts the checkout's id in the success URL in place of this.
    checkout_id_placeholder = "{CHECKOUT_ID}"

    def __init__(
        self,
        *,
        access_token: str,
        environment: Literal["sandbox", "production"],
        product: str,
        webhook_secret: str,
        site: str = "",
        tolerance_s: float = 300,
        timeout_s: float = 15.0,
        org_tax_inclusive: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Set the provider up.

        :param access_token: A Polar Organization Access Token (``checkouts:write``).
        :param environment: ``sandbox`` or ``production``: which Polar API to call.
        :param product: The top-up product's id (a custom price in USD and in EUR, tax exclusive).
        :param webhook_secret: The endpoint's ``whsec_…`` secret.
        :param site: This site (``public_base_url``), put in each checkout's metadata so that only this site credits
            its orders when dev and production share a Polar organisation (:func:`unfret.credits.fiat.apply_event`).
        :param tolerance_s: The webhooks' replay window.
        :param timeout_s: Timeout of calls to Polar's API.
        :param org_tax_inclusive: Whether the organisation's default tax behaviour is inclusive (a price's ``null``).
        :param transport: An httpx transport (tests).
        """
        self.environment = environment
        self.org_tax_inclusive = org_tax_inclusive
        self.product = product
        self.secret, self.tolerance_s, self.site = webhook_secret, tolerance_s, site
        self.http = httpx.AsyncClient(
            base_url=BASE_URLS[environment],
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=timeout_s,
            transport=transport,
        )

    @property
    def status(self) -> str:
        """
        What readiness reports: the provider and its environment, never a secret.

        :return: E.g. ``polar:production``.
        """
        return f"{self.name}:{self.environment}"

    async def create_checkout(
        self,
        account: str,
        top: Topup,
        return_url: str,
        currency: CardCurrency = "usd",
        billing_country: str | None = None,
    ) -> FiatCheckout:
        """
        Create a Polar checkout session for a top-up's amount, in USD or EUR (:meth:`FiatProvider.create_checkout`).

        :param account: The account to credit, for the checkout's metadata.
        :param top: The top-up: its amount before tax.
        :param return_url: The success URL.
        :param currency: ``usd`` or ``eur`` (the product needs a custom price in it).
        :param billing_country: Prefill the buyer's billing country (ISO 3166 alpha-2), for a local payment method;
            ``None`` leaves it to the buyer (cards only until they pick a country).
        :return: The checkout.
        :raises FiatProviderError: If Polar refuses or can't be reached, or the checkout isn't the amount in that
            currency, or the price includes tax (:func:`tax_behaviour_matches`: it must be added on top).
        """
        expected = top.amount_cents
        payload: dict[str, Any] = {
            "products": [self.product],
            "currency": currency,  # the product's custom price in this currency
            "amount": expected,  # before tax: Polar adds the buyer's VAT/sales tax
            "metadata": {"account": account, "site": self.site},
            "success_url": return_url,
            "allow_discount_codes": False,
        }
        if billing_country:
            # Polar's AddressInput needs only the country; it decides which payment methods the checkout shows.
            payload["customer_billing_address"] = {"country": billing_country}
        try:
            response = await self.http.post("/v1/checkouts/", json=payload)
            response.raise_for_status()
            checkout = response.json()
            checkout_id, url = str(checkout["id"]), str(checkout["url"])
        except httpx.HTTPStatusError as exc:
            logger.warning("polar_checkout_failed", status=exc.response.status_code, currency=currency)
            raise FiatProviderError("Polar refused the checkout.") from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("polar_checkout_failed", error=type(exc).__name__, currency=currency)
            raise FiatProviderError("Polar could not be reached.") from exc
        charged, amount = str(checkout.get("currency", "")).lower(), checkout.get("amount")
        if charged != currency or amount != expected:
            # The product's price isn't a custom one in this currency: refuse before anyone pays.
            logger.warning(
                "polar_checkout_price_mismatch",
                currency=charged,
                expected_currency=currency,
                amount_cents=amount,
                expected_cents=expected,
            )
            raise FiatProviderError("The Polar product doesn't take this amount.")
        behaviour = checkout.get("tax_behavior") or (checkout.get("product_price") or {}).get("tax_behavior")
        if not tax_behaviour_matches(False, behaviour, org_inclusive=self.org_tax_inclusive):
            # A price Polar would take the VAT out of, while we show it added on top.
            logger.warning("polar_checkout_tax_mismatch", currency=currency, tax_behavior=behaviour)
            raise FiatProviderError("The Polar product's tax behaviour doesn't match: tax must be added on top.")
        return FiatCheckout(checkout_id, url)

    def parse_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> FiatEvent | None:
        """
        Verify a Polar webhook and reduce it to a :class:`FiatEvent` (see :meth:`FiatProvider.parse_webhook`).

        :param headers: The request headers (lower-case names).
        :param body: The raw body.
        :param now: Current time.
        :return: The event, or ``None`` if it changes nothing (or must not be credited).
        :raises FiatWebhookError: If the webhook is not authentic, too old, or unreadable.
        """
        verify_standard_webhook(self.secret, headers, body, now=now, tolerance_s=self.tolerance_s)
        try:
            event = json.loads(body)
            kind, data = event["type"], event["data"]
            if kind == "order.paid":
                return self._paid(data)
            if kind == "order.refunded":
                return FiatEvent(
                    kind="refunded",
                    provider=self.name,
                    order_id=str(data["id"]),
                    amount_cents=int(data["net_amount"]),
                    refunded_cents=int(data["refunded_amount"]),
                )
            if kind in {"refund.created", "refund.updated"}:
                dispute = data.get("dispute") or {}
                if dispute.get("status") == "lost":
                    return FiatEvent(kind="disputed", provider=self.name, order_id=str(data["order_id"]))
            return None
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise FiatWebhookError("Unreadable event.") from exc

    def _paid(self, data: dict[str, Any]) -> FiatEvent | None:
        order_id = str(data["id"])
        product, currency, net = data.get("product_id"), str(data["currency"]).lower(), int(data["net_amount"])
        if str(product) != self.product or currency not in ("usd", "eur") or net <= 0:
            reason = "unknown_product" if str(product) != self.product else "currency" if net > 0 else "amount"
            logger.warning(
                "polar_order_not_credited", order_id=order_id, reason=reason, currency=currency, amount_cents=net
            )
            return None
        metadata = data.get("metadata") or {}
        # Polar's fee, if it reports one in the order's currency (kept for support; fees are absorbed, not deducted).
        fee, fee_currency = data.get("platform_fee_amount"), str(data.get("platform_fee_currency") or currency)
        return FiatEvent(
            kind="paid",
            provider=self.name,
            order_id=order_id,
            amount_cents=net,
            currency=currency,
            checkout_id=str(data["checkout_id"]) if data.get("checkout_id") else None,
            account=str(metadata["account"]) if metadata.get("account") else None,
            site=str(metadata["site"]) if metadata.get("site") else None,
            fee_cents=int(fee) if fee and fee_currency.lower() == currency else None,
        )

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self.http.aclose()


def tax_behaviour_matches(inclusive: bool, behaviour: object, *, org_inclusive: bool = False) -> bool:
    """
    Whether a Polar price's ``tax_behavior`` charges what we show.

    With ``org_inclusive`` (``polar_org_tax_inclusive``: the organisation's default is set to inclusive), ``null``
    means inclusive: accepted for a tax-inclusive price, refused for one before tax.

    A tax-inclusive price must say ``inclusive`` on the price itself: ``null`` falls back to the organisation's
    default, which the token can't read, and ``location`` adds the tax on top outside the EU. A price before tax may be
    ``exclusive``, ``null`` (the default) or ``location`` (US buyers still pay their sales tax on top), just not
    ``inclusive``, which would charge less than the price list says and credit nothing.

    :param inclusive: Whether the price in that currency includes tax.
    :param behaviour: The price's ``tax_behavior`` as Polar returned it.
    :param org_inclusive: Whether the organisation's default tax behaviour is inclusive.
    :return: True if the checkout may go ahead.
    """
    effective = "inclusive" if behaviour is None and org_inclusive else behaviour
    return effective == "inclusive" if inclusive else effective != "inclusive"


def from_settings(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> PolarProvider | None:
    """
    The Polar provider, if it is fully configured (token, webhook secret, the top-up product); else ``None``.

    :param settings: The service settings.
    :param transport: An httpx transport (tests).
    :return: The provider, or ``None`` (fiat stays off).
    """
    if not settings.polar_configured:
        return None
    return PolarProvider(
        access_token=settings.polar_access_token.get_secret_value(),
        environment=settings.polar_environment,
        product=settings.polar_topup_product.strip(),
        webhook_secret=settings.fiat_webhook_secret.get_secret_value(),
        site=settings.public_base_url,
        tolerance_s=settings.fiat_webhook_tolerance_s,
        timeout_s=settings.polar_timeout_s,
        org_tax_inclusive=settings.polar_org_tax_inclusive,
        transport=transport,
    )


__all__ = ["BASE_URLS", "PolarProvider", "from_settings"]
