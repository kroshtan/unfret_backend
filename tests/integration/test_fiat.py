import base64
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from veridex.config import settings
from veridex.credits import fiat, keys
from veridex.credits.fiat import FakeFiatProvider, FiatEvent
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


async def checkout(client: httpx.AsyncClient, key_id: str, usd_micros: int = 10_000_000) -> httpx.Response:
    return await client.post("/v1/credits/fiat-checkout", json={"key_id": key_id, "amount_usd_micros": usd_micros})


async def test_fiat_is_off_without_a_provider(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    issued = await keys.create(pool)
    assert (await checkout(client, issued.key_id)).status_code == 503
    assert (await client.get("/v1/pricing")).json()["fiat"] == {"available": False, "min_usd_micros": 10_000_000}


async def test_the_minimum_fiat_pack_is_ten_dollars(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    assert (await checkout(client, issued.key_id, 9_990_000)).status_code == 400
    assert (await checkout(client, issued.key_id, 10_000_001)).status_code == 400  # whole cents only
    assert (await checkout(client, issued.key_id, 10_000_000)).status_code == 200


async def test_a_paid_order_credits_the_key_once_without_storing_the_buyer(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    started = (await checkout(client, issued.key_id)).json()
    checkout_id = started["checkout_url"].split("id=")[1].split("&")[0]
    # The order doesn't echo the checkout metadata: the server-side mapping finds the key.
    paid = order("order.paid", "ord_1", checkout_id=checkout_id, total_amount=1000)

    first = await send(client, provider, paid)
    retry = await send(client, provider, paid)

    assert first.json() == {"status": "credited"}
    assert retry.json() == {"status": "duplicate"}
    assert await balance(pool, issued.account) == 11_000_000  # $10 plus the >= $5 bonus, like crypto
    rows = [dict(r) for r in await pool.fetch("SELECT * FROM fiat_payments")]
    rows += [dict(r) for r in await pool.fetch("SELECT * FROM fiat_checkouts")]
    stored = str(rows)
    for detail in ("jan.jansen", "Jan Jansen", "Kerkstraat"):
        assert detail not in stored
    assert rows[0]["account"] == issued.account
    assert rows[0]["amount_cents"] == 1000


async def test_metadata_alone_also_names_the_key(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    paid = order("order.paid", "ord_2", metadata={"account": issued.account}, total_amount=2500)
    assert (await send(client, provider, paid)).json() == {"status": "credited"}
    assert await balance(pool, issued.account) == 30_000_000  # $25 plus 20%


async def test_partial_refunds_debit_pro_rata_and_may_go_negative(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, graph: FakeGraph
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": {"account": issued.account}, "total_amount": 1000}
    await send(client, provider, order("order.paid", "ord_3", **meta))  # $11 of credits
    await pool.execute("UPDATE wallets SET balance_usd_micros = 1_000_000 WHERE pubkey = $1", issued.account)  # spent

    quarter = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=250, **meta))
    again = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=250, **meta))
    half = await send(client, provider, order("order.refunded", "ord_3", refunded_amount=500, **meta))

    assert [r.json()["status"] for r in (quarter, again, half)] == ["debited", "unchanged", "debited"]
    assert await balance(pool, issued.account) == 1_000_000 - 5_500_000  # half of $11, the balance below zero
    analysis = await client.post("/v1/analyze", json=PAGE, headers={"Authorization": f"Bearer {issued.key}"})
    assert analysis.status_code == 402  # a negative balance blocks paid use
    assert (await client.get("/v1/credits", headers={"Authorization": f"Bearer {issued.key}"})).json()[
        "checks_remaining"
    ] == 0


async def test_a_dispute_takes_back_the_whole_order(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    meta = {"metadata": {"account": issued.account}, "total_amount": 1000}
    await send(client, provider, order("order.paid", "ord_4", **meta))
    await send(client, provider, order("order.refunded", "ord_4", refunded_amount=500, **meta))

    disputed = await send(client, provider, order("order.disputed", "ord_4", **meta))
    later_refund = await send(client, provider, order("order.refunded", "ord_4", refunded_amount=1000, **meta))

    assert disputed.json() == {"status": "debited"}
    assert later_refund.json() == {"status": "unchanged"}  # never taken back twice
    assert await balance(pool, issued.account) == 0
    row = await pool.fetchrow("SELECT * FROM fiat_payments")
    assert (row["disputed"], row["debited_usd_micros"], row["refunded_cents"]) == (True, 11_000_000, 1000)


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
    await fiat.apply_event(pool, event, settings.credit_bonus_tiers)

    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=179))
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 1
    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=181))
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0
