"""
Polar (polar.sh), the Merchant of Record for card top-ups of API keys: :class:`PolarProvider`.

Each pack is a one-time Polar product priced at the pack's card price in US dollars (``polar_products`` maps pack
sizes to product ids). A checkout is created for the pack's product, in USD, with discount codes off and our account
id in its metadata; nothing about the buyer is sent. The API calls need an Organization Access Token with the
``checkouts:write`` scope only.

Webhooks (Standard Webhooks, the ``whsec_`` secret shown when the endpoint is created in Polar) and what they become:

- ``order.paid`` → ``paid``, for the order's ``net_amount`` (after discounts, before sales tax/VAT). Credited only if
  the order is in USD, for a configured pack's product, and its ``net_amount`` is exactly that pack's card price;
  anything else (another currency, a zero or changed price, an unknown product) credits nothing and is logged as
  ``polar_order_not_credited`` for a person to look at.
- ``order.refunded`` → ``refunded``, with the order's ``refunded_amount`` (the total refunded so far, before tax;
  Polar reports the refunded tax separately in ``refunded_tax_amount``).
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

from veridex.config import Settings
from veridex.credits.fiat import (
    USD_MICROS_PER_CENT,
    FiatCheckout,
    FiatEvent,
    FiatProviderError,
    FiatWebhookError,
    verify_standard_webhook,
)
from veridex.credits.packs import Pack, packs

logger = structlog.get_logger("veridex")

BASE_URLS: dict[str, str] = {"sandbox": "https://sandbox-api.polar.sh", "production": "https://api.polar.sh"}


class PolarProvider:
    """Polar's hosted checkout and order webhooks (see the module docstring)."""

    name = "polar"

    def __init__(
        self,
        *,
        access_token: str,
        environment: Literal["sandbox", "production"],
        products: Mapping[Pack, str],
        webhook_secret: str,
        tolerance_s: float = 300,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """
        Set the provider up.

        :param access_token: A Polar Organization Access Token (``checkouts:write``).
        :param environment: ``sandbox`` or ``production``: which Polar API to call.
        :param products: The Polar product id of each pack.
        :param webhook_secret: The endpoint's ``whsec_…`` secret.
        :param tolerance_s: The webhooks' replay window.
        :param timeout_s: Timeout of calls to Polar's API.
        :param transport: An httpx transport (tests).
        """
        self.environment = environment
        self.products = {pack.checks: product for pack, product in products.items()}
        self.packs_by_product = {product: pack for pack, product in products.items()}
        self.secret, self.tolerance_s = webhook_secret, tolerance_s
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

    async def create_checkout(self, account: str, pack: Pack, return_url: str) -> FiatCheckout:
        """
        Create a Polar checkout session for the pack's product, in USD (see :meth:`FiatProvider.create_checkout`).

        :param account: The account to credit, for the checkout's metadata.
        :param pack: The pack.
        :param return_url: The success URL.
        :return: The checkout.
        :raises FiatProviderError: If Polar refuses or can't be reached, or the checkout isn't the pack's USD price.
        """
        product = self.products.get(pack.checks)
        if product is None:
            raise FiatProviderError("No Polar product for this pack.")
        payload = {
            "products": [product],
            "currency": "usd",  # the product's USD price, whatever other currencies it carries
            "metadata": {"account": account},
            "success_url": return_url,
            "allow_discount_codes": False,  # every order must pay exactly the pack's card price
        }
        try:
            response = await self.http.post("/v1/checkouts/", json=payload)
            response.raise_for_status()
            checkout = response.json()
            checkout_id, url = str(checkout["id"]), str(checkout["url"])
        except httpx.HTTPStatusError as exc:
            logger.warning("polar_checkout_failed", status=exc.response.status_code, checks=pack.checks)
            raise FiatProviderError("Polar refused the checkout.") from exc
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning("polar_checkout_failed", error=type(exc).__name__, checks=pack.checks)
            raise FiatProviderError("Polar could not be reached.") from exc
        expected = pack.card_usd_micros // USD_MICROS_PER_CENT
        currency, amount = str(checkout.get("currency", "")).lower(), checkout.get("amount")
        if currency != "usd" or amount != expected:
            # A product priced differently from the pack: refuse before anyone pays.
            logger.warning(
                "polar_checkout_price_mismatch",
                checks=pack.checks,
                currency=currency,
                amount_cents=amount,
                expected_cents=expected,
            )
            raise FiatProviderError("The Polar product's price doesn't match the pack.")
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
        pack = self.packs_by_product.get(str(product)) if product else None
        expected = pack.card_usd_micros // USD_MICROS_PER_CENT if pack else None
        if pack is None or currency != "usd" or net != expected:
            reason = "unknown_product" if pack is None else "currency" if currency != "usd" else "amount"
            logger.warning(
                "polar_order_not_credited",
                order_id=order_id,
                reason=reason,
                currency=currency,
                amount_cents=net,
                expected_cents=expected,
            )
            return None
        metadata = data.get("metadata") or {}
        return FiatEvent(
            kind="paid",
            provider=self.name,
            order_id=order_id,
            amount_cents=net,
            checkout_id=str(data["checkout_id"]) if data.get("checkout_id") else None,
            account=str(metadata["account"]) if metadata.get("account") else None,
        )

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self.http.aclose()


def from_settings(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> PolarProvider | None:
    """
    The Polar provider, if it is fully configured (token, webhook secret, a product for every pack); else ``None``.

    :param settings: The service settings.
    :param transport: An httpx transport (tests).
    :return: The provider, or ``None`` (fiat stays off).
    """
    if not settings.polar_configured:
        return None
    ids = settings.polar_product_ids
    return PolarProvider(
        access_token=settings.polar_access_token.get_secret_value(),
        environment=settings.polar_environment,
        products={pack: ids[pack.checks] for pack in packs(settings)},
        webhook_secret=settings.fiat_webhook_secret.get_secret_value(),
        tolerance_s=settings.fiat_webhook_tolerance_s,
        timeout_s=settings.polar_timeout_s,
        transport=transport,
    )


__all__ = ["BASE_URLS", "PolarProvider", "from_settings"]
