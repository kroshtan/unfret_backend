import base64
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from tests.unit import test_polar  # Polar's sample payloads
from veridex.config import settings
from veridex.credits import fiat, keys, polar
from veridex.credits.fiat import FakeFiatProvider, FiatEvent
from veridex.credits.polar import PolarProvider
from veridex.main import app

SECRET = "whsec_" + base64.b64encode(b"fake-webhook-secret-32-bytes!!!!").decode()
BUYER = {"email": "jan.jansen@example.nl", "name": "Jan Jansen", "billing_address": {"line1": "Kerkstraat 1"}}
PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater"}


@pytest.fixture
async def provider(client: httpx.AsyncClient) -> AsyncIterator[FakeFiatProvider]:
    app.fiat = FakeFiatProvider(SECRET)
    yield app.fiat
    app.fiat = None


async def balance(pool: asyncpg.Pool, account: str) -> int | None:
    value = await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", account)
    return None if value is None else int(value)


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
    assert (await checkout(client, issued.key_id)).status_code == 503
    assert (await client.get("/v1/pricing")).json()["fiat"] == {"available": False, "min_usd_micros": 6_000_000}


async def test_card_payments_buy_packs_only(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    assert (await checkout(client, issued.key_id, 10)).status_code == 400
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
    paid = order("order.paid", "ord_2", metadata={"account": issued.account}, total_amount=2200)
    assert (await send(client, provider, paid)).json() == {"status": "credited"}
    assert await balance(pool, issued.account) == 20_000_000  # the 100-pack ($22 by card)


async def test_partial_refunds_debit_pro_rata_and_may_go_negative(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, graph: FakeGraph
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": {"account": issued.account}, "total_amount": 2200}
    await send(client, provider, order("order.paid", "ord_3", **meta))  # the 100-pack: $20 of credits
    await pool.execute("UPDATE wallets SET balance_usd_micros = 1_000_000 WHERE pubkey = $1", issued.account)  # spent

    quarter = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=550, **meta))
    again = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=550, **meta))
    half = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=1100, **meta))

    assert [r.json()["status"] for r in (quarter, again, half)] == ["debited", "unchanged", "debited"]
    assert await balance(pool, issued.account) == 1_000_000 - 10_000_000  # half of $20, the balance below zero
    analysis = await client.post("/v1/analyze", json=PAGE, headers={"Authorization": f"Bearer {issued.key}"})
    assert analysis.status_code == 402  # a negative balance blocks paid use
    assert (await client.get("/v1/credits", headers={"Authorization": f"Bearer {issued.key}"})).json()[
        "checks_remaining"
    ] == 0


async def test_a_dispute_takes_back_the_whole_order(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": {"account": issued.account}, "total_amount": 600}
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
    paid = order("order.paid", "ord_5", metadata={"account": issued.account}, total_amount=1000)
    body, headers = provider.webhook(paid)

    forged = await client.post("/v1/webhooks/fiat", content=body, headers={**headers, "webhook-signature": "v1,AAAA"})
    tampered = await client.post("/v1/webhooks/fiat", content=body.replace(b"1000", b"9000"), headers=headers)
    stale = await send(client, provider, paid, now=time.time() - 600)  # a replay outside the window
    wallet = await send(
        client, provider, order("order.paid", "ord_6", metadata={"account": "SomeWallet"}, total_amount=1000)
    )
    refund = await send(client, provider, order("order.refunded", "ord_unknown", refunded_amount=100))

    assert [r.status_code for r in (forged, tampered, stale)] == [400, 400, 400]
    assert wallet.json() == {"status": "unknown_account"}  # fiat tops up API keys only
    assert refund.json() == {"status": "unknown_order"}
    assert await balance(pool, issued.account) is None


async def test_order_rows_are_deleted_after_retention(pool: asyncpg.Pool) -> None:
    issued = await keys.create(pool)
    event = FiatEvent(kind="paid", provider="fake", order_id="ord_7", amount_cents=1000, account=issued.account)
    await fiat.apply_event(pool, event, settings)

    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=179))
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 1
    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=181))
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0


# ── Polar, end to end through the routes (its API mocked) ─────────────────────────────────────────────────────


@pytest.fixture
async def polar_provider(client: httpx.AsyncClient) -> AsyncIterator[PolarProvider]:
    def api(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        price = {test_polar.PRODUCTS[25]: 600, test_polar.PRODUCTS[100]: 2200}[sent["products"][0]]
        body = {"id": test_polar.CHECKOUT_ID, "url": "https://buy.polar.sh/c", "currency": "usd", "amount": price}
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
    assert started.json() == {"checkout_url": "https://buy.polar.sh/c", "provider": "polar"}
    # Polar's order without our metadata: the checkout mapping names the key.
    paid = test_polar.order(metadata={})

    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "credited"}
    assert (await polar_send(client, "order.paid", paid)).json() == {"status": "duplicate"}
    assert await balance(pool, issued.account) == 5_000_000
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
    assert response.status_code == 503
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_checkouts") == 0


async def test_ready_says_fiat_is_off_without_polar(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/health/ready")).json()["fiat"] == "off"
