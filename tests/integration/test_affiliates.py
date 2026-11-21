"""Affiliates and bonus codes: attribution, bonus, commission (card, crypto, refunds), stats and payouts."""

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from tests.integration.conftest import bearer
from tests.integration.test_credits import HOT, SOL_BOOK, ChainRpc, transfer, with_memos
from tests.integration.test_keys import solve
from unfret.config import settings
from unfret.credits import affiliates, keys, vat
from unfret.credits.fiat import FakeFiatProvider
from unfret.credits.scanner import CreditScanner
from unfret.main import app
from unfret.routes import affiliates as affiliate_routes
from unfret.visitors import DailyLimit

ADMIN = {"Authorization": "Bearer operator-token"}
SECRET = "whsec_" + base64.b64encode(b"affiliate-webhook-secret-32bytes").decode()
CHECK = settings.analysis_price_usd_micros
PACK_25 = 25 * CHECK  # $5 of checks
PRICE_25 = 5_000_000  # the 25-pack, VAT included, in lamports at $1,000/SOL


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    monkeypatch.setattr(affiliate_routes, "LOOKUPS", DailyLimit(settings.code_checks_per_ip_per_day, 1_000_000))
    monkeypatch.setattr(settings, "key_pow_bits", 4)  # a handful of hashes


@pytest.fixture
async def provider(client: httpx.AsyncClient) -> AsyncIterator[FakeFiatProvider]:
    app.fiat = FakeFiatProvider(SECRET)
    yield app.fiat
    app.fiat = None


def auth(issued: keys.IssuedKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued.key}"}


async def affiliate(client: httpx.AsyncClient, code: str = "ANNA", name: str = "Anna's channel") -> dict[str, Any]:
    response = await client.post(
        "/v1/admin/affiliates", json={"code": code, "name": name, "payout_note": "IBAN NL00 TEST"}, headers=ADMIN
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def bonus_code(client: httpx.AsyncClient, code: str, **fields: Any) -> httpx.Response:
    return await client.post("/v1/admin/bonus-codes", json={"code": code, **fields}, headers=ADMIN)


async def new_pass(client: httpx.AsyncClient, ref: str | None = None) -> keys.IssuedKey:
    """A pass made the way pages make one (proof of work solved for the test's tiny difficulty)."""
    challenge = (await client.get("/v1/keys/challenge")).json()
    solution = solve(challenge["challenge"], challenge["bits"])
    body = {"challenge": challenge["challenge"], "solution": solution} | ({"ref": ref} if ref else {})
    response = await client.post("/v1/keys", json=body)
    assert response.status_code == 201, response.text
    made = response.json()
    return keys.IssuedKey(made["key_id"], made["key"], made["recovery_code"])


async def attributed_to(pool: asyncpg.Pool, account: str) -> str | None:
    value = await pool.fetchval(
        "SELECT c.code FROM affiliate_passes p JOIN bonus_codes c ON c.affiliate_id = p.affiliate_id "
        "AND c.bonus_pct IS NULL WHERE p.account = $1",
        account,
    )
    return None if value is None else str(value)


async def balance(pool: asyncpg.Pool, account: str) -> int:
    return int(await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", account) or 0)


async def earnings(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        "SELECT source, currency, net_minor, commission_minor, reversed_minor, fee_estimated FROM affiliate_earnings "
        "ORDER BY id"
    )
    return [dict(r) for r in rows]


async def card_purchase(
    client: httpx.AsyncClient,
    provider: FakeFiatProvider,
    issued: keys.IssuedKey,
    *,
    code: str | None = None,
    order_id: str = "order-1",
    paid_cents: int = 600,
    fee: int | None = 69,
) -> httpx.Response:
    """Start a card checkout for the 25-pack and have the provider confirm it paid."""
    body = {"key_id": issued.key_id, "checks": 25, "currency": "usd"} | ({"code": code} if code else {})
    started = await client.post("/v1/credits/fiat-checkout", json=body)
    assert started.status_code == 200, started.text
    data: dict[str, Any] = {
        "id": order_id,
        "checkout_id": started.json()["checkout_id"],
        "metadata": {"account": keys.account_of(issued.key_id), "site": settings.public_base_url},
        "total_amount": paid_cents,
        "currency": "usd",
    }
    if fee is not None:
        data["platform_fee_amount"] = fee
    webhook, headers = provider.webhook({"type": "order.paid", "data": data})
    return await client.post("/v1/webhooks/fiat", content=webhook, headers=headers)


async def refund(client: httpx.AsyncClient, provider: FakeFiatProvider, kind: str, **data: Any) -> None:
    webhook, headers = provider.webhook({"type": kind, "data": {"id": "order-1", **data}})
    assert (await client.post("/v1/webhooks/fiat", content=webhook, headers=headers)).status_code == 200


# ── Attribution ──────────────────────────────────────────────────────────────────────────────────────────────


async def test_a_new_pass_made_with_a_ref_counts_for_the_affiliate(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    made = await affiliate(client)
    assert made["links"]["ref"] == f"{settings.public_base_url}/?ref=ANNA"

    issued = await new_pass(client, ref=" anna ")

    assert await attributed_to(pool, issued.account) == "ANNA"
    row = await pool.fetchrow("SELECT * FROM affiliate_passes")
    assert set(row.keys()) == {"account", "affiliate_id", "attributed_at", "first_check_on", "bonus_used"}  # no IP


async def test_an_unknown_or_switched_off_ref_attributes_nothing(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await affiliate(client)
    assert (await client.post("/v1/admin/affiliates/ANNA", json={"active": False}, headers=ADMIN)).status_code == 200

    for ref in ("ANNA", "NOBODY", "x"):
        issued = await new_pass(client, ref=ref)
        assert await attributed_to(pool, issued.account) is None


async def test_a_page_attributes_its_pass_once_and_never_an_old_customer(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await affiliate(client)
    await affiliate(client, "BORIS", "Boris")
    fresh = await keys.create(pool)

    async def attribute(issued: keys.IssuedKey | None, code: str, headers: dict[str, str] | None = None) -> str:
        sent = headers if headers is not None else auth(issued)  # type: ignore[arg-type]
        response = await client.post("/v1/affiliates/attribute", json={"code": code}, headers=sent)
        assert response.status_code == 200, response.text
        return str(response.json()["status"])

    assert await attribute(fresh, "anna") == "attributed"
    assert await attribute(fresh, "BORIS") == "already"  # the first affiliate stays
    assert await attributed_to(pool, fresh.account) == "ANNA"

    customer = await keys.create(pool)  # bought before the link: not the affiliate's customer
    await pool.execute(
        "INSERT INTO wallets (pubkey, balance_usd_micros, purchased) VALUES ($1, 0, TRUE)", customer.account
    )
    assert await attribute(customer, "ANNA") == "not_eligible"
    assert await attribute(None, "ANNA", bearer(str(Keypair().pubkey()))) == "not_eligible"  # a wallet
    assert await attribute(await keys.create(pool), "NOPE") == "invalid"
    assert (await client.post("/v1/affiliates/attribute", json={"code": "ANNA"})).status_code == 401
    assert await pool.fetchval("SELECT COUNT(*) FROM affiliate_passes") == 1


async def test_a_gift_code_or_free_checks_dont_make_a_pass_a_customer(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await affiliate(client)
    issued = await keys.create(pool, free_checks=5)
    await pool.execute(
        "INSERT INTO wallets (pubkey, balance_usd_micros, invite_redeemed) VALUES ($1, 600000, TRUE)", issued.account
    )
    assert await affiliates.attribute(pool, issued.account, "ANNA") == "attributed"


# ── Card purchases: bonus, commission, refunds ──────────────────────────────────────────────────────────────


async def test_the_first_card_pack_on_an_attributed_pass_gets_20_percent_more_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    issued = await new_pass(client, ref="ANNA")

    assert (await card_purchase(client, provider, issued)).json() == {"status": "credited"}

    assert await balance(pool, issued.account) == PACK_25 + 5 * CHECK  # 25 checks + 5
    payment = await pool.fetchrow("SELECT credited_usd_micros, bonus_usd_micros FROM fiat_payments")
    assert (payment["credited_usd_micros"], payment["bonus_usd_micros"]) == (PACK_25 + 5 * CHECK, 5 * CHECK)
    # Polar's fee (69 cents) comes off before the 25%: (600 - 69) * 25% = 132.75 → 132 cents.
    assert await earnings(pool) == [
        {
            "source": "card",
            "currency": "usd",
            "net_minor": 531,
            "commission_minor": 132,
            "reversed_minor": 0,
            "fee_estimated": False,
        }
    ]

    await card_purchase(client, provider, issued, order_id="order-2", fee=None)  # second pack: no bonus

    assert await balance(pool, issued.account) == 2 * PACK_25 + 5 * CHECK
    # No fee reported: the configured worst case (6.5% + 50 cents = 89 cents) is taken off instead.
    second = (await earnings(pool))[1]
    assert (second["net_minor"], second["commission_minor"], second["fee_estimated"]) == (511, 127, True)


async def test_a_webhook_sent_twice_counts_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    issued = await new_pass(client, ref="ANNA")
    data = {
        "id": "order-1",
        "metadata": {"account": issued.account, "site": settings.public_base_url},
        "total_amount": 600,
        "platform_fee_amount": 69,
    }
    for _ in range(2):
        webhook, headers = provider.webhook({"type": "order.paid", "data": data})
        await client.post("/v1/webhooks/fiat", content=webhook, headers=headers)

    assert len(await earnings(pool)) == 1
    assert await balance(pool, issued.account) == PACK_25 + 5 * CHECK


async def test_a_refund_takes_back_its_share_of_the_bonus_and_the_commission(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    issued = await new_pass(client, ref="ANNA")
    await card_purchase(client, provider, issued)

    await refund(client, provider, "order.refunded", total_amount=600, refunded_amount=300)  # half

    assert await balance(pool, issued.account) == (PACK_25 + 5 * CHECK) // 2
    assert (await earnings(pool))[0]["reversed_minor"] == 66  # half of 132
    await refund(client, provider, "order.refunded", total_amount=600, refunded_amount=300)  # the same, again
    assert (await earnings(pool))[0]["reversed_minor"] == 66

    await refund(client, provider, "order.disputed")  # a chargeback takes all of it

    assert await balance(pool, issued.account) == 0
    assert (await earnings(pool))[0]["reversed_minor"] == 132
    assert await pool.fetchval("SELECT SUM(amount_minor) FROM affiliate_reversals") == 132


async def test_no_commission_after_twelve_months_or_for_a_switched_off_affiliate(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    old, current = await new_pass(client, ref="ANNA"), await new_pass(client, ref="ANNA")
    await pool.execute(
        "UPDATE affiliate_passes SET attributed_at = NOW() - INTERVAL '12 months 1 day' WHERE account = $1",
        old.account,
    )
    await card_purchase(client, provider, old)
    assert await earnings(pool) == []

    await client.post("/v1/admin/affiliates/ANNA", json={"active": False}, headers=ADMIN)
    await card_purchase(client, provider, current, order_id="order-2")
    assert await earnings(pool) == []
    assert await balance(pool, current.account) == PACK_25 + 5 * CHECK  # the bonus promised still applies


async def test_an_affiliate_code_at_checkout_attributes_a_pass_that_never_bought(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    newcomer, customer = await keys.create(pool), await keys.create(pool)
    await card_purchase(client, provider, customer, order_id="before")  # a customer already

    await card_purchase(client, provider, newcomer, code="anna", order_id="order-1")
    await card_purchase(client, provider, customer, code="ANNA", order_id="order-2")

    assert await attributed_to(pool, newcomer.account) == "ANNA"
    assert await balance(pool, newcomer.account) == PACK_25 + 5 * CHECK
    assert await attributed_to(pool, customer.account) is None  # never an old customer
    assert await balance(pool, customer.account) == 2 * PACK_25
    assert len(await earnings(pool)) == 1
    assert await pool.fetchval("SELECT uses FROM bonus_codes WHERE code = 'ANNA'") == 2


async def test_starting_a_checkout_attributes_nothing_until_it_is_paid(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    someone = await keys.create(pool)  # anyone can name a pass's id in a checkout; only paying attributes it
    body = {"key_id": someone.key_id, "checks": 25, "code": "ANNA"}
    assert (await client.post("/v1/credits/fiat-checkout", json=body)).status_code == 200
    assert await attributed_to(pool, someone.account) is None


# ── Discount codes ───────────────────────────────────────────────────────────────────────────────────────────


async def test_codes_are_checked_before_paying_with_one_calm_answer(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await affiliate(client)
    assert (await bonus_code(client, "MORE20", bonus_pct=20)).status_code == 201
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    assert (await bonus_code(client, "OLD", bonus_pct=10, expires_on=yesterday)).status_code == 201
    assert (await bonus_code(client, "ONCE", bonus_pct=10, max_uses=1)).status_code == 201
    await pool.execute("UPDATE bonus_codes SET uses = 1 WHERE code = 'ONCE'")

    async def check(code: str) -> httpx.Response:
        return await client.post("/v1/credits/codes/check", json={"code": code})

    assert (await check("more20")).json() == {
        "code": "MORE20",
        "label": "+20% checks",
        "bonus_pct": 20,
        "first_pack_only": False,
    }
    assert (await check("ANNA")).json()["label"] == "+20% checks on your first pack"
    refusals = [await check(code) for code in ("OLD", "ONCE", "NOSUCHCODE", "!!")]
    assert {(r.status_code, r.json()["error_code"], r.json()["detail"]) for r in refusals} == {
        (404, "INVALID_CODE", "This code isn't valid, or has ended.")
    }


async def test_code_lookups_are_limited_per_network(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(affiliate_routes, "LOOKUPS", DailyLimit(3, 1_000_000))
    replies = [await client.post("/v1/credits/codes/check", json={"code": f"GUESS{n}"}) for n in range(4)]
    assert [r.status_code for r in replies] == [404, 404, 404, 429]


async def test_a_bonus_code_adds_its_checks_and_runs_out(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await bonus_code(client, "MORE40", bonus_pct=40, max_uses=1)
    buyer = await keys.create(pool)

    await card_purchase(client, provider, buyer, code="MORE40")

    assert await balance(pool, buyer.account) == PACK_25 + 10 * CHECK
    assert await pool.fetchval("SELECT uses FROM bonus_codes") == 1
    again = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": buyer.key_id, "checks": 25, "code": "MORE40"}
    )
    assert (again.status_code, again.json()["error_code"]) == (404, "INVALID_CODE")
    assert await earnings(pool) == []  # no affiliate on this code


async def test_an_expired_code_is_refused_at_checkout(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await bonus_code(client, "SUMMER", bonus_pct=10, expires_on=(date.today() - timedelta(days=1)).isoformat())
    buyer = await keys.create(pool)
    response = await client.post(
        "/v1/credits/fiat-checkout", json={"key_id": buyer.key_id, "checks": 25, "code": "SUMMER"}
    )
    assert (response.status_code, response.json()["detail"]) == (404, "This code isn't valid, or has ended.")


async def test_operator_codes_are_checked(client: httpx.AsyncClient) -> None:
    await affiliate(client)
    assert (await bonus_code(client, "NONE")).status_code == 422  # a bonus is required
    assert (await bonus_code(client, "HUGE", bonus_pct=500)).status_code == 422
    assert (await bonus_code(client, "ANNA", bonus_pct=10)).status_code == 409  # taken by the affiliate
    assert (await bonus_code(client, "X1", bonus_pct=10)).status_code == 422
    assert (await bonus_code(client, "FORBORIS", bonus_pct=10, affiliate="BORIS")).status_code == 409
    assert (
        await client.post("/v1/admin/affiliates", json={"code": "ANNA", "name": "Again"}, headers=ADMIN)
    ).status_code == 409
    assert (await client.get("/v1/admin/affiliates")).status_code == 401  # admin only


# ── Crypto ───────────────────────────────────────────────────────────────────────────────────────────────────


async def crypto_quote(pool: asyncpg.Pool, account: str, code: str | None = None) -> vat.Quote:
    quote = vat.price_quote(settings, 25, PACK_25, "NL", edge=None, beneficiary=account, code=code)
    await vat.save_quote(pool, quote)
    return quote


async def scan(pool: asyncpg.Pool, quote: vat.Quote, lamports: int, signature: str = "buy") -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add(signature, with_memos(transfer("anna", str(HOT.pubkey()), lamports), quote.memo))
    assert await scanner.scan_once() == 1


async def test_a_crypto_pack_gets_the_bonus_and_earns_on_its_net(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await affiliate(client)
    issued = await new_pass(client, ref="ANNA")

    await scan(pool, await crypto_quote(pool, issued.account), PRICE_25)

    assert await balance(pool, issued.account) == PACK_25 + 5 * CHECK
    # What arrived ($5.00, VAT included) less its VAT: $4.13, no payment fee: 25% = 103 cents.
    assert await earnings(pool) == [
        {
            "source": "crypto",
            "currency": "usd",
            "net_minor": 413,
            "commission_minor": 103,
            "reversed_minor": 0,
            "fee_estimated": False,
        }
    ]
    (earning_id,) = [r["id"] for r in await pool.fetch("SELECT id FROM affiliate_earnings")]
    reversed_ = await client.post(f"/v1/admin/affiliates/earnings/{earning_id}/reverse", headers=ADMIN)
    assert reversed_.json() == {"earning_id": earning_id, "reversed_minor": 103}  # a refund by hand


# ── The stats page, the report, payouts ──────────────────────────────────────────────────────────────────────


async def test_the_stats_link_shows_numbers_and_nothing_about_buyers(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    made = await affiliate(client)
    token = made["links"]["stats"].split("#", 1)[1]
    assert made["links"]["stats"].startswith(f"{settings.public_base_url}/affiliates/stats#ufa_")
    issued = await new_pass(client, ref="ANNA")
    await new_pass(client, ref="ANNA")
    await affiliates.first_check(pool, issued.account)
    await card_purchase(client, provider, issued)
    assert await pool.fetchval("SELECT stats_hash FROM affiliates") != token.encode()  # only the hash is kept

    response = await client.get("/v1/affiliates/stats", headers={"X-Unfret-Stats": token})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    month = datetime.now(UTC).strftime("%Y-%m")
    assert body == {
        "name": "Anna's channel",
        "active": True,
        "codes": ["ANNA"],
        "months": [
            {
                "month": month,
                "passes": 2,
                "first_checks": 1,
                "purchases": 1,
                "money": {"usd": {"net": 531, "commission": 132, "reversed": 0, "paid": 0}},
            }
        ],
        "balance": {"usd": {"payable": 0, "held": 132, "due": 0, "carried_over": 0}},  # held 30 days
        "rules": {"hold_days": 30, "min_payout_cents": 2500},
    }
    assert issued.key_id not in response.text

    rotated = await client.post("/v1/admin/affiliates/anna", json={"stats": "rotate"}, headers=ADMIN)
    new_token = rotated.json()["links"]["stats"].split("#", 1)[1]
    assert (await client.get("/v1/affiliates/stats", headers={"X-Unfret-Stats": token})).status_code == 404
    assert (await client.get("/v1/affiliates/stats", headers={"X-Unfret-Stats": new_token})).status_code == 200
    await client.post("/v1/admin/affiliates/anna", json={"stats": "revoke"}, headers=ADMIN)
    gone = await client.get("/v1/affiliates/stats", headers={"X-Unfret-Stats": new_token})
    assert (gone.status_code, gone.json()["error_code"]) == (404, "UNKNOWN_STATS_LINK")
    assert (await client.get("/v1/affiliates/stats")).status_code == 404


async def test_purge_keeps_bookkeeping_and_forgets_old_attributions(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await affiliate(client)
    issued = await new_pass(client, ref="ANNA")
    later = datetime.now(UTC) + timedelta(days=settings.affiliate_pass_retention_days + 1)
    await affiliates.purge(pool, settings, now=later)
    assert await attributed_to(pool, issued.account) is None
    assert await pool.fetchval("SELECT COUNT(*) FROM affiliates") == 1


async def test_checkouts_racing_for_a_codes_last_use_get_it_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await bonus_code(client, "MORE40", bonus_pct=40, max_uses=1)
    first, second = await keys.create(pool), await keys.create(pool)
    started = [
        await client.post("/v1/credits/fiat-checkout", json={"key_id": k.key_id, "checks": 25, "code": "MORE40"})
        for k in (first, second)
    ]
    assert [s.status_code for s in started] == [200, 200]  # both started while one use was left

    for issued, response, order_id in zip((first, second), started, ("o1", "o2"), strict=True):
        data = {
            "id": order_id,
            "checkout_id": response.json()["checkout_id"],
            "metadata": {"account": issued.account, "site": settings.public_base_url},
            "total_amount": 600,
        }
        webhook, headers = provider.webhook({"type": "order.paid", "data": data})
        await client.post("/v1/webhooks/fiat", content=webhook, headers=headers)

    assert await balance(pool, first.account) == PACK_25 + 10 * CHECK
    assert await balance(pool, second.account) == PACK_25  # paid, credited, without the used-up bonus
    assert await pool.fetchval("SELECT uses FROM bonus_codes") == 1


async def test_an_affiliate_made_by_mistake_can_be_removed_until_money_is_booked(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client, "TYPO")
    await new_pass(client, ref="TYPO")
    assert (await client.delete("/v1/admin/affiliates/typo", headers=ADMIN)).json() == {
        "code": "TYPO",
        "deleted": True,
    }
    assert await pool.fetchval("SELECT COUNT(*) FROM affiliate_passes") == 0
    assert (await client.delete("/v1/admin/affiliates/TYPO", headers=ADMIN)).status_code == 404

    await affiliate(client)
    await card_purchase(client, provider, await new_pass(client, ref="ANNA"))
    assert (await client.delete("/v1/admin/affiliates/ANNA", headers=ADMIN)).status_code == 409


async def test_a_crypto_bonus_code_adds_its_checks(pool: asyncpg.Pool) -> None:
    await affiliates.create_code(pool, "MORE20", bonus_pct=20)
    issued = await keys.create(pool)

    await scan(pool, await crypto_quote(pool, issued.account, "MORE20"), PRICE_25)

    assert await balance(pool, issued.account) == PACK_25 + 5 * CHECK  # the same price, 20% more checks
    assert await pool.fetchval("SELECT uses FROM bonus_codes") == 1


# ── Payouts: a 30-day hold, a minimum, refunds after a payout ───────────────────────────────────────────────


async def age(pool: asyncpg.Pool, days: int) -> None:
    """Make every earning ``days`` days old."""
    await pool.execute("UPDATE affiliate_earnings SET earned_on = CURRENT_DATE - $1::int", days)


async def owed(client: httpx.AsyncClient) -> dict[str, Any]:
    (only,) = (await client.get("/v1/admin/affiliates", headers=ADMIN)).json()["affiliates"]
    balance_: dict[str, Any] = only["balance"]
    return balance_


async def pay(client: httpx.AsyncClient, code: str = "ANNA") -> dict[str, int]:
    response = await client.post(f"/v1/admin/affiliates/{code}/paid", json={"note": "SEPA 1"}, headers=ADMIN)
    assert response.status_code == 200, response.text
    paid: dict[str, int] = response.json()["paid"]
    return paid


async def big_spender(
    client: httpx.AsyncClient, provider: FakeFiatProvider, orders: int, prefix: str = "order"
) -> keys.IssuedKey:
    """A pass that came through ANNA and bought ``orders`` 25-packs (132 cents of commission each)."""
    issued = await new_pass(client, ref="ANNA")
    for n in range(orders):
        await card_purchase(client, provider, issued, order_id=f"{prefix}-{n + 1}")
    return issued


async def test_commission_is_held_30_days_and_paid_from_25(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    await big_spender(client, provider, 20)  # 20 x 132 = $26.40

    assert await owed(client) == {"usd": {"payable": 0, "held": 2640, "due": 0, "carried_over": 0}}
    assert await pay(client) == {}  # nothing is payable inside the refund window

    await age(pool, 31)
    assert await owed(client) == {"usd": {"payable": 2640, "held": 0, "due": 2640, "carried_over": 0}}
    assert await pay(client) == {"usd": 2640}
    assert await owed(client) == {"usd": {"payable": 0, "held": 0, "due": 0, "carried_over": 0}}
    assert await pay(client) == {}  # paid once


async def test_below_the_minimum_carries_over_unless_the_affiliate_is_switched_off(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    await big_spender(client, provider, 2)  # $2.64
    await age(pool, 31)

    assert await owed(client) == {"usd": {"payable": 264, "held": 0, "due": 0, "carried_over": 264}}
    assert await pay(client) == {}

    await client.post("/v1/admin/affiliates/ANNA", json={"active": False}, headers=ADMIN)  # the cooperation ends

    assert (await owed(client))["usd"]["due"] == 264  # all of it, whatever the minimum
    assert await pay(client) == {"usd": 264}


async def test_a_refund_after_a_payout_is_deducted_from_the_next(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    await big_spender(client, provider, 20)
    await age(pool, 31)
    assert await pay(client) == {"usd": 2640}

    await refund(client, provider, "order.disputed")  # order-1 charged back after the payout: -132

    assert await owed(client) == {"usd": {"payable": -132, "held": 0, "due": 0, "carried_over": -132}}
    report = (await client.get("/v1/admin/affiliates/report", headers=ADMIN)).json()
    totals = {k: sum(r["money"]["usd"][k] for r in report["months"]) for k in ("commission", "reversed", "paid")}
    assert totals == {"commission": 2640, "reversed": 132, "paid": 2640}  # earned, reversed, paid, month by month

    await big_spender(client, provider, 21, prefix="later")  # more sales: 21 x 132 = 2772
    await pool.execute("UPDATE affiliate_earnings SET earned_on = CURRENT_DATE - 31")

    assert (await owed(client))["usd"]["due"] == 2772 - 132  # the refund is made up for, never lost
    assert await pay(client) == {"usd": 2640}


async def test_a_refund_inside_the_hold_lowers_what_will_be_paid(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    await big_spender(client, provider, 20)
    await refund(client, provider, "order.refunded", total_amount=600, refunded_amount=600)  # order-1 in full

    assert await owed(client) == {"usd": {"payable": 0, "held": 2640 - 132, "due": 0, "carried_over": 0}}


async def test_two_payouts_racing_pay_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    await affiliate(client)
    await big_spender(client, provider, 20)
    await age(pool, 31)
    await asyncio.gather(*(affiliates.mark_paid(pool, settings, "ANNA") for _ in range(3)))
    assert await pool.fetchval("SELECT SUM(amount_minor) FROM affiliate_payouts") == 2640


async def test_the_stats_page_leads_with_the_affiliates_own_code(client: httpx.AsyncClient) -> None:
    made = await affiliate(client, "ZARA")
    await bonus_code(client, "AAAEXTRA", bonus_pct=10, affiliate="ZARA")  # sorts before ZARA
    token = made["links"]["stats"].split("#", 1)[1]
    body = (await client.get("/v1/affiliates/stats", headers={"X-Unfret-Stats": token})).json()
    assert body["codes"] == ["ZARA", "AAAEXTRA"]  # the page's link is /?ref=<the first>
