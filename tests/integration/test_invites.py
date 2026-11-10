"""Invites: issued with every credited pack (crypto and card), shown to the buyer once, redeemed once."""

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
from solders.keypair import Keypair

from tests.integration.conftest import DROP_ALL, bearer
from tests.integration.test_credits import HOT, SOL_BOOK, ChainRpc, transfer, with_memos
from tests.integration.test_fiat import SECRET, send
from tests.unit.x402_helpers import StaticOracle
from unfret.config import settings
from unfret.credits import CreditsDoor, fiat, invites, keys, vat
from unfret.credits.fiat import FakeFiatProvider
from unfret.credits.scanner import CreditScanner
from unfret.main import app
from unfret.migrations import MIGRATIONS, migrate
from unfret.prices import PriceBook

INVITE = 3 * settings.analysis_price_usd_micros  # 3 checks: $0.60
GROSS_NL = {25: 6_050_000, 50: 12_100_000, 100: 24_200_000}  # the packs with Dutch VAT, in lamports at $1,000/SOL


async def quote_with_claim(pool: asyncpg.Pool, checks: int, *, claim: bool = True) -> tuple[vat.Quote, str]:
    value = checks * settings.analysis_price_usd_micros
    quote = vat.price_quote(settings, checks, value, "NL", edge=None, beneficiary=None)
    token = invites.new_claim()
    await vat.save_quote(pool, quote, claim_hash=invites.digest(token) if claim else None)
    return quote, token


async def scanner(pool: asyncpg.Pool) -> tuple[ChainRpc, CreditScanner]:
    rpc = ChainRpc()
    scan = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scan.initialise()
    return rpc, scan


async def collect(client: httpx.AsyncClient, quote_id: str, claim: str | None) -> dict[str, object]:
    headers = {"X-Unfret-Claim": claim} if claim else {}
    response = await client.get(f"/v1/credits/quotes/{quote_id}", headers=headers)
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


async def an_invite(pool: asyncpg.Pool) -> str:
    """One issued invite code, as a buyer would hand it on."""
    async with pool.acquire() as conn, conn.transaction():
        quote, _ = await quote_with_claim(pool, 25)
        assert await invites.issue_for_quote(conn, settings, quote.id, quote.net_usd_micros) == 1
        codes = await conn.fetchval("SELECT invite_codes FROM vat_quotes WHERE id = $1", quote.id)
    return str(codes[0])


async def redeem(client: httpx.AsyncClient, code: str, headers: dict[str, str]) -> httpx.Response:
    return await client.post("/v1/invites/redeem", json={"code": code}, headers=headers)


# ── Issuing with crypto purchases ─────────────────────────────────────────────


@pytest.mark.parametrize(("checks", "expected"), [(25, 1), (50, 2), (100, 4)])
async def test_a_crypto_pack_comes_with_its_invites_shown_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, checks: int, expected: int
) -> None:
    rpc, scan = await scanner(pool)
    quote, claim = await quote_with_claim(pool, checks)
    rpc.add("buy", with_memos(transfer("anna", str(HOT.pubkey()), GROSS_NL[checks]), quote.memo))
    assert await collect(client, quote.id, claim) == {"paid": False, "checks": checks, "invites": []}

    assert await scan.scan_once() == 1

    assert (await collect(client, quote.id, None))["invites"] == []  # the quote id alone (on chain) shows no codes
    assert (await collect(client, quote.id, invites.new_claim()))["invites"] == []  # nor another token
    shown = await collect(client, quote.id, claim)
    codes = shown["invites"]
    assert isinstance(codes, list) and len(codes) == expected and len(set(codes)) == expected
    assert all(invites.normalise(code) == code for code in codes)
    assert await collect(client, quote.id, claim) == {"paid": True, "checks": checks, "invites": []}  # once
    # Only the hashes are left; the plaintext went with the first read.
    assert await pool.fetchval("SELECT invite_codes FROM vat_quotes WHERE id = $1", quote.id) is None
    stored = {bytes(r["code_hash"]) for r in await pool.fetch("SELECT code_hash FROM invites")}
    assert stored == {hashlib.sha256(code.encode()).digest() for code in codes}
    # A gift is no sale: the one crypto_sales row is the pack's, for its price.
    assert await pool.fetchval("SELECT COUNT(*) FROM crypto_sales") == 1
    assert await pool.fetchval("SELECT net_usd_micros FROM crypto_sales") == quote.net_usd_micros


async def test_no_invites_without_a_claim_for_a_short_payment_or_a_second_one(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    rpc, scan = await scanner(pool)
    hot = str(HOT.pubkey())
    unclaimable, _ = await quote_with_claim(pool, 25, claim=False)  # an older client's quote: nobody could collect
    short, short_claim = await quote_with_claim(pool, 25)
    twice, twice_claim = await quote_with_claim(pool, 25)
    rpc.add("old", with_memos(transfer("anna", hot, GROSS_NL[25]), unclaimable.memo))
    rpc.add("short", with_memos(transfer("bill", hot, GROSS_NL[25] // 2), short.memo))  # half the pack
    rpc.add("one", with_memos(transfer("cleo", hot, GROSS_NL[25]), twice.memo))
    rpc.add("two", with_memos(transfer("cleo", hot, GROSS_NL[25]), twice.memo))  # the same quote paid again
    rpc.add("hand", transfer("dirk", hot, GROSS_NL[25]))  # by hand, no quote

    assert await scan.scan_once() == 5
    assert (await collect(client, short.id, short_claim))["invites"] == []
    assert len((await collect(client, twice.id, twice_claim))["invites"]) == 1  # type: ignore[arg-type]
    assert await pool.fetchval("SELECT COUNT(*) FROM invites") == 1


async def test_purchase_routes_hand_out_a_claim_that_is_only_stored_hashed(
    client: httpx.AsyncClient, pool: asyncpg.Pool, shop: ChainRpc
) -> None:
    body = {"address": str(Keypair().pubkey()), "checks": 50, "country": "NL"}
    reply = (await client.post("/v1/credits/purchase-tx", json=body)).json()
    svg = await client.get("/v1/credits/solana-pay.svg", params={"checks": 25, "country": "NL"})
    claims = {reply["claim"]: reply["quote_id"], svg.headers["x-unfret-claim"]: svg.headers["x-unfret-quote"]}
    for claim, quote_id in claims.items():
        stored = await pool.fetchval("SELECT claim_hash FROM vat_quotes WHERE id = $1", quote_id)
        assert bytes(stored) == hashlib.sha256(claim.encode()).digest()
        assert claim not in reply["transaction"] and claim not in reply["solana_pay_url"]  # never on chain
    rows = str([dict(r) for r in await pool.fetch("SELECT * FROM vat_quotes")])
    assert not any(claim in rows for claim in claims)


@pytest.fixture
async def shop(client: httpx.AsyncClient, pool: asyncpg.Pool) -> ChainRpc:
    rpc = ChainRpc()
    door = CreditsDoor(settings, rpc, HOT.pubkey(), PriceBook(settings, StaticOracle("100")))  # type: ignore[arg-type]
    door.attach(pool)
    assert door.scanner is not None and door.usdc_scanner is not None
    await door.scanner.initialise()
    await door.usdc_scanner.initialise()
    app.credits = door
    return rpc


async def test_two_reads_at_once_get_the_codes_once(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    rpc, scan = await scanner(pool)
    quote, claim = await quote_with_claim(pool, 100)
    rpc.add("buy", with_memos(transfer("anna", str(HOT.pubkey()), GROSS_NL[100]), quote.memo))
    await scan.scan_once()

    results = await asyncio.gather(*(invites.take_from_quote(pool, quote.id, claim) for _ in range(5)))
    assert sorted(len(r) for r in results) == [0, 0, 0, 0, 4]


# ── Issuing with card purchases ───────────────────────────────────────────────


@pytest.fixture
async def provider(client: httpx.AsyncClient) -> AsyncIterator[FakeFiatProvider]:
    app.fiat = FakeFiatProvider(SECRET)
    yield app.fiat
    app.fiat = None


@pytest.mark.parametrize("pack", [(25, 600, 1), (50, 1150, 2), (100, 2200, 4)])
async def test_a_card_pack_comes_with_its_invites_shown_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider, pack: tuple[int, int, int]
) -> None:
    checks, cents, expected = pack
    issued = await keys.create(pool)
    started = (await client.post("/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": checks})).json()
    assert started["claim"] not in started["checkout_url"]  # the provider never learns it
    status = f"/v1/credits/fiat-checkouts/{started['checkout_id']}"
    claim = {"X-Unfret-Claim": started["claim"]}
    paid = {"type": "order.paid", "data": {"id": f"ord_{checks}", "checkout_id": started["checkout_id"]}}
    paid["data"]["total_amount"] = cents  # type: ignore[index]

    assert (await send(client, provider, paid)).json() == {"status": "credited"}
    assert (await send(client, provider, paid)).json() == {"status": "duplicate"}

    assert (await client.get(status)).status_code == 404  # no token
    assert (await client.get(status, headers={"X-Unfret-Claim": invites.new_claim()})).status_code == 404
    shown = (await client.get(status, headers=claim)).json()
    assert shown["paid"] and shown["checks"] == checks and len(shown["invites"]) == expected
    assert (await client.get(status, headers=claim)).json()["invites"] == []
    assert await pool.fetchval("SELECT COUNT(*) FROM invites") == expected
    assert await pool.fetchval("SELECT credited_usd_micros FROM fiat_payments") == checks * 200_000  # no gift in it


async def test_an_odd_card_amount_or_a_checkout_without_a_claim_issues_none(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    started = (await client.post("/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25})).json()
    odd = {"type": "order.paid", "data": {"id": "ord_o", "checkout_id": started["checkout_id"], "total_amount": 1000}}
    assert (await send(client, provider, odd)).json() == {"status": "credited"}  # credits what it covers
    status = f"/v1/credits/fiat-checkouts/{started['checkout_id']}"
    shown = (await client.get(status, headers={"X-Unfret-Claim": started["claim"]})).json()
    assert shown == {"paid": True, "checks": 25, "invites": []}
    metadata_only = {"type": "order.paid", "data": {"id": "ord_m", "metadata": {"account": issued.account}}}
    metadata_only["data"]["total_amount"] = 600  # type: ignore[index]
    assert (await send(client, provider, metadata_only)).json() == {"status": "credited"}
    assert await pool.fetchval("SELECT COUNT(*) FROM invites") == 0


async def test_uncollected_card_codes_are_wiped_after_a_week(
    client: httpx.AsyncClient, pool: asyncpg.Pool, provider: FakeFiatProvider
) -> None:
    issued = await keys.create(pool)
    started = (await client.post("/v1/credits/fiat-checkout", json={"key_id": issued.key_id, "checks": 25})).json()
    paid = {"type": "order.paid", "data": {"id": "ord_w", "checkout_id": started["checkout_id"], "total_amount": 600}}
    await send(client, provider, paid)

    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=6))
    assert await pool.fetchval("SELECT invite_codes FROM fiat_checkouts") is not None
    await fiat.purge(pool, 180, now=datetime.now(UTC) + timedelta(days=8))
    assert await pool.fetchval("SELECT invite_codes FROM fiat_checkouts") is None
    assert await pool.fetchval("SELECT COUNT(*) FROM invites") == 1  # the hash stays: no expiry, just undeliverable


# ── Redeeming ─────────────────────────────────────────────────────────────────


async def test_a_fresh_wallet_redeems_an_invite_once(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    code = await an_invite(pool)
    address = str(Keypair().pubkey())

    response = await redeem(client, f"  {code.upper()} ", bearer(address))  # as pasted

    assert response.status_code == 200
    assert response.json() == {
        "checks_added": 3,
        "usd_micros_added": INVITE,
        "balance_usd_micros": INVITE,
        "checks_remaining": 3,
    }
    row = await pool.fetchrow("SELECT * FROM wallets WHERE pubkey = $1", address)
    assert (row["balance_usd_micros"], row["invite_redeemed"]) == (INVITE, True)
    assert await pool.fetchval("SELECT redeemed_on FROM invites") == datetime.now(UTC).date()
    # A gift: no sales record, no fiat payment.
    assert await pool.fetchval("SELECT COUNT(*) FROM crypto_sales") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0

    again = await redeem(client, code, bearer(str(Keypair().pubkey())))  # the same code for someone else
    assert (again.status_code, again.json()["error_code"]) == (409, "INVITE_USED")
    second = await redeem(client, await an_invite(pool), bearer(address))  # a second invite for the same wallet
    assert (second.status_code, second.json()["error_code"]) == (403, "INVITE_NOT_FOR_THIS_ACCOUNT")
    assert await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address) == INVITE


async def test_accounts_that_had_checks_are_refused(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    code = await an_invite(pool)
    buyer = str(Keypair().pubkey())
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, 5000000)", buyer)
    spent = str(Keypair().pubkey())  # bought once, spent it all
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, 0)", spent)
    funded = await keys.create(pool)
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, 200000)", funded.account)

    for headers in (bearer(buyer), bearer(spent), {"Authorization": f"Bearer {funded.key}"}):
        assert (await client.get("/v1/credits", headers=headers)).json()["invite_eligible"] is False
        response = await redeem(client, code, headers)
        assert (response.status_code, response.json()["error_code"]) == (403, "INVITE_NOT_FOR_THIS_ACCOUNT")
    assert await pool.fetchval("SELECT redeemed_on FROM invites") is None  # the code is still good

    fresh = await keys.create(pool)
    before = await client.get("/v1/credits", headers={"Authorization": f"Bearer {fresh.key}"})
    assert (before.json()["checks_remaining"], before.json()["invite_eligible"]) == (0, True)
    ok = await redeem(client, code, {"Authorization": f"Bearer {fresh.key}"})
    assert ok.status_code == 200 and ok.json()["checks_remaining"] == 3
    after = await client.get("/v1/credits", headers={"Authorization": f"Bearer {fresh.key}"})
    assert (after.json()["checks_remaining"], after.json()["invite_eligible"]) == (3, False)


async def test_invalid_unknown_and_unauthenticated(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    headers = bearer(str(Keypair().pubkey()))
    bad = await redeem(client, "free-checks-please", headers)
    assert (bad.status_code, bad.json()["error_code"]) == (400, "INVALID_INVITE")
    unknown = await redeem(client, invites.new_code(), headers)
    assert (unknown.status_code, unknown.json()["error_code"]) == (404, "UNKNOWN_INVITE")
    assert (await redeem(client, await an_invite(pool), {})).status_code == 401
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 0


async def test_two_new_accounts_racing_for_one_code(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    code = await an_invite(pool)
    replies = await asyncio.gather(*(redeem(client, code, bearer(str(Keypair().pubkey()))) for _ in range(4)))
    assert sorted(r.status_code for r in replies) == [200, 409, 409, 409]
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 1


# ── Nothing links a buyer to an invitee ───────────────────────────────────────


async def test_invites_store_no_link_to_anyone(pool: asyncpg.Pool) -> None:
    columns = {
        r["column_name"]: r["data_type"]
        for r in await pool.fetch(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'invites'"
        )
    }
    assert columns == {"code_hash": "bytea", "issued_on": "date", "redeemed_on": "date"}  # days, not times
    added = await pool.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'wallets' AND column_name = 'invite_redeemed'"
    )
    assert added == "boolean"  # that it redeemed one, never which
    code = await an_invite(pool)
    assert code.encode() not in str([dict(r) for r in await pool.fetch("SELECT * FROM invites")]).encode()
    assert base64.b32decode(code.removeprefix("ufi_").upper())  # 160 random bits


async def test_migration_15_leaves_existing_accounts_ineligible(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, MIGRATIONS[:14])
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ('old', 0)")
    assert 15 in await migrate(pool)
    assert await pool.fetchval("SELECT invite_redeemed FROM wallets") is False
    with pytest.raises(invites.InviteError) as refused:
        await invites.redeem(pool, settings, "old", await an_invite(pool))
    assert refused.value.kind == "not_eligible"


async def test_the_operator_sees_invites_outstanding(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await an_invite(pool)
    await invites.redeem(pool, settings, str(Keypair().pubkey()), await an_invite(pool))
    assert await invites.summary(pool, settings) == {
        "issued": 2,
        "redeemed": 1,
        "outstanding": 1,
        "outstanding_usd_micros": INVITE,
    }
