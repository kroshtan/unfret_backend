import base64
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import DROP_ALL, FakeGraph
from tests.unit import test_polar  # Polar's sample payloads
from unfret.config import Settings, settings
from unfret.credits import fiat, keys, ledger, polar
from unfret.credits.fiat import FakeFiatProvider, FiatEvent
from unfret.credits.packs import EURO_AREA
from unfret.credits.polar import PolarProvider
from unfret.main import app
from unfret.migrations import MIGRATIONS, migrate

SECRET = "whsec_" + base64.b64encode(b"fake-webhook-secret-32-bytes!!!!").decode()
BUYER = {"email": "jan.jansen@example.nl", "name": "Jan Jansen", "billing_address": {"line1": "Kerkstraat 1"}}
PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater"}


@pytest.fixture(autouse=True)
def old_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests run at fixed prices ($6 / $20 before tax, €6 / €20 with VAT); the defaults have their own test."""
    for name, value in test_polar.OLD_SETUP.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
async def provider(client: httpx.AsyncClient) -> AsyncIterator[FakeFiatProvider]:
    app.fiat = FakeFiatProvider(SECRET)
    yield app.fiat
    app.fiat = None


async def balance(pool: asyncpg.Pool, account: str) -> int | None:
    value = await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", account)
    return None if value is None else int(value)


def site_meta(account: str) -> dict[str, str]:
    """The checkout metadata this site's checkouts carry."""
    return {"account": account, "site": settings.public_base_url}


def order(kind: str, order_id: str, **data: Any) -> dict[str, Any]:
    return {"type": kind, "data": {"id": order_id, "customer": BUYER, **data}}


async def send(
    client: httpx.AsyncClient, provider: FakeFiatProvider, event: dict[str, Any], **kw: Any
) -> httpx.Response:
    body, headers = provider.webhook(event, **kw)
    return await client.post("/v1/webhooks/fiat", content=body, headers=headers)


async def checkout(client: httpx.AsyncClient, key_id: str, checks: int = 25) -> httpx.Response:
    return await client.post("/v1/credits/fiat-checkout", json={"key_id": key_id, "checks": checks})


async def test_fiat_is_off_without_a_provider(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await keys.create(pool)
    refused = await checkout(client, issued.key_id)
    assert (refused.status_code, refused.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")
    assert (await client.get("/v1/pricing")).json()["fiat"] == {
        "available": False,
        "min_usd_micros": 6_000_000,
        "currencies": ["usd", "eur"],
        "min_eur_cents": 600,
    }


async def test_card_payments_buy_packs_only(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    odd = await checkout(client, issued.key_id, 10)
    assert (odd.status_code, odd.json()["error_code"], odd.json()["detail"]) == (
        400,
        "BAD_REQUEST",
        "Not a pack size.",
    )
    started = await checkout(client, issued.key_id, 25)
    assert started.status_code == 200
    assert "amount=600&" in started.json()["checkout_url"]  # the 25-pack by card: $6.00


async def test_a_paid_order_credits_the_key_once_without_storing_the_buyer(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    started = (await checkout(client, issued.key_id)).json()
    checkout_id = started["checkout_url"].split("id=")[1].split("&")[0]
    # The order doesn't echo the checkout metadata: the server-side mapping finds the key.
    paid = order("order.paid", "ord_1", checkout_id=checkout_id, total_amount=600)

    first = await send(client, provider, paid)
    retry = await send(client, provider, paid)

    assert first.json() == {"status": "credited"}
    assert retry.json() == {"status": "duplicate"}
    assert await balance(pool, issued.account) == 5_000_000  # the 25-pack: 25 checks at $0.20
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM fiat_payments")]
    rows += [dict(r) for r in await pool.fetch("SELECT * FROM fiat_checkouts")]
    stored = str(rows)
    for detail in ("jan.jansen", "Jan Jansen", "Kerkstraat"):
        assert detail not in stored
    assert rows[0]["account"] == issued.account
    assert rows[0]["amount_cents"] == 600


async def test_metadata_alone_also_names_the_key(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    paid = order("order.paid", "ord_2", metadata=site_meta(issued.account), total_amount=2000)
    assert (await send(client, provider, paid)).json() == {"status": "credited"}
    assert await balance(pool, issued.account) == 20_000_000  # the 100-pack ($20 by card)


@pytest.mark.parametrize("site", ["https://other.unfret.invalid", None])
async def test_another_sites_order_credits_nothing_here(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, site: str | None
) -> None:
    # Dev and production share one Polar organisation, so each receives the other's orders. The metadata's key
    # exists only on the site that made the checkout; elsewhere nothing is credited and no payment row is kept.
    issued = await keys.create(pool)  # even a key with that id here
    metadata = {"account": issued.account, **({"site": site} if site else {})}
    paid = order("order.paid", "ord_other", metadata=metadata, total_amount=600)

    assert (await send(client, provider, paid)).json() == {"status": "other_site"}
    assert await balance(pool, issued.account) is None
    assert await pool.fetchval("SELECT count(*) FROM fiat_payments") == 0


async def test_partial_refunds_debit_pro_rata_and_may_go_negative(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, graph: FakeGraph
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": site_meta(issued.account), "total_amount": 2000}
    await send(client, provider, order("order.paid", "ord_3", **meta))  # the 100-pack: $20 of credits
    await pool.execute("UPDATE wallets SET balance_usd_micros = 1_000_000 WHERE pubkey = $1", issued.account)  # spent

    quarter = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=500, **meta))
    again = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=500, **meta))
    half = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=1000, **meta))

    assert [r.json()["status"] for r in (quarter, again, half)] == ["debited", "unchanged", "debited"]
    assert await balance(pool, issued.account) == 1_000_000 - 10_000_000  # half of $20, the balance below zero
    analysis = await client.post("/v1/analyze", json=PAGE, headers={"Authorization": f"Bearer {issued.key}"})
    # A negative balance pays for nothing: the pass isn't used, and its check would be a shopper's free one.
    assert (analysis.status_code, analysis.json()["error_code"]) == (428, "PROOF_OF_WORK_REQUIRED")
    assert (await client.get("/v1/credits", headers={"Authorization": f"Bearer {issued.key}"})).json()[
        "checks_remaining"
    ] == 0


async def test_a_dispute_takes_back_the_whole_order(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": site_meta(issued.account), "total_amount": 600}
    await send(client, provider, order("order.paid", "ord_4", **meta))
    await send(client, provider, order("order.refunded", "ord_4", refunded_amount=300, **meta))

    disputed = await send(client, provider, order("order.disputed", "ord_4", **meta))
    later_refund = await send(client, provider, order("order.refunded", "ord_4", refunded_amount=600, **meta))

    assert disputed.json() == {"status": "debited"}
    assert later_refund.json() == {"status": "unchanged"}  # never taken back twice
    assert await balance(pool, issued.account) == 0
    row = await pool.fetchrow("SELECT * FROM fiat_payments")
    assert (row["disputed"], row["debited_usd_micros"], row["refunded_cents"]) == (True, 5_000_000, 600)


async def test_forged_stale_or_unplaceable_webhooks(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    paid = order("order.paid", "ord_5", metadata=site_meta(issued.account), total_amount=1000)
    body, headers = provider.webhook(paid)

    forged = await client.post("/v1/webhooks/fiat", content=body, headers={**headers, "webhook-signature": "v1,AAAA"})
    tampered = await client.post("/v1/webhooks/fiat", content=body.replace(b"1000", b"9000"), headers=headers)
    stale = await send(client, provider, paid, now=time.time() - 600)  # a replay outside the window
    wallet = await send(
        client, provider, order("order.paid", "ord_6", metadata={"account": "SomeWallet"}, total_amount=1000)
    )
    refund = await send(client, provider, order("order.refunded", "ord_unknown", refunded_amount=100))

    for rejected in (forged, tampered, stale):  # each fails one check only: a broken check would let it through
        assert (rejected.status_code, rejected.json()) == (
            400,
            {"detail": "Webhook rejected.", "error_code": "BAD_REQUEST"},
        )
    assert wallet.json() == {"status": "unknown_account"}  # fiat tops up API keys only
    assert refund.json() == {"status": "unknown_order"}
    assert await balance(pool, issued.account) is None


async def test_order_rows_are_deleted_after_retention(pool: asyncpg.Pool) -> None:
    issued = await keys.create(pool)
    event = FiatEvent(
        kind="paid",
        provider="fake",
        order_id="ord_7",
        amount_cents=1000,
        account=issued.account,
        site=settings.public_base_url,
    )
    await fiat.apply_event(pool, event, settings)

    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=179))
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 1
    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=181))
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0


# ── Polar, end to end through the routes (its API mocked) ─────────────────────────────────────────────────────


@pytest.fixture
def polar_sent() -> list[dict[str, Any]]:
    """The checkout requests the mocked Polar API received."""
    return []


@pytest.fixture
async def polar_provider(client: httpx.AsyncClient, polar_sent: list[dict[str, Any]]) -> AsyncIterator[PolarProvider]:
    def api(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        polar_sent.append(sent)
        prices = {
            "usd": {test_polar.PRODUCTS[25]: 600, test_polar.PRODUCTS[100]: 2000},
            "eur": {test_polar.PRODUCTS[25]: 600, test_polar.PRODUCTS[100]: 2000},
        }
        price = prices[sent["currency"]][sent["products"][0]]
        body = {
            "id": test_polar.CHECKOUT_ID,
            "url": "https://buy.polar.sh/c",
            "currency": sent["currency"],
            "amount": price,
            "product_price": {"tax_behavior": "inclusive" if sent["currency"] == "eur" else None},
        }
        return httpx.Response(201, json=body)

    built = polar.from_settings(test_polar.settings(), transport=httpx.MockTransport(api))
    assert built is not None
    app.fiat = built
    yield built
    app.fiat = None
    await built.aclose()


async def polar_send(client: httpx.AsyncClient, event_type: str, data: dict[str, Any]) -> httpx.Response:
    body, headers = test_polar.signed(event_type, data, time.time())
    return await client.post("/v1/webhooks/fiat", content=body, headers=headers)


async def test_polar_checkout_then_paid_order_credits_the_key(
    client: httpx.AsyncClient, pool: asyncpg.Pool, polar_provider: PolarProvider
) -> None:
    issued = await keys.create(pool)
    started = await checkout(client, issued.key_id, 25)
    reply = started.json()
    claim = reply.pop("claim")  # confirms the order on the way back; never sent to Polar
    assert reply == {
        "checkout_url": "https://buy.polar.sh/c",
        "provider": "polar",
        "currency": "usd",
        "payment_method": "card",
        "checkout_id": test_polar.CHECKOUT_ID,
    }
    status = f"/v1/credits/fiat-checkouts/{test_polar.CHECKOUT_ID}"
    assert (await client.get(status, headers={"X-Unfret-Claim": claim})).json() == {
        "paid": False,
        "checks": 25,
        "reissued": False,
    }
    # Polar's order without our metadata: the checkout mapping names the key.
    paid = test_polar.order(metadata={})

    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "credited"}
    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "duplicate"}
    assert await balance(pool, issued.account) == 5_000_000
    assert (await client.get(status)).status_code == 404  # the checkout id alone (Polar knows it) shows nothing
    shown = (await client.get(status, headers={"X-Unfret-Claim": claim})).json()
    assert shown == {"paid": True, "checks": 25, "reissued": False}
    assert "Kerkstraat" not in str([dict(r) for r in await pool.fetch("SELECT * FROM fiat_payments")])

    refunded = test_polar.order(status="partially_refunded", refunded_amount=300, refunded_tax_amount=63)
    assert (await polar_send(client, "order.refunded", refunded)).json() == {"status": "debited"}
    assert await balance(pool, issued.account) == 2_500_000
    assert (await polar_send(client, "refund.created", test_polar.REFUND)).json() == {"status": "debited"}
    assert await balance(pool, issued.account) == 0
    assert (await client.get("/v1/health/ready")).json()["fiat"] == "polar:sandbox"


@pytest.mark.parametrize(
    "changes",
    [{"currency": "eur"}, {"subtotal_amount": 0, "net_amount": 0, "tax_amount": 0, "total_amount": 0}],
)
async def test_polar_non_usd_or_zero_orders_credit_nothing(
    client: httpx.AsyncClient, pool: asyncpg.Pool, polar_provider: PolarProvider, changes: dict[str, Any]
) -> None:
    issued = await keys.create(pool)
    await checkout(client, issued.key_id, 25)
    response = await polar_send(client, "order.paid", test_polar.order(**changes))
    assert response.status_code == 200 and response.json() == {"status": "ignored"}
    assert await balance(pool, issued.account) is None
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0


async def test_polar_checkout_failure_is_a_503(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token"})

    app.fiat = polar.from_settings(test_polar.settings(), transport=httpx.MockTransport(refuse))
    try:
        issued = await keys.create(pool)
        response = await checkout(client, issued.key_id, 25)
    finally:
        app.fiat = None
    assert (response.status_code, response.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_checkouts") == 0


@pytest.mark.parametrize("country", ["US", "NL"])
async def test_a_pack_whose_polar_price_is_still_the_old_one_is_refused_before_anyone_pays(
    client: httpx.AsyncClient, pool: asyncpg.Pool, country: str
) -> None:
    # Until the operator changes the 100-pack's product in Polar, its checkouts come back at $22 / €17.50: the euro
    # checkout falls back to dollars, the dollar one is refused too, and the buyer gets a calm 503 (and crypto). The
    # 25-pack's product, unchanged, still sells.
    old = {"usd": {test_polar.PRODUCTS[25]: 600, test_polar.PRODUCTS[100]: 2200}}
    old["eur"] = {test_polar.PRODUCTS[25]: 600, test_polar.PRODUCTS[100]: 1750}
    asked: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        asked.append(sent["currency"])
        price = old[sent["currency"]][sent["products"][0]]
        body = {
            "id": test_polar.CHECKOUT_ID,
            "url": "https://buy.polar.sh/c",
            "currency": sent["currency"],
            "amount": price,
            "product_price": {"tax_behavior": "inclusive" if sent["currency"] == "eur" else None},
        }
        return httpx.Response(201, json=body)

    app.fiat = polar.from_settings(test_polar.settings(), transport=httpx.MockTransport(api))
    try:
        issued = await keys.create(pool)
        body = {"key_id": issued.key_id, "checks": 100}
        refused = await client.post("/v1/credits/fiat-checkout", json=body, headers=from_country(country))
        assert asked == (["eur", "usd"] if country == "NL" else ["usd"])
        assert await pool.fetchval("SELECT COUNT(*) FROM fiat_checkouts") == 0
        sold = await client.post(
            "/v1/credits/fiat-checkout", json={**body, "checks": 25}, headers=from_country(country)
        )
    finally:
        app.fiat = None
    assert (refused.status_code, refused.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")
    assert "pay with SOL or USDC" in refused.json()["detail"]
    assert sold.status_code == 200 and sold.json()["currency"] == ("eur" if country == "NL" else "usd")


@pytest.mark.parametrize("case", [(25, 600, 496, 104), (100, 2000, 1594, 406)])
async def test_a_euro_order_paid_with_vat_included_credits_the_pack_and_refunds_count_before_vat(
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    polar_provider: PolarProvider,
    polar_sent: list[dict[str, Any]],
    case: tuple[int, int, int, int],
) -> None:
    checks, cents, net, tax = case
    # €6 / €20 VAT included (the Netherlands at 21%, Finland at 25.5%): Polar's net amount is the price less the VAT.
    issued = await keys.create(pool)
    started = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": checks}, headers=from_country("NL")
    )
    assert started.status_code == 200 and started.json()["currency"] == "eur"
    assert polar_sent[-1]["currency"] == "eur"
    amounts = {"subtotal_amount": cents, "net_amount": net, "tax_amount": tax, "total_amount": cents}
    paid = test_polar.order(currency="eur", product_id=test_polar.PRODUCTS[checks], metadata={}, **amounts)
    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "credited"}
    value = checks * 200_000
    assert await balance(pool, issued.account) == value  # 25 or 100 checks, as by dollars
    row = await pool.fetchrow("SELECT amount_cents, currency, credited_usd_micros FROM fiat_payments")
    assert (row["amount_cents"], row["currency"], row["credited_usd_micros"]) == (net, "eur", value)
    half = net // 2
    refunded = test_polar.order(**{**paid, "status": "partially_refunded", "refunded_amount": half})
    assert (await polar_send(client, "order.refunded", refunded)).json() == {"status": "debited"}
    assert await balance(pool, issued.account) == value - value * half // net


async def test_an_old_link_to_the_50_pack_is_not_a_pack_for_the_api(
    client: httpx.AsyncClient, pool: asyncpg.Pool, polar_provider: PolarProvider
) -> None:
    issued = await keys.create(pool)
    refused = await client.post("/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 50})
    assert (refused.status_code, refused.json()["metadata"]) == (400, {"packs": [25, 100]})


async def test_ready_says_fiat_is_off_without_polar(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/health/ready")).json()["fiat"] == "off"


# ── Euros: card and iDEAL | Wero for visitors from the euro area ──────────────────────────────────────────────


def from_country(country: str) -> dict[str, str]:
    """Headers of a request that came through Cloudflare from ``country``."""
    return {"X-Edge-Peer": "172.64.1.2", "CF-IPCountry": country}


@pytest.mark.parametrize(("country", "currency"), [("NL", "eur"), ("BE", "eur"), ("US", "usd"), ("DK", "usd")])
async def test_pricing_has_euro_card_prices_and_the_visitors_card_currency(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, country: str, currency: str
) -> None:
    for name in test_polar.OLD_SETUP:  # the shipped defaults
        monkeypatch.setattr(settings, name, Settings.model_fields[name].default)
    body = (await client.get("/v1/pricing", headers=from_country(country))).json()
    assert body["card_currency"] == currency
    assert body["vat"]["country_hint"] == country
    packs = [(p["checks"], p["price_usd"], p["price_eur"], p["price_eur_cents"]) for p in body["packs"]]
    assert packs == [(25, "$6.50", "€6.50", 650), (100, "$21.00", "€21.00", 2100)]
    assert body["card_tax_inclusive"] == {"usd": True, "eur": True}  # the final price everywhere
    assert body["packs"][0]["price_usd_micros"] == 6_500_000  # every dollar field stays
    assert (await client.get("/v1/pricing")).json()["card_currency"] == "usd"  # no hint: dollars


async def test_pricing_without_euro_prices(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "card_prices_eur", "")
    body = (await client.get("/v1/pricing", headers=from_country("NL"))).json()
    assert body["card_currency"] == "usd"
    assert body["fiat"]["currencies"] == ["usd"] and body["fiat"]["min_eur_cents"] is None
    assert {p["price_eur"] for p in body["packs"]} == {None}


async def test_euro_area_visitors_get_euro_checkouts_and_anyone_may_choose(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)

    async def start(headers: dict[str, str], **extra: Any) -> dict[str, Any]:
        body = {"key_id": issued.key_id, "checks": 25, **extra}
        response = await client.post("/v1/credits/fiat-checkout", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return dict(response.json())

    dutch = await start(from_country("NL"))
    assert dutch["currency"] == "eur" and "amount=600&currency=eur&" in dutch["checkout_url"]
    american = await start(from_country("US"))
    assert american["currency"] == "usd" and "amount=600&currency=usd&" in american["checkout_url"]
    assert (await start({}, currency="EUR"))["currency"] == "eur"  # what the page showed, case-insensitive
    assert (await start(from_country("NL"), currency="usd"))["currency"] == "usd"
    wrong = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25, "currency": "gbp"}
    )
    assert wrong.status_code == 422


async def test_no_euro_checkout_while_euro_prices_are_off(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "card_prices_eur", "")
    issued = await keys.create(pool)
    asked = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25, "currency": "eur"}
    )
    assert (asked.status_code, asked.json()["detail"]) == (400, "Card payments are in US dollars only.")
    dutch = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25}, headers=from_country("NL")
    )
    assert dutch.json()["currency"] == "usd"


# ── Local payment methods (iDEAL | Wero in the Netherlands) ─────────────────────────────────────────────────


@pytest.mark.parametrize(("country", "method"), [("NL", "wero"), ("BE", None), ("US", None), (None, None)])
async def test_pricing_names_the_visitors_local_payment_method(
    client: httpx.AsyncClient, country: str | None, method: str | None
) -> None:
    body = (await client.get("/v1/pricing", headers=from_country(country) if country else {})).json()
    expected = {"id": "wero", "label": "iDEAL | Wero", "currency": "eur"} if method else None
    assert body["local_payment_method"] == expected


@pytest.mark.parametrize("headers", [{}, from_country("US"), from_country("NL")])
async def test_pricing_lists_every_countrys_card_currency_and_local_method(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    # The same tables whatever the visitor's country: the page picks from them for the country the visitor chose.
    body = (await client.get("/v1/pricing", headers=headers)).json()
    assert body["euro_area"] == sorted(EURO_AREA) and "NL" in body["euro_area"] and "US" not in body["euro_area"]
    assert body["local_payment_methods"] == {"NL": {"id": "wero", "label": "iDEAL | Wero", "currency": "eur"}}


async def test_no_euro_countries_or_euro_methods_while_euro_prices_are_off(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "card_prices_eur", "")
    body = (await client.get("/v1/pricing", headers=from_country("NL"))).json()
    assert (body["euro_area"], body["local_payment_methods"]) == ([], {})


async def test_no_local_method_while_its_currency_is_off(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "card_prices_eur", "")
    assert (await client.get("/v1/pricing", headers=from_country("NL"))).json()["local_payment_method"] is None


async def test_wero_is_a_euro_checkout_with_the_netherlands_prefilled(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)

    async def start(headers: dict[str, str], **extra: Any) -> httpx.Response:
        body = {"key_id": issued.key_id, "checks": 25, **extra}
        return await client.post("/v1/credits/fiat-checkout", json=body, headers=headers)

    wero = (await start(from_country("NL"), payment_method="wero", currency="eur")).json()
    assert (wero["currency"], wero["payment_method"]) == ("eur", "wero")
    assert "amount=600&currency=eur&" in wero["checkout_url"] and wero["checkout_url"].endswith("&country=NL")
    anywhere = (await start({}, payment_method="Wero")).json()  # its currency by default, from any country
    assert anywhere["currency"] == "eur" and "country=NL" in anywhere["checkout_url"]
    card = (await start(from_country("NL"), payment_method="card")).json()
    assert (card["currency"], card["payment_method"]) == ("eur", "card") and "country=" not in card["checkout_url"]
    assert "country=" not in (await start(from_country("NL"))).json()["checkout_url"]  # card by default

    dollars = await start(from_country("NL"), payment_method="wero", currency="usd")
    assert (dollars.status_code, dollars.json()["detail"]) == (400, "iDEAL | Wero is paid in EUR.")
    assert (await start({}, payment_method="paypal")).status_code == 422


async def test_no_wero_while_euro_prices_are_off(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "card_prices_eur", "")
    issued = await keys.create(pool)
    asked = await client.post(
        "/v1/credits/fiat-checkout",
        json={"key_id": issued.key_id, "checks": 25, "payment_method": "wero"},
        headers=from_country("NL"),
    )
    assert asked.status_code == 400
    assert asked.json()["detail"] == "iDEAL | Wero isn't available: card payments are in US dollars only."


async def test_polar_wero_checkout_sends_the_billing_country_and_credits_as_usual(
    client: httpx.AsyncClient, pool: asyncpg.Pool, polar_provider: PolarProvider, polar_sent: list[dict[str, Any]]
) -> None:
    issued = await keys.create(pool)
    for method in ("wero", "card"):
        started = await client.post(
            "/v1/credits/fiat-checkout",
            json={"key_id": issued.key_id, "checks": 25, "payment_method": method},
            headers=from_country("NL"),
        )
        assert (started.json()["currency"], started.json()["payment_method"]) == ("eur", method)
    wero, card = polar_sent
    assert wero["customer_billing_address"] == {"country": "NL"} and wero["currency"] == "eur"
    assert wero["metadata"] == site_meta(issued.account) and wero["allow_discount_codes"] is False
    assert {k: v for k, v in wero.items() if k != "customer_billing_address"} == card

    euro = {"currency": "eur", "subtotal_amount": 600, "net_amount": 496, "tax_amount": 104, "total_amount": 600}
    paid = test_polar.order(metadata={}, **euro)
    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "credited"}
    assert await balance(pool, issued.account) == 5_000_000


async def test_a_euro_order_credits_the_packs_value(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    started = (
        await client.post(
            "/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25}, headers=from_country("NL")
        )
    ).json()
    checkout_id = started["checkout_url"].split("id=")[1].split("&")[0]
    paid = order("order.paid", "ord_eur_1", checkout_id=checkout_id, total_amount=600, currency="eur")

    assert (await send(client, provider, paid)).json() == {"status": "credited"}
    assert (await send(client, provider, paid)).json() == {"status": "duplicate"}
    assert await balance(pool, issued.account) == 5_000_000  # 25 checks, as the $6.00 pack
    row = await pool.fetchrow("SELECT amount_cents, currency, credited_usd_micros FROM fiat_payments")
    assert dict(row) == {"amount_cents": 600, "currency": "eur", "credited_usd_micros": 5_000_000}
    status = f"/v1/credits/fiat-checkouts/{checkout_id}"
    shown = (await client.get(status, headers={"X-Unfret-Claim": started["claim"]})).json()
    assert shown["paid"] and shown["checks"] == 25


@pytest.mark.parametrize(
    "case",
    [
        (550, "eur", "too_small"),
        (726, "eur", "too_small"),
        (500, "eur", "too_small"),
        (600, "gbp", "unknown_currency"),
    ],
)
async def test_a_euro_order_that_isnt_exactly_a_pack_credits_nothing(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, case: tuple[int, str, str]
) -> None:
    amount, currency, outcome = case
    issued = await keys.create(pool)
    paid = order("order.paid", "ord_eur_x", metadata=site_meta(issued.account), total_amount=amount, currency=currency)
    assert (await send(client, provider, paid)).json() == {"status": outcome}
    assert await balance(pool, issued.account) is None
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0


async def test_euro_refunds_debit_pro_rata_and_a_dispute_takes_back_the_rest(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": site_meta(issued.account), "total_amount": 2000, "currency": "eur"}
    assert (await send(client, provider, order("order.paid", "ord_eur_2", **meta))).json() == {"status": "credited"}
    assert await balance(pool, issued.account) == 20_000_000  # the 100-pack

    half = await send(client, provider, order("order.refunded", "ord_eur_2", refunded_amount=1000, **meta))
    assert half.json() == {"status": "debited"}
    assert await balance(pool, issued.account) == 10_000_000  # half of the euros back: half of the credits
    disputed = await send(client, provider, order("order.disputed", "ord_eur_2", **meta))
    assert disputed.json() == {"status": "debited"}
    assert await balance(pool, issued.account) == 0
    row = await pool.fetchrow("SELECT * FROM fiat_payments")
    assert (row["currency"], row["refunded_cents"], row["disputed"], row["debited_usd_micros"]) == (
        "eur",
        1000,
        True,
        20_000_000,
    )


async def test_polar_euro_checkout_then_order_refund_and_dispute(
    client: httpx.AsyncClient, pool: asyncpg.Pool, polar_provider: PolarProvider
) -> None:
    issued = await keys.create(pool)
    started = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25}, headers=from_country("NL")
    )
    assert started.json()["currency"] == "eur"
    euro = {"currency": "eur", "subtotal_amount": 600, "net_amount": 496, "tax_amount": 104, "total_amount": 600}
    paid = test_polar.order(metadata={}, **euro)

    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "credited"}
    assert await balance(pool, issued.account) == 5_000_000
    assert await pool.fetchval("SELECT currency FROM fiat_payments") == "eur"
    # Half the order back: Polar reports the refund before VAT, like the order's net amount.
    refunded = test_polar.order(**euro, status="partially_refunded", refunded_amount=248, refunded_tax_amount=52)
    assert (await polar_send(client, "order.refunded", refunded)).json() == {"status": "debited"}
    assert await balance(pool, issued.account) == 2_500_000
    dispute = {**test_polar.REFUND, "currency": "eur", "amount": 496}
    assert (await polar_send(client, "refund.created", dispute)).json() == {"status": "debited"}
    assert await balance(pool, issued.account) == 0


@pytest.mark.parametrize("method", ["card", "wero"])
async def test_polar_refusing_euros_falls_back_to_a_dollar_checkout(
    client: httpx.AsyncClient, pool: asyncpg.Pool, method: str
) -> None:
    # E.g. a product whose euro price isn't in the Polar dashboard yet: a dollar card checkout beats none.
    seen: list[tuple[str, str | None]] = []

    def api(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        currency = sent["currency"]
        seen.append((currency, (sent.get("customer_billing_address") or {}).get("country")))
        if currency == "eur":
            return httpx.Response(422, json={"error": "no price in eur"})
        body = {"id": test_polar.CHECKOUT_ID, "url": "https://buy.polar.sh/c", "currency": "usd", "amount": 600}
        return httpx.Response(201, json=body)

    app.fiat = polar.from_settings(test_polar.settings(), transport=httpx.MockTransport(api))
    try:
        issued = await keys.create(pool)
        response = await client.post(
            "/v1/credits/fiat-checkout",
            json={"key_id": issued.key_id, "checks": 25, "payment_method": method},
            headers=from_country("NL"),
        )
    finally:
        app.fiat = None
    assert response.status_code == 200, response.text
    assert (response.json()["currency"], response.json()["payment_method"]) == ("usd", "card")
    assert seen == [("eur", "NL" if method == "wero" else None), ("usd", None)]


async def test_migration_21_marks_earlier_card_orders_as_dollars(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 21])
    await pool.execute(
        "INSERT INTO fiat_payments (provider, order_id, account, amount_cents, credited_usd_micros) "
        "VALUES ('polar', 'ord_old', 'key:abcdefghij', 600, 5000000)"
    )
    assert 21 in await migrate(pool)
    assert await pool.fetchval("SELECT currency FROM fiat_payments") == "usd"
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("UPDATE fiat_payments SET currency = 'EURO'")


async def test_migration_22_leaves_earlier_checkouts_unreissuable(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 22])
    await pool.execute(
        "INSERT INTO fiat_checkouts (provider, checkout_id, account, checks, paid_at) "
        "VALUES ('fake', 'co_old', 'key:abcdefghij', 25, NOW())"
    )
    assert 22 in await migrate(pool)
    assert await migrate(pool) == []  # applied once
    row = await pool.fetchrow("SELECT collected_at, reissued_at, new_pass FROM fiat_checkouts")
    assert (row["collected_at"], row["reissued_at"], row["new_pass"]) == (None, None, False)
    with pytest.raises(fiat.ReissueError) as refused:
        await fiat.reissue_pass(pool, "fake", "co_old")
    assert refused.value.reason == "topped_up"


# ── Picking a card purchase up in another browser ─────────────────────────────────────────────────────────────


async def paid_checkout(
    client: httpx.AsyncClient, provider: FakeFiatProvider, pool: asyncpg.Pool, *, pay: bool = True
) -> tuple[keys.IssuedKey, dict[str, Any], str]:
    """A key, its card checkout (paid unless told otherwise) and the checkout's id."""
    issued = await keys.create(pool)
    started = (await checkout(client, issued.key_id)).json()
    checkout_id = started["checkout_id"]
    if pay:
        paid = order("order.paid", f"ord_{checkout_id[:8]}", checkout_id=checkout_id, total_amount=600)
        assert (await send(client, provider, paid)).json() == {"status": "credited"}
    return issued, started, checkout_id


async def checks_left(client: httpx.AsyncClient, key: str) -> int | None:
    response = await client.get("/v1/credits", headers={"Authorization": f"Bearer {key}"})
    return response.json()["checks_remaining"] if response.status_code == 200 else None


async def test_the_return_link_names_the_checkout(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    _, started, checkout_id = await paid_checkout(client, provider, pool, pay=False)
    assert f"checkout%3D{checkout_id}" in started["checkout_url"]  # the provider filled in its placeholder
    assert "paid%3D1" in started["checkout_url"]


async def test_a_purchase_picked_up_in_another_browser_gets_a_fresh_pass(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued, started, checkout_id = await paid_checkout(client, provider, pool)

    picked = await client.post(f"/v1/credits/fiat-checkouts/{checkout_id}/pass")

    assert picked.status_code == 200 and picked.headers["cache-control"] == "no-store"
    body = picked.json()
    assert body["key_id"] == issued.key_id and body["key"] != issued.key
    assert body["recovery_code"] != issued.recovery_code and body["account"] == issued.account
    assert (body["checks"], body["checks_remaining"]) == (25, 25)
    assert await checks_left(client, body["key"]) == 25
    assert await checks_left(client, issued.key) is None  # the buying browser's pass stopped working
    again = await client.post(f"/v1/credits/fiat-checkouts/{checkout_id}/pass")
    assert (again.status_code, again.json()["error_code"]) == (409, "CHECKOUT_COLLECTED")  # once only
    # The buying browser, opened later, learns that its pass was replaced.
    status = f"/v1/credits/fiat-checkouts/{checkout_id}"
    seen = (await client.get(status, headers={"X-Unfret-Claim": started["claim"]})).json()
    assert seen == {"paid": True, "checks": 25, "reissued": True}


async def test_a_purchase_the_buying_browser_picked_up_cant_be_taken_over(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued, started, checkout_id = await paid_checkout(client, provider, pool)
    status = f"/v1/credits/fiat-checkouts/{checkout_id}"
    assert (await client.get(status, headers={"X-Unfret-Claim": started["claim"]})).json()["paid"]

    refused = await client.post(f"/v1/credits/fiat-checkouts/{checkout_id}/pass")

    assert (refused.status_code, refused.json()["error_code"]) == (409, "CHECKOUT_COLLECTED")
    assert await checks_left(client, issued.key) == 25  # untouched


async def test_a_top_up_of_a_pass_in_use_is_never_reissued(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    # A pass bought earlier (and in use in the extension, on a phone) is topped up; the bank app brings the buyer
    # back to another browser. Rotating the pass there would sign every other device out.
    issued, _, _ = await paid_checkout(client, provider, pool)
    topup = (await checkout(client, issued.key_id)).json()["checkout_id"]
    paid = order("order.paid", "ord_topup", checkout_id=topup, total_amount=600)
    assert (await send(client, provider, paid)).json() == {"status": "credited"}

    refused = await client.post(f"/v1/credits/fiat-checkouts/{topup}/pass")

    assert (refused.status_code, refused.json()["error_code"]) == (409, "CHECKOUT_TOPPED_UP")
    assert await checks_left(client, issued.key) == 50  # untouched


@pytest.mark.parametrize("since", ["grant", "order"])
async def test_a_new_pass_that_got_checks_another_way_before_payment_is_not_reissued(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, since: str
) -> None:
    # A pass made for a checkout that was left open, then given checks or paid by another checkout: by
    # the time this one is paid, the pass is in use.
    issued, _, checkout_id = await paid_checkout(client, provider, pool, pay=False)
    if since == "grant":
        await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, 600000)", issued.account)
    else:
        other = (await checkout(client, issued.key_id)).json()["checkout_id"]
        first = order("order.paid", "ord_other", checkout_id=other, total_amount=600)
        assert (await send(client, provider, first)).json() == {"status": "credited"}
    paid = order("order.paid", "ord_left_open", checkout_id=checkout_id, total_amount=600)
    assert (await send(client, provider, paid)).json() == {"status": "credited"}

    refused = await client.post(f"/v1/credits/fiat-checkouts/{checkout_id}/pass")

    assert (refused.status_code, refused.json()["error_code"]) == (409, "CHECKOUT_TOPPED_UP")
    assert await checks_left(client, issued.key) is not None  # still works


@pytest.mark.parametrize("when", ["before_payment", "after_payment"])
@pytest.mark.parametrize("how", ["crypto", "grant"])
async def test_a_pass_that_got_checks_besides_this_purchase_is_not_reissued(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, when: str, how: str
) -> None:
    # Checks bought with crypto (or granted, or refunded) onto the pass while its card checkout was open, or after
    # it was paid: the pass holds more than this purchase, so it is in use and never rotated from another browser.
    issued, _, checkout_id = await paid_checkout(client, provider, pool, pay=when == "after_payment")
    await ledger.credit(pool, issued.account, 5_000_000, purchase=how == "crypto")
    if when == "before_payment":
        paid = order("order.paid", "ord_after_crypto", checkout_id=checkout_id, total_amount=600)
        assert (await send(client, provider, paid)).json() == {"status": "credited"}

    refused = await client.post(f"/v1/credits/fiat-checkouts/{checkout_id}/pass")

    assert (refused.status_code, refused.json()["error_code"]) == (409, "CHECKOUT_TOPPED_UP")
    assert await checks_left(client, issued.key) == 50  # still works, with everything on it


async def test_a_purchase_before_payment_or_unknown_or_too_old_gives_no_pass(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued, started, unpaid = await paid_checkout(client, provider, pool, pay=False)
    waiting = await client.post(f"/v1/credits/fiat-checkouts/{unpaid}/pass")
    assert (waiting.status_code, waiting.json()["error_code"]) == (409, "CHECKOUT_NOT_PAID")
    assert waiting.headers["retry-after"] == "3"
    # A status poll before payment doesn't count as picking the purchase up.
    assert not (
        await client.get(f"/v1/credits/fiat-checkouts/{unpaid}", headers={"X-Unfret-Claim": started["claim"]})
    ).json()["paid"]
    paid = order("order.paid", "ord_late", checkout_id=unpaid, total_amount=600)
    assert (await send(client, provider, paid)).json() == {"status": "credited"}
    assert (await client.post(f"/v1/credits/fiat-checkouts/{unpaid}/pass")).status_code == 200

    unknown = await client.post("/v1/credits/fiat-checkouts/no-such-checkout/pass")
    assert (unknown.status_code, unknown.json()["error_code"]) == (404, "UNKNOWN_CHECKOUT")

    old, _, old_id = await paid_checkout(client, provider, pool)
    await pool.execute(
        "UPDATE fiat_checkouts SET paid_at = NOW() - INTERVAL '25 hours' WHERE checkout_id = $1", old_id
    )
    expired = await client.post(f"/v1/credits/fiat-checkouts/{old_id}/pass")
    assert (expired.status_code, expired.json()["error_code"]) == (410, "CHECKOUT_EXPIRED")
    assert await checks_left(client, old.key) == 25
    assert await checks_left(client, issued.key) is None  # the late one was picked up above


async def test_picking_up_a_purchase_needs_card_payments(client: httpx.AsyncClient) -> None:
    refused = await client.post("/v1/credits/fiat-checkouts/anything/pass")
    assert (refused.status_code, refused.json()["error_code"]) == (503, "SERVICE_UNAVAILABLE")
