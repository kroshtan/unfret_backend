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
SUCCESS_URL = "https://unfret.net/checkout?paid=1"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "polar_access_token": SecretStr("polar_oat_test"),
        "polar_products": ",".join(f"{size}={product}" for size, product in PRODUCTS.items()),
        "fiat_webhook_secret": SecretStr(SECRET),
        # The mechanics below are tested at fixed prices (dollars before tax, euros with VAT, an exclusive org
        # default); the shipped defaults (all tax-inclusive) have their own tests at the end.
        **OLD_SETUP,
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


OLD_SETUP: dict[str, Any] = {
    "card_prices_usd": "25=6.00,100=20.00",
    "card_prices_eur": "25=6.00,100=20.00",
    "card_prices_usd_tax_inclusive": False,
    "polar_org_tax_inclusive": False,
}


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


@pytest.mark.parametrize("bad", ["25", "25=", "x=abc", f"25={PRODUCTS[25]},25={PRODUCTS[50]}", "=abc"])
def test_malformed_products_are_refused_at_start_up(bad: str) -> None:
    with pytest.raises(ValueError, match="polar_products"):
        settings(polar_products=bad)


def test_a_product_for_a_pack_off_sale_is_ignored() -> None:
    # Dev and production still name the 50-pack's product until it is archived in Polar: card payments stay on.
    built = provider()
    assert settings().polar_configured
    assert built.products == {25: PRODUCTS[25], 100: PRODUCTS[100]}
    assert PRODUCTS[50] not in built.packs_by_product
    price = {"subtotal_amount": 1150, "net_amount": 1150}
    assert parse("order.paid", order(product_id=PRODUCTS[50], **price)) is None  # left for a person to look at
    assert not settings(polar_products=f"25={PRODUCTS[25]},50={PRODUCTS[50]}").polar_configured  # 100 is missing


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
            # The selected price's tax behaviour: the euro prices include VAT, the dollar ones are left to the default.
            "product_price": {"tax_behavior": "inclusive" if sent.get("currency") == "eur" else None},
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
        "metadata": {"account": ACCOUNT, "site": settings().public_base_url},
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

    chosen = pack(settings(), 100)
    assert chosen is not None
    with pytest.raises(FiatProviderError):
        await provider(httpx.MockTransport(down)).create_checkout(ACCOUNT, chosen, SUCCESS_URL)


# ── Webhooks ──────────────────────────────────────────────────────────────────────────────────────────────────


def test_order_paid_is_the_pre_tax_amount_and_nothing_about_the_buyer() -> None:
    event = parse("order.paid", order())
    assert event == FiatEvent(
        kind="paid",
        provider="polar",
        order_id=ORDER["id"],
        amount_cents=600,
        price_cents=600,  # before tax: a dollar price is matched on the amount before sales tax/VAT
        checkout_id=CHECKOUT_ID,
        account=ACCOUNT,
        fee_cents=69,  # Polar's fee
    )
    assert "jan" not in repr(event).lower()


def test_a_fee_in_another_currency_is_left_to_the_estimate() -> None:
    event = parse("order.paid", order(platform_fee_currency="eur"))
    assert event is not None and event.fee_cents is None
    assert (no_fee := parse("order.paid", order(platform_fee_amount=0))) is not None and no_fee.fee_cents is None


@pytest.mark.parametrize("size", [25, 100])
def test_each_pack_matches_its_own_card_price(size: int) -> None:
    price = {25: 600, 100: 2000}[size]
    event = parse("order.paid", order(product_id=PRODUCTS[size], subtotal_amount=price, net_amount=price))
    assert event is not None and event.amount_cents == price


def test_an_order_at_the_100_packs_old_price_credits_nothing() -> None:
    # The 100-pack's product still at $22 / €17.50 in Polar while the pack costs $20 / €20: never credited as is.
    assert parse("order.paid", order(product_id=PRODUCTS[100], subtotal_amount=2200, net_amount=2200)) is None
    euro = {**EURO, "product_id": PRODUCTS[100], "subtotal_amount": 1750, "net_amount": 1446, "tax_amount": 304}
    assert parse("order.paid", order(**euro)) is None


@pytest.mark.parametrize(("currency", "polar_cents"), [("usd", 2200), ("eur", 1750)])
async def test_a_100_pack_checkout_is_refused_while_its_polar_price_is_the_old_one(
    currency: str, polar_cents: int
) -> None:
    # Until the product's price is changed in Polar, the checkout comes back at the old price: refused before anyone
    # pays, so nobody is charged $22 for what is now a $20 pack (the route answers 503 and offers crypto).
    seen: list[httpx.Request] = []
    built = provider(polar_api(seen, amount=polar_cents))
    chosen = pack(settings(), 100)
    assert chosen is not None and chosen.card_cents(currency) == {"usd": 2000, "eur": 2000}[currency]
    with pytest.raises(FiatProviderError, match="doesn't match"):
        await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, currency)  # type: ignore[arg-type]
    assert [json.loads(r.content)["products"] for r in seen] == [[PRODUCTS[100]]]


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "eur"},  # the dollar price's number in euros isn't the pack's euro price
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


# ── Euros (iDEAL | Wero for Dutch buyers) ─────────────────────────────────────────────────────────────────────


async def test_a_euro_checkout_is_the_packs_product_in_eur_with_the_same_metadata() -> None:
    seen: list[httpx.Request] = []
    built = provider(polar_api(seen, amount=600))
    chosen = pack(settings(), 25)
    assert chosen is not None

    checkout = await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur")

    assert checkout.checkout_id == CHECKOUT_ID
    (request,) = seen
    assert json.loads(request.content) == {
        "products": [PRODUCTS[25]],
        "currency": "eur",
        "metadata": {"account": ACCOUNT, "site": settings().public_base_url},
        "success_url": SUCCESS_URL,
        "allow_discount_codes": False,
    }


async def test_a_wero_checkout_prefills_only_the_billing_country() -> None:
    # Polar shows iDEAL | Wero only for a EUR checkout whose billing country is NL; AddressInput needs only country.
    seen: list[httpx.Request] = []
    built = provider(polar_api(seen, amount=600))
    chosen = pack(settings(), 25)
    assert chosen is not None

    await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur", "NL")
    await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur")

    with_country, without = (json.loads(r.content) for r in seen)
    assert with_country == {
        "products": [PRODUCTS[25]],
        "currency": "eur",
        "metadata": {"account": ACCOUNT, "site": settings().public_base_url},
        "success_url": SUCCESS_URL,
        "allow_discount_codes": False,
        "customer_billing_address": {"country": "NL"},
    }
    assert "customer_billing_address" not in without  # a card checkout leaves the country to the buyer


@pytest.mark.parametrize(
    "reply",
    [
        {"currency": "usd", "amount": 550},  # Polar fell back to the dollar price
        {"currency": "eur", "amount": 550},  # the product's euro price isn't the pack's (the old €5.50)
        {"status": 422},  # e.g. the product has no euro price
    ],
)
async def test_a_euro_checkout_at_the_wrong_price_is_refused(reply: dict[str, Any]) -> None:
    status = reply.pop("status", 201)
    built = provider(polar_api([], status=status, **reply))
    chosen = pack(settings(), 25)
    assert chosen is not None
    with pytest.raises(FiatProviderError):
        await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur")


async def test_no_euro_checkout_without_euro_prices() -> None:
    seen: list[httpx.Request] = []
    chosen = pack(settings(card_prices_eur=""), 25)
    assert chosen is not None
    with pytest.raises(FiatProviderError):
        await provider(polar_api(seen), card_prices_eur="").create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur")
    assert seen == []  # Polar isn't even asked


# The 25-pack at €6.00 VAT included, bought from the Netherlands: Polar takes the 21% VAT out of the price.
EURO = {"currency": "eur", "subtotal_amount": 600, "net_amount": 496, "tax_amount": 104, "total_amount": 600}


@pytest.mark.parametrize(
    ("size", "cents", "net", "tax"),
    [
        (25, 600, 496, 104),  # the Netherlands, 21%
        (100, 2000, 1653, 347),
        (25, 600, 504, 96),  # Germany, 19%: another net amount, the same price
        (100, 2000, 1594, 406),  # Finland, 25.5%
        (25, 600, 600, 0),  # a business with a VAT number (reverse charge): no VAT, the same price
    ],
)
def test_a_euro_order_matches_its_packs_euro_price_with_vat_included(
    size: int, cents: int, net: int, tax: int
) -> None:
    changes = {"product_id": PRODUCTS[size], "subtotal_amount": cents, "net_amount": net, "tax_amount": tax}
    event = parse("order.paid", order(**{**EURO, **changes, "total_amount": cents}))
    assert event == FiatEvent(
        kind="paid",
        provider="polar",
        order_id=ORDER["id"],
        amount_cents=net,  # before VAT: what refunds count from
        price_cents=cents,  # what the buyer paid, VAT included: the pack's euro price
        currency="eur",
        checkout_id=CHECKOUT_ID,
        account=ACCOUNT,
    )
    assert event.paid_price_cents == cents


def test_a_euro_order_with_vat_on_top_of_the_price_credits_nothing() -> None:
    # The product's EUR price left exclusive in Polar: the buyer paid €7.26 for a pack shown at €6.00.
    assert parse("order.paid", order(**{**EURO, "net_amount": 600, "tax_amount": 126, "total_amount": 726})) is None


def test_euro_prices_before_vat_are_matched_before_vat() -> None:
    # card_prices_eur_tax_inclusive off: the euro prices are before VAT, like the dollar ones.
    now = time.time()
    body, headers = signed("order.paid", order(**{**EURO, "net_amount": 600, "tax_amount": 126}), now)
    event = provider(card_prices_eur_tax_inclusive=False).parse_webhook(headers, body, now)
    assert event is not None and (event.amount_cents, event.paid_price_cents) == (600, 600)
    body, headers = signed("order.paid", order(**EURO), now)
    assert provider(card_prices_eur_tax_inclusive=False).parse_webhook(headers, body, now) is None


@pytest.mark.parametrize(
    "changes",
    [
        {**EURO, "net_amount": 600},  # the price before VAT, with the VAT on top
        {**EURO, "subtotal_amount": 550, "net_amount": 455, "tax_amount": 95},  # the old €5.50 price
        {**EURO, "net_amount": 500, "discount_amount": 50},  # a discount
        {**EURO, "product_id": PRODUCTS[100]},  # the 25-pack's euro price for the 100-pack
        {**EURO, "product_id": PRODUCTS[50], "net_amount": 826, "tax_amount": 174},  # the 50-pack, off sale
        {"currency": "gbp", "net_amount": 550},  # a currency we don't sell in
        {"currency": "gbp"},
    ],
)
def test_a_euro_order_that_isnt_exactly_a_packs_euro_price_credits_nothing(changes: dict[str, Any]) -> None:
    assert parse("order.paid", order(**changes)) is None


def test_no_euro_orders_without_euro_prices() -> None:
    now = time.time()
    body, headers = signed("order.paid", order(**EURO), now)
    assert provider(card_prices_eur="").parse_webhook(headers, body, now) is None


def test_euro_refunds_are_in_the_orders_own_cents() -> None:
    # Polar reports the refund before VAT, like net_amount: half the order is half of both.
    refunded = order(**EURO, status="partially_refunded", refunded_amount=248, refunded_tax_amount=52)
    event = parse("order.refunded", refunded)
    assert event is not None and (event.amount_cents, event.refunded_cents) == (496, 248)


# ── Tax behaviour of the product's price ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("behaviour", [None, "exclusive", "location"])
async def test_a_euro_checkout_whose_price_isnt_tax_inclusive_is_refused(behaviour: str | None) -> None:
    # A €6.00 price Polar would add VAT to (exclusive, or the organisation's default, which the token can't read):
    # refused before anyone pays €7.26 for a pack shown at €6.00 (the route falls back to the dollar checkout).
    built = provider(polar_api([], product_price={"tax_behavior": behaviour}))
    chosen = pack(settings(), 25)
    assert chosen is not None
    with pytest.raises(FiatProviderError, match="tax behaviour"):
        await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur")


async def test_a_euro_checkout_at_the_tax_inclusive_price_goes_ahead() -> None:
    seen: list[httpx.Request] = []
    chosen = pack(settings(), 100)
    assert chosen is not None and chosen.card_cents("eur") == 2000 and chosen.tax_inclusive("eur")
    checkout = await provider(polar_api(seen, amount=2000)).create_checkout(
        ACCOUNT, chosen, SUCCESS_URL, "eur", billing_country="NL"
    )
    assert checkout.checkout_id == CHECKOUT_ID
    (request,) = seen
    assert json.loads(request.content)["currency"] == "eur"


@pytest.mark.parametrize(
    ("behaviour", "ok"), [(None, True), ("exclusive", True), ("location", True), ("inclusive", False)]
)
async def test_a_dollar_checkout_needs_a_price_before_tax(behaviour: str | None, ok: bool) -> None:
    built = provider(polar_api([], product_price={"tax_behavior": behaviour}))
    chosen = pack(settings(), 25)
    assert chosen is not None and not chosen.tax_inclusive("usd")
    if ok:
        assert (await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL)).checkout_id == CHECKOUT_ID
    else:
        with pytest.raises(FiatProviderError, match="tax behaviour"):
            await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL)


async def test_euro_prices_before_vat_need_a_price_polar_adds_vat_to() -> None:
    built = provider(polar_api([], product_price={"tax_behavior": "exclusive"}), card_prices_eur_tax_inclusive=False)
    chosen = pack(settings(card_prices_eur_tax_inclusive=False), 25)
    assert chosen is not None and not chosen.tax_inclusive("eur")
    assert (await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "eur")).checkout_id == CHECKOUT_ID


# ── The shipped defaults: every card price includes tax, Polar's organisation default is inclusive ──────────────
def shipped(transport: httpx.MockTransport | None = None) -> PolarProvider:
    built = polar.from_settings(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            polar_access_token=SecretStr("polar_oat_test"),
            polar_products=",".join(f"{size}={product}" for size, product in PRODUCTS.items()),
            fiat_webhook_secret=SecretStr(SECRET),
        ),
        transport=transport,
    )
    assert built is not None
    return built


def test_the_defaults_are_tax_inclusive_in_both_currencies() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.card_usd_cents == {25: 650, 100: 2100} and s.card_eur_cents == {25: 650, 100: 2100}
    assert s.card_prices_usd_tax_inclusive and s.card_prices_eur_tax_inclusive and s.polar_org_tax_inclusive
    chosen = pack(s, 25)
    assert chosen is not None and chosen.tax_inclusive("usd") and chosen.tax_inclusive("eur")


@pytest.mark.parametrize(("currency", "cents"), [("usd", 650), ("eur", 650), ("usd", 2100), ("eur", 2100)])
@pytest.mark.parametrize(
    ("behaviour", "ok"), [(None, True), ("inclusive", True), ("exclusive", False), ("location", False)]
)
async def test_with_an_inclusive_org_default_null_counts_as_inclusive(
    currency: str, cents: int, behaviour: str | None, ok: bool
) -> None:
    built = shipped(polar_api([], amount=cents, product_price={"tax_behavior": behaviour}))
    chosen = pack(Settings(_env_file=None), 25 if cents == 650 else 100)  # type: ignore[call-arg]
    assert chosen is not None
    if ok:
        checkout = await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, currency)  # type: ignore[arg-type]
        assert checkout.checkout_id == CHECKOUT_ID
    else:
        with pytest.raises(FiatProviderError, match="tax behaviour"):
            await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, currency)  # type: ignore[arg-type]


def test_tax_behaviour_with_the_org_default() -> None:
    assert polar.tax_behaviour_matches(True, None, org_inclusive=True)
    assert not polar.tax_behaviour_matches(True, None, org_inclusive=False)
    assert not polar.tax_behaviour_matches(False, None, org_inclusive=True)
    assert polar.tax_behaviour_matches(False, None, org_inclusive=False)
    assert not polar.tax_behaviour_matches(True, "exclusive", org_inclusive=True)


@pytest.mark.parametrize(("net", "tax"), [(537, 113), (650, 0), (609, 41)])  # NL 21%; reverse charge; a US state
def test_a_dollar_order_is_credited_at_its_price_with_tax_included(net: int, tax: int) -> None:
    now = time.time()
    data = order(subtotal_amount=650, net_amount=net, tax_amount=tax, total_amount=650)
    body, headers = signed("order.paid", data, now)
    event = shipped().parse_webhook(headers, body, now)
    assert event is not None and (event.amount_cents, event.paid_price_cents) == (net, 650)
    on_top = order(subtotal_amount=650, net_amount=650, tax_amount=137, total_amount=787)
    body, headers = signed("order.paid", on_top, now)
    assert shipped().parse_webhook(headers, body, now) is None  # tax added on top: not the price we show


@pytest.mark.parametrize(("top", "ok"), [("inclusive", True), ("exclusive", False)])
async def test_the_checkouts_own_tax_behaviour_wins(top: str, ok: bool) -> None:
    # Polar's checkout-level tax_behavior is resolved; it decides over the price's (null: the org default).
    built = shipped(polar_api([], amount=650, tax_behavior=top, product_price={"tax_behavior": None}))
    chosen = pack(Settings(_env_file=None), 25)  # type: ignore[call-arg]
    assert chosen is not None
    if ok:
        assert (await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "usd")).checkout_id == CHECKOUT_ID
    else:
        with pytest.raises(FiatProviderError, match="tax behaviour"):
            await built.create_checkout(ACCOUNT, chosen, SUCCESS_URL, "usd")
