"""Polar as the fiat provider: the checkout request, the webhooks it sends, and when it is on at all."""

import base64
import copy
import json
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from unfret.config import Settings
from unfret.credits import polar
from unfret.credits.fiat import FiatEvent, FiatProviderError, FiatWebhookError, sign_standard_webhook
from unfret.credits.polar import PolarProvider
from unfret.credits.topup import topup

SECRET = "whsec_" + base64.b64encode(b"polar-test-webhook-secret-32byte").decode()
PRODUCT = "2f1c9a6e-6c1b-4a51-9a1e-3f4f0c1d2b25"  # the top-up product: a custom price in USD and EUR, tax exclusive
ACCOUNT = "key:abcdefghij"
CHECKOUT_ID = "c6a3b2e1-0f9d-4e8c-b7a6-5d4c3b2a1f00"
SUCCESS_URL = "https://unfret.net/checkout?paid=1"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "polar_access_token": SecretStr("polar_oat_test"),
        "polar_topup_product": PRODUCT,
        "fiat_webhook_secret": SecretStr(SECRET),
        "polar_org_tax_inclusive": False,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def provider(transport: httpx.MockTransport | None = None, **overrides: Any) -> PolarProvider:
    built = polar.from_settings(settings(**overrides), transport=transport)
    assert built is not None
    return built


# An order as Polar's webhooks carry it (OpenAPI 2026-10, schema Order): a $6.00 top-up, bought from the Netherlands.
ORDER: dict[str, Any] = {
    "id": "8b1e5c7a-3d2f-4e6a-9c0b-1f2e3d4c5b6a",
    "created_at": "2026-11-09T12:00:00.000000Z",
    "modified_at": "2026-11-09T12:00:05.000000Z",
    "status": "paid",
    "paid": True,
    "subtotal_amount": 600,
    "discount_amount": 0,
    "net_amount": 600,
    "tax_amount": 126,
    "total_amount": 726,
    "applied_balance_amount": 0,
    "due_amount": 0,
    "refunded_amount": 0,
    "refunded_tax_amount": 0,
    "currency": "usd",
    "billing_reason": "purchase",
    "billing_name": "Jan Jansen",
    "billing_address": {"line1": "Kerkstraat 1", "postal_code": "1011 AA", "city": "Amsterdam", "country": "NL"},
    "invoice_number": "UNFRET-0001",
    "is_invoice_generated": False,
    "receipt_number": None,
    "seats": None,
    "units": None,
    "customer_id": "0a9b8c7d-6e5f-4a3b-2c1d-0e9f8a7b6c5d",
    "product_id": PRODUCT,
    "discount_id": None,
    "subscription_id": None,
    "checkout_id": CHECKOUT_ID,
    "next_payment_attempt_at": None,
    "metadata": {"account": ACCOUNT},
    "custom_field_data": {},
    "platform_fee_amount": 69,
    "platform_fee_currency": "usd",
    "customer": {
        "id": "0a9b8c7d-6e5f-4a3b-2c1d-0e9f8a7b6c5d",
        "email": "jan.jansen@example.nl",
        "name": "Jan Jansen",
        "billing_address": {"line1": "Kerkstraat 1", "country": "NL"},
        "tax_id": None,
        "metadata": {},
    },
    "product": {"id": PRODUCT, "name": "Unfret: top-up", "is_recurring": False, "metadata": {}},
    "discount": None,
    "subscription": None,
    "items": [{"id": "1d2c3b4a-5f6e-4d7c-8b9a-0f1e2d3c4b5a", "label": "Unfret: 25 checks", "amount": 600}],
    "description": "Unfret: 25 checks",
    "refundable_amount": 600,
    "refundable_tax_amount": 126,
}

# A refund as Polar's refund.* webhooks carry it (schema Refund), here one that lost a dispute.
REFUND: dict[str, Any] = {
    "created_at": "2026-11-20T09:00:00.000000Z",
    "modified_at": None,
    "id": "4f3e2d1c-0b9a-4876-a5b4-c3d2e1f0a9b8",
    "metadata": {},
    "status": "succeeded",
    "reason": "other",
    "amount": 600,
    "tax_amount": 126,
    "currency": "usd",
    "organization_id": "5e4d3c2b-1a09-4f8e-b7d6-c5b4a3928170",
    "order_id": ORDER["id"],
    "subscription_id": None,
    "customer_id": ORDER["customer_id"],
    "revoke_benefits": False,
    "dispute": {
        "id": "6a5b4c3d-2e1f-4a0b-9c8d-7e6f5a4b3c2d",
        "status": "lost",
        "resolved": True,
        "closed": True,
        "amount": 600,
        "tax_amount": 126,
        "currency": "usd",
        "reason": "fraudulent",
    },
}


def signed(event_type: str, data: dict[str, Any], now: float) -> tuple[bytes, dict[str, str]]:
    event = {"type": event_type, "timestamp": "2026-11-09T12:00:05.000000Z", "api_version": "2026-10", "data": data}
    body, msg_id, stamp = json.dumps(event).encode(), "msg_2pXk9Q", int(now)
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(stamp),
        "webhook-signature": sign_standard_webhook(SECRET, msg_id, stamp, body),
    }
    return body, headers


def parse(event_type: str, data: dict[str, Any]) -> FiatEvent | None:
    now = time.time()
    body, headers = signed(event_type, data, now)
    return provider().parse_webhook(headers, body, now)


def order(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(ORDER), **changes}


# ── When it is on ─────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "missing",
    [{"polar_access_token": SecretStr("")}, {"fiat_webhook_secret": SecretStr("")}, {"polar_topup_product": ""}],
)
def test_off_unless_fully_configured(missing: dict[str, Any]) -> None:
    assert polar.from_settings(settings(**missing)) is None


def test_on_when_configured_and_reports_no_secret() -> None:
    built = provider(polar_environment="production")
    assert built.status == "polar:production"
    assert settings(polar_environment="production").summary()["fiat"] == "production"
    assert settings(polar_topup_product="").summary()["fiat"] == "off"
    assert settings(polar_environment="").polar_environment == "sandbox"  # an empty deploy secret


# ── Checkout ──────────────────────────────────────────────────────────────────────────────────────────────────


def polar_api(seen: list[httpx.Request], *, status: int = 201, **reply: Any) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        sent = json.loads(request.content)
        body = {
            "id": CHECKOUT_ID,
            "url": f"https://buy.polar.sh/polar_c_{CHECKOUT_ID}",
            "client_secret": "polar_c_secret",
            "status": "open",
            "currency": sent.get("currency", "usd"),
            "amount": sent.get("amount"),
            "net_amount": sent.get("amount"),
            "tax_amount": None,
            "product_id": sent["products"][0],
            "product_price": {"amount_type": "custom", "tax_behavior": "exclusive"},
            "metadata": sent.get("metadata", {}),
            "success_url": sent.get("success_url"),
            **reply,
        }
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("environment", "base"), [("sandbox", "https://sandbox-api.polar.sh"), ("production", "https://api.polar.sh")]
)
async def test_a_checkout_is_the_top_up_product_at_the_amount_with_our_account_only(
    environment: str, base: str
) -> None:
    seen: list[httpx.Request] = []
    built = provider(polar_api(seen), polar_environment=environment)

    checkout = await built.create_checkout(ACCOUNT, topup(settings(), Decimal(50)), SUCCESS_URL)

    assert checkout.checkout_id == CHECKOUT_ID
    assert checkout.url == f"https://buy.polar.sh/polar_c_{CHECKOUT_ID}"
    (request,) = seen
    assert str(request.url) == f"{base}/v1/checkouts/"
    assert request.headers["authorization"] == "Bearer polar_oat_test"
    assert json.loads(request.content) == {
        "products": [PRODUCT],
        "currency": "usd",
        "amount": 5000,  # before tax: Polar adds the buyer's VAT/sales tax
        "metadata": {"account": ACCOUNT, "site": settings().public_base_url},
        "success_url": SUCCESS_URL,
        "allow_discount_codes": False,
    }  # nothing about the buyer


async def test_a_euro_checkout_for_wero_prefills_only_the_billing_country() -> None:
    seen: list[httpx.Request] = []
    built = provider(polar_api(seen))
    await built.create_checkout(ACCOUNT, topup(settings(), Decimal(100), "eur"), SUCCESS_URL, "eur", "NL")
    sent = json.loads(seen[0].content)
    assert (sent["currency"], sent["amount"], sent["customer_billing_address"]) == ("eur", 10000, {"country": "NL"})


@pytest.mark.parametrize(
    "reply",
    [
        {"currency": "eur"},  # no custom price in dollars
        {"amount": 0},  # a fixed or free price: the amount was ignored
        {"amount": 600},
        {"product_price": {"tax_behavior": "inclusive"}},  # Polar would take the VAT out of the amount
        {"status": 422},
    ],
)
async def test_a_checkout_polar_wont_charge_as_shown_is_refused_before_anyone_pays(reply: dict[str, Any]) -> None:
    status = reply.pop("status", 201)
    built = provider(polar_api([], status=status, **reply))
    with pytest.raises(FiatProviderError):
        await built.create_checkout(ACCOUNT, topup(settings(), Decimal(50)), SUCCESS_URL)


async def test_with_an_inclusive_org_default_the_price_must_say_exclusive() -> None:
    built = provider(polar_api([], product_price={"tax_behavior": None}), polar_org_tax_inclusive=True)
    with pytest.raises(FiatProviderError):
        await built.create_checkout(ACCOUNT, topup(settings(), Decimal(50)), SUCCESS_URL)


async def test_polar_unreachable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(FiatProviderError):
        await provider(httpx.MockTransport(down)).create_checkout(ACCOUNT, topup(settings(), Decimal(10)), SUCCESS_URL)


# ── Webhooks ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_order_paid_is_the_pre_tax_amount_and_nothing_about_the_buyer() -> None:
    event = parse("order.paid", order())
    assert event == FiatEvent(
        kind="paid",
        provider="polar",
        order_id=ORDER["id"],
        amount_cents=600,  # net_amount: before tax, what buys the checks
        checkout_id=CHECKOUT_ID,
        account=ACCOUNT,
        fee_cents=69,
    )
    assert "jan" not in repr(event).lower()


@pytest.mark.parametrize(
    "changes",
    [{"product_id": "another-product"}, {"product_id": None}, {"currency": "gbp"}, {"net_amount": 0}],
)
def test_an_order_that_isnt_a_top_up_credits_nothing(changes: dict[str, Any]) -> None:
    assert parse("order.paid", order(**changes)) is None


def test_a_euro_order_is_its_amount_before_vat() -> None:
    event = parse("order.paid", order(currency="eur", net_amount=10000, tax_amount=2100))
    assert event is not None and (event.currency, event.amount_cents) == ("eur", 10000)


def test_refunds_carry_the_pre_tax_total_refunded_so_far() -> None:
    partial = order(status="partially_refunded", refunded_amount=300, refunded_tax_amount=63)
    event = parse("order.refunded", partial)
    assert event == FiatEvent(
        kind="refunded", provider="polar", order_id=ORDER["id"], amount_cents=600, refunded_cents=300
    )


def test_a_lost_dispute_takes_back_the_order() -> None:
    assert parse("refund.created", copy.deepcopy(REFUND)) == FiatEvent(
        kind="disputed", provider="polar", order_id=ORDER["id"]
    )
    assert parse("refund.created", {**copy.deepcopy(REFUND), "dispute": None}) is None


@pytest.mark.parametrize("event_type", ["order.created", "order.updated", "checkout.updated", "customer.created"])
def test_other_events_change_nothing(event_type: str) -> None:
    assert parse(event_type, order()) is None


def test_forged_stale_and_unreadable_webhooks_are_refused() -> None:
    built, now = provider(), time.time()
    body, headers = signed("order.paid", order(), now)
    with pytest.raises(FiatWebhookError):
        built.parse_webhook({**headers, "webhook-signature": "v1,AAAA"}, body, now)
    with pytest.raises(FiatWebhookError):
        built.parse_webhook(headers, body.replace(b"600", b"2200"), now)
    with pytest.raises(FiatWebhookError):
        built.parse_webhook(headers, body, now + 600)
    broken, broken_headers = signed("order.paid", {"id": "x"}, now)
    with pytest.raises(FiatWebhookError):
        built.parse_webhook(broken_headers, broken, now)


def test_tax_behaviour_with_the_org_default() -> None:
    assert polar.tax_behaviour_matches(False, "exclusive")
    assert polar.tax_behaviour_matches(False, None)
    assert not polar.tax_behaviour_matches(False, None, org_inclusive=True)
    assert not polar.tax_behaviour_matches(False, "inclusive")
