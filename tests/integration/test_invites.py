"""Invites: issued with every credited pack (crypto and card), shown to the buyer once, redeemed once."""

import asyncio
import base64
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import pytest
from solders.keypair import Keypair

from tests.integration.conftest import DROP_ALL, bearer
from tests.integration.test_credits import HOT, SOL_BOOK, ChainRpc, transfer, with_memos
from tests.integration.test_fiat import SECRET, send
from tests.unit.x402_helpers import StaticOracle
from unfret.config import settings
from unfret.credits import CreditsDoor, fiat, invites, keys, ledger, vat
from unfret.credits.fiat import FakeFiatProvider
from unfret.credits.scanner import CreditScanner
from unfret.main import app
from unfret.migrations import MIGRATIONS, migrate
from unfret.prices import PriceBook

INVITE = 5 * settings.analysis_price_usd_micros  # 5 checks: $1.00
PRICE = {25: 5_000_000, 50: 10_000_000, 100: 20_000_000}  # the packs, VAT included, in lamports at $1,000/SOL


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


async def an_invite(pool: asyncpg.Pool, buyer: str | None = None) -> str:
    """One issued invite code, as a buyer would hand it on."""
    async with pool.acquire() as conn, conn.transaction():
        quote, _ = await quote_with_claim(pool, 25)
        assert await invites.issue_for_quote(conn, settings, quote.id, quote.gross_usd_micros, buyer=buyer) == 1
        codes = await conn.fetchval("SELECT invite_codes FROM vat_quotes WHERE id = $1", quote.id)
    return str(codes[0])


async def redeem(client: httpx.AsyncClient, code: str, headers: dict[str, str]) -> httpx.Response:
    return await client.post("/v1/invites/redeem", json={"code": code}, headers=headers)


# ── Issuing with crypto purchases ─────────────────────────────────────────────


# 50: a quote for the 50-pack made before it went off sale still pays, and still brings its two codes.
@pytest.mark.parametrize(("checks", "expected"), [(25, 1), (50, 2), (100, 4)])
async def test_a_crypto_pack_comes_with_its_invites_shown_once(
    client: httpx.AsyncClient, pool: asyncpg.Pool, checks: int, expected: int
) -> None:
    rpc, scan = await scanner(pool)
    quote, claim = await quote_with_claim(pool, checks)
    rpc.add("buy", with_memos(transfer("anna", str(HOT.pubkey()), PRICE[checks]), quote.memo))
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
    rpc.add("old", with_memos(transfer("anna", hot, PRICE[25]), unclaimable.memo))
    rpc.add("short", with_memos(transfer("bill", hot, PRICE[25] // 2), short.memo))  # half the pack
    rpc.add("one", with_memos(transfer("cleo", hot, PRICE[25]), twice.memo))
    rpc.add("two", with_memos(transfer("cleo", hot, PRICE[25]), twice.memo))  # the same quote paid again
    rpc.add("hand", transfer("dirk", hot, PRICE[25]))  # by hand, no quote

    assert await scan.scan_once() == 5
    assert (await collect(client, short.id, short_claim))["invites"] == []
    assert len((await collect(client, twice.id, twice_claim))["invites"]) == 1  # type: ignore[arg-type]
    assert await pool.fetchval("SELECT COUNT(*) FROM invites") == 1


async def test_purchase_routes_hand_out_a_claim_that_is_only_stored_hashed(
    client: httpx.AsyncClient, pool: asyncpg.Pool, shop: ChainRpc
) -> None:
    body = {"address": str(Keypair().pubkey()), "checks": 100, "country": "NL"}
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
    rpc.add("buy", with_memos(transfer("anna", str(HOT.pubkey()), PRICE[100]), quote.memo))
    await scan.scan_once()

    results = await asyncio.gather(*(invites.take_from_quote(pool, quote.id, claim) for _ in range(5)))
    assert sorted(len(r) for r in results) == [0, 0, 0, 0, 4]


# ── Issuing with card purchases ───────────────────────────────────────────────


@pytest.fixture
async def provider(client: httpx.AsyncClient) -> AsyncIterator[FakeFiatProvider]:
    app.fiat = FakeFiatProvider(SECRET)
    yield app.fiat
    app.fiat = None


@pytest.mark.parametrize("pack", [(25, 600, 1), (100, 2000, 4)])
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
    assert shown == {"paid": True, "checks": 25, "invites": [], "reissued": False}
    metadata_only = {
        "type": "order.paid",
        "data": {"id": "ord_m", "metadata": {"account": issued.account, "site": settings.public_base_url}},
    }
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
        "checks_added": 5,
        "usd_micros_added": INVITE,
        "balance_usd_micros": INVITE,
        "checks_remaining": 5,
    }
    row = await pool.fetchrow("SELECT * FROM wallets WHERE pubkey = $1", address)
    assert (row["balance_usd_micros"], row["invite_redeemed"]) == (INVITE, True)
    assert row["gift_month"] == datetime.now(UTC).date().replace(day=1)  # the month only, never which code
    assert await pool.fetchval("SELECT redeemed_on FROM invites") == datetime.now(UTC).date()
    # A gift: no sales record, no fiat payment.
    assert await pool.fetchval("SELECT COUNT(*) FROM crypto_sales") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM fiat_payments") == 0

    again = await redeem(client, code, bearer(str(Keypair().pubkey())))  # the same code for someone else
    assert (again.status_code, again.json()["error_code"]) == (409, "INVITE_USED")


async def test_one_gift_code_a_month_and_the_second_stays_unused(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    address = str(Keypair().pubkey())
    assert (await redeem(client, await an_invite(pool), bearer(address))).status_code == 200
    second = await an_invite(pool)

    refused = await redeem(client, second, bearer(address))  # a second code in the same month

    today = datetime.now(UTC).date()
    assert (refused.status_code, refused.json()["error_code"]) == (429, "INVITE_MONTHLY_LIMIT")
    assert refused.json()["detail"] == (
        "You've already used a gift code this month; this one still works next month, or give it to someone else."
    )
    assert refused.json()["metadata"] == {"next_month": invites.next_month(today).isoformat()}
    assert await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address) == INVITE
    assert await pool.fetchval("SELECT COUNT(*) FROM invites WHERE redeemed_on IS NULL") == 1  # still unused
    # Someone else can use it now…
    assert (await redeem(client, second, bearer(str(Keypair().pubkey())))).status_code == 200


async def test_a_gift_code_works_again_next_month(pool: asyncpg.Pool) -> None:
    address = str(Keypair().pubkey())
    today = datetime.now(UTC).date()
    following = invites.next_month(today)
    await invites.redeem(pool, settings, address, await an_invite(pool), today=today)
    kept = await an_invite(pool)
    with pytest.raises(invites.InviteError) as refused:
        last_day = following - timedelta(days=1)
        await invites.redeem(pool, settings, address, kept, today=last_day)  # the last day of the same month
    assert refused.value.kind == "monthly"
    done = await invites.redeem(pool, settings, address, kept, today=following)  # the 1st of the next (UTC)
    assert done.balance_usd_micros == 2 * INVITE
    assert await pool.fetchval("SELECT gift_month FROM wallets WHERE pubkey = $1", address) == following
    assert await pool.fetchval("SELECT MAX(redeemed_on) FROM invites") == following
    with pytest.raises(invites.InviteError):
        await invites.redeem(pool, settings, address, await an_invite(pool), today=following + timedelta(days=5))


@pytest.mark.parametrize(
    ("day", "first"),
    [
        (date(2026, 11, 17), date(2026, 12, 1)),
        (date(2026, 12, 31), date(2027, 1, 1)),
        (date(2027, 1, 1), date(2027, 2, 1)),
    ],
)
def test_next_month(day: date, first: date) -> None:
    assert invites.next_month(day) == first


async def test_two_codes_at_once_on_one_account_credit_one(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    address = str(Keypair().pubkey())
    codes = [await an_invite(pool) for _ in range(4)]
    replies = await asyncio.gather(*(redeem(client, code, bearer(address)) for code in codes))
    assert sorted(r.status_code for r in replies) == [200, 429, 429, 429]
    assert await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address) == INVITE
    assert await pool.fetchval("SELECT COUNT(*) FROM invites WHERE redeemed_on IS NULL") == 3


async def test_a_pass_in_use_redeems_a_friends_code(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    friend = await keys.create(pool)
    await pool.execute(
        "INSERT INTO wallets (pubkey, balance_usd_micros, purchased) VALUES ($1, 2000000, TRUE)", friend.account
    )
    code = await an_invite(pool, buyer="key:someoneelse")

    response = await redeem(client, code, {"Authorization": f"Bearer {friend.key}"})

    assert response.status_code == 200
    assert response.json()["balance_usd_micros"] == 2_000_000 + INVITE
    assert await pool.fetchval("SELECT redeemed_on FROM invites") is not None


async def test_the_buyers_own_pass_is_refused_and_the_code_stays_good(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    buyer = await keys.create(pool)
    code = await an_invite(pool, buyer=buyer.account)

    own = await redeem(client, code, {"Authorization": f"Bearer {buyer.key}"})

    assert (own.status_code, own.json()["error_code"]) == (403, "INVITE_OWN_PACK")
    assert own.json()["detail"] == "This gift code came with your own pack: give it to a friend."
    assert await pool.fetchval("SELECT redeemed_on FROM invites") is None  # still good for a friend
    assert (await redeem(client, code, bearer(str(Keypair().pubkey())))).status_code == 200


async def test_invalid_unknown_and_unauthenticated(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    headers = bearer(str(Keypair().pubkey()))
    bad = await redeem(client, "free-checks-please", headers)
    assert (bad.status_code, bad.json()["error_code"]) == (400, "INVALID_INVITE")
    unknown = await redeem(client, invites.new_code(), headers)
    assert (unknown.status_code, unknown.json()["error_code"]) == (404, "UNKNOWN_INVITE")
    assert (await redeem(client, await an_invite(pool), {})).status_code == 401
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 0


async def test_a_former_intro_code_is_like_any_unknown_code(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    # A database from before the purge, with an intro code and its day counter: migration 33 drops both.
    await pool.execute(DROP_ALL)
    await migrate(pool, [m for m in MIGRATIONS if m[0] < 33])
    await pool.execute("INSERT INTO intro_codes (code, checks, max_uses) VALUES ('FRIENDS', 5, 40)")
    await pool.execute("INSERT INTO usage_counters (name, period, count) VALUES ('intro_checks', '2026-11-01', 5)")
    assert 33 in await migrate(pool)
    assert await pool.fetchval("SELECT to_regclass('intro_codes')") is None
    assert await pool.fetchval("SELECT COUNT(*) FROM usage_counters WHERE name = 'intro_checks'") == 0

    headers = bearer(str(Keypair().pubkey()))
    answers = [await redeem(client, code, headers) for code in ("FRIENDS", " friends ", "DEV", "NEVERACODE")]
    assert {(a.status_code, a.json()["error_code"], a.json()["detail"]) for a in answers} == {
        (400, "INVALID_INVITE", "That isn't a gift code.")
    }
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 0  # nothing redeemed, no account made


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
    # Days, not times; the buyer only as a keyed hash (so their own pass can't take it), never the redeemer.
    assert columns == {"code_hash": "bytea", "issued_on": "date", "redeemed_on": "date", "buyer_hash": "bytea"}
    added = await pool.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'wallets' AND column_name = 'invite_redeemed'"
    )
    assert added == "boolean"  # that it redeemed one, never which
    month = await pool.fetchval(
        "SELECT data_type FROM information_schema.columns WHERE table_name = 'wallets' AND column_name = 'gift_month'"
    )
    assert month == "date"  # the month of its latest one, never which
    code = await an_invite(pool, buyer="key:abcdefghij")
    stored = str([dict(r) for r in await pool.fetch("SELECT * FROM invites")]).encode()
    assert code.encode() not in stored
    assert b"abcdefghij" not in stored
    assert await pool.fetchval("SELECT buyer_hash FROM invites") != hashlib.sha256(b"key:abcdefghij").digest()
    assert base64.b32decode(code.removeprefix("ufi_").upper())  # 160 random bits


async def test_accounts_from_before_invites_redeem_gift_codes_too(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, MIGRATIONS[:14])
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ('old', 0)")
    assert 15 in await migrate(pool)
    assert await pool.fetchval("SELECT invite_redeemed FROM wallets") is False
    done = await invites.redeem(pool, settings, "old", await an_invite(pool))
    assert done.balance_usd_micros == INVITE


async def test_the_operator_sees_invites_outstanding(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await an_invite(pool)
    await invites.redeem(pool, settings, str(Keypair().pubkey()), await an_invite(pool))
    assert await invites.summary(pool, settings) == {
        "issued": 2,
        "redeemed": 1,
        "outstanding": 1,
        "outstanding_usd_micros": INVITE,
        "via_code": 1,
        "via_code_bought": 0,
        "organic_bought": 0,
    }


async def test_the_operator_counts_buyers_by_how_they_came(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    came = str(Keypair().pubkey())
    await invites.redeem(pool, settings, came, await an_invite(pool))
    await ledger.credit(pool, came, 5_000_000, purchase=True)  # later bought a pack
    await ledger.credit(pool, str(Keypair().pubkey()), 5_000_000, purchase=True)  # organic buyer
    await ledger.credit(pool, str(Keypair().pubkey()), 200_000)  # a refund: not a purchase
    summary = await invites.summary(pool, settings)
    assert (summary["via_code"], summary["via_code_bought"], summary["organic_bought"]) == (1, 1, 1)


async def test_a_refunded_card_purchase_is_no_buyer(pool: asyncpg.Pool) -> None:
    async def card(account: str, order: str, *, refunded: int = 0, disputed: bool = False) -> None:
        await ledger.credit(pool, account, 5_000_000, purchase=True)
        await pool.execute(
            "INSERT INTO fiat_payments (provider, order_id, account, amount_cents, credited_usd_micros, "
            "refunded_cents, disputed) VALUES ('polar', $1, $2, 500, 5000000, $3, $4)",
            order,
            account,
            refunded,
            disputed,
        )

    refunded, disputed, partly, kept = (str(Keypair().pubkey()) for _ in range(4))
    await card(refunded, "o-1", refunded=500)  # the owner's own test purchase, refunded in full
    await card(disputed, "o-2", disputed=True)
    await card(partly, "o-3", refunded=200)  # partly refunded: still a buyer
    await card(kept, "o-4", refunded=500)
    await card(kept, "o-5")  # a second order it kept
    await ledger.credit(pool, str(Keypair().pubkey()), 5_000_000, purchase=True)  # crypto: never refunded
    assert (await invites.summary(pool, settings))["organic_bought"] == 3
