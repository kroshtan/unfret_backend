"""Polar as the fiat provider: the checkout request, the webhooks it sends, and when it is on at all."""

import base64
import copy
import json
import time
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from unfret.config import Settings
from unfret.credits import polar
from unfret.credits.fiat import FiatEvent, FiatProviderError, FiatWebhookError, sign_standard_webhook
from unfret.credits.packs import pack
from unfret.credits.polar import PolarProvider

SECRET = "whsec_" + base64.b64encode(b"polar-test-webhook-secret-32byte").decode()
PRODUCTS = {
    25: "2f1c9a6e-6c1b-4a51-9a1e-3f4f0c1d2b25",
    50: "7d0f6e3a-8b2c-4c4e-8f0d-1a2b3c4d5e50",
    100: "9e8d7c6b-5a49-4382-9170-0a1b2c3d4100",
}
ACCOUNT = "key:abcdefghij"
CHECKOUT_ID = "c6a3b2e1-0f9d-4e8c-b7a6-5d4c3b2a1f00"
SUCCESS_URL = "https://unfret.kroshtan.com/checkout?paid=1"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "polar_access_token": SecretStr("polar_oat_test"),
        "polar_products": ",".join(f"{size}={product}" for size, product in PRODUCTS.items()),
        "fiat_webhook_secret": SecretStr(SECRET),
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def provider(transport: httpx.MockTransport | None = None, **overrides: Any) -> PolarProvider:
    built = polar.from_settings(settings(**overrides), transport=transport)
    assert built is not None
    return built


# An order as Polar's webhooks carry it (OpenAPI 2026-10, schema Order): the 25-pack, bought from the Netherlands.
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
    "product_id": PRODUCTS[25],
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
    "product": {"id": PRODUCTS[25], "name": "Unfret: 25 checks", "is_recurring": False, "metadata": {}},
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
    [
        {"polar_access_token": SecretStr("")},
        {"fiat_webhook_secret": SecretStr("")},
        {"polar_products": ""},
        {"polar_products": f"25={PRODUCTS[25]},50={PRODUCTS[50]}"},  # no product for the 100-pack
    ],
)
def test_off_unless_fully_configured(missing: dict[str, Any]) -> None:
    assert polar.from_settings(settings(**missing)) is None


def test_on_when_configured_and_reports_no_secret() -> None:
    built = provider(polar_environment="production")
    assert built.status == "polar:production"
    assert settings(polar_environment="production").summary()["fiat"] == "production"
    assert settings(polar_products="").summary()["fiat"] == "off"
    assert settings(polar_environment="").polar_environment == "sandbox"  # an empty deploy secret


@pytest.mark.parametrize("bad", ["25", "25=", "x=abc", f"25={PRODUCTS[25]},25={PRODUCTS[50]}", "30=abc"])
def test_malformed_products_are_refused_at_start_up(bad: str) -> None:
    with pytest.raises(ValueError, match="polar_products"):
        settings(polar_products=bad)


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
            "amount": 600,
            "net_amount": 600,
            "tax_amount": None,
            "total_amount": 600,
            "product_id": sent["products"][0],
            "metadata": sent.get("metadata", {}),
            "success_url": sent.get("success_url"),
            **reply,
        }
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    ("environment", "base"), [("sandbox", "https://sandbox-api.polar.sh"), ("production", "https://api.polar.sh")]
)
async def test_checkout_is_the_packs_product_in_usd_with_our_account_only(environment: str, base: str) -> None:
    seen: list[httpx.Request] = []
    built = provider(polar_api(seen), polar_environment=environment)
    chosen = pack(settings(), 25)
    assert chosen is not None

    checkout = await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL)

    assert checkout.checkout_id == CHECKOUT_ID
    assert checkout.url == f"https://buy.polar.sh/polar_c_{CHECKOUT_ID}"
    (request,) = seen
    assert str(request.url) == f"{base}/v1/checkouts/"
    assert request.method == "POST"
    assert request.headers["authorization"] == "Bearer polar_oat_test"
    assert json.loads(request.content) == {
        "products": [PRODUCTS[25]],
        "currency": "usd",
        "metadata": {"account": ACCOUNT},
        "success_url": SUCCESS_URL,
        "allow_discount_codes": False,
    }  # nothing about the buyer


@pytest.mark.parametrize(
    "reply",
    [
        {"currency": "eur"},  # a product priced in another currency
        {"amount": 0},  # a zero price (e.g. a stray free price on the product)
        {"amount": 550},  # a price that isn't the pack's card price
        {"status": 422},
    ],
)
async def test_a_checkout_at_the_wrong_price_is_refused_before_anyone_pays(reply: dict[str, Any]) -> None:
    seen: list[httpx.Request] = []
    status = reply.pop("status", 201)
    built = provider(polar_api(seen, status=status, **reply))
    chosen = pack(settings(), 25)
    assert chosen is not None
    with pytest.raises(FiatProviderError):
        await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL)


async def test_polar_unreachable() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    chosen = pack(settings(), 50)
    assert chosen is not None
    with pytest.raises(FiatProviderError):
        await provider(httpx.MockTransport(down)).create_checkout(ACCOUNT, chosen, SUCCESS_URL)


# ── Webhooks ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_order_paid_is_the_pre_tax_amount_and_nothing_about_the_buyer() -> None:
    event = parse("order.paid", order())
    assert event == FiatEvent(
        kind="paid", provider="polar", order_id=ORDER["id"], amount_cents=600, checkout_id=CHECKOUT_ID, account=ACCOUNT
    )
    assert "jan" not in repr(event).lower()


@pytest.mark.parametrize("size", [50, 100])
def test_each_pack_matches_its_own_card_price(size: int) -> None:
    price = {50: 1150, 100: 2200}[size]
    event = parse("order.paid", order(product_id=PRODUCTS[size], subtotal_amount=price, net_amount=price))
    assert event is not None and event.amount_cents == price


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "eur"},  # not USD, even at the right number
        {"subtotal_amount": 0, "net_amount": 0, "tax_amount": 0, "total_amount": 0},  # a zero-price order
        {"net_amount": 500, "discount_amount": 100},  # a discount
        {"product_id": PRODUCTS[100]},  # the 25-pack's price for the 100-pack
        {"product_id": "00000000-0000-4000-8000-000000000000"},  # not one of our products
        {"product_id": None},
    ],
)
def test_an_order_that_isnt_exactly_a_pack_in_usd_credits_nothing(changes: dict[str, Any]) -> None:
    assert parse("order.paid", order(**changes)) is None


def test_refunds_carry_the_pre_tax_total_refunded_so_far() -> None:
    partial = order(status="partially_refunded", refunded_amount=300, refunded_tax_amount=63)
    event = parse("order.refunded", partial)
    assert event == FiatEvent(
        kind="refunded", provider="polar", order_id=ORDER["id"], amount_cents=600, refunded_cents=300
    )
    full = parse("order.refunded", order(status="refunded", refunded_amount=600, refunded_tax_amount=126))
    assert full is not None and full.refunded_cents == 600


def test_a_lost_dispute_takes_back_the_order() -> None:
    assert parse("refund.created", copy.deepcopy(REFUND)) == FiatEvent(
        kind="disputed", provider="polar", order_id=ORDER["id"]
    )
    updated = copy.deepcopy(REFUND)
    updated["dispute"]["status"] = "under_review"
    assert parse("refund.updated", updated) is None
    assert parse("refund.created", {**copy.deepcopy(REFUND), "dispute": None}) is None  # order.refunded covers it


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
