"""Intro codes: named, operator-made, many uses each; new accounts only, like an invite; an optional daily cap."""

import asyncio

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from tests.integration.conftest import bearer
from unfret.config import settings
from unfret.credits import invites, keys, ledger

ADMIN = {"Authorization": "Bearer operator-token"}
FIVE = 5 * settings.analysis_price_usd_micros


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))


async def make(client: httpx.AsyncClient, code: str, uses: int, checks: int = 5) -> httpx.Response:
    return await client.post("/v1/admin/codes", json={"code": code, "checks": checks, "max_uses": uses}, headers=ADMIN)


async def redeem(client: httpx.AsyncClient, code: str, headers: dict[str, str]) -> httpx.Response:
    return await client.post("/v1/invites/redeem", json={"code": code}, headers=headers)


def new_wallet() -> dict[str, str]:
    return bearer(str(Keypair().pubkey()))


async def test_a_code_gives_new_accounts_its_checks_until_its_uses_run_out(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    made = await make(client, "friends", uses=2)
    assert made.status_code == 201
    assert made.json() | {"created_on": None} == {
        "code": "FRIENDS",
        "checks": 5,
        "max_uses": 2,
        "uses": 0,
        "active": True,
        "created_on": None,
    }
    key = await keys.create(pool)
    first = await redeem(client, " Friends ", {"Authorization": f"Bearer {key.key}"})  # as typed
    assert first.status_code == 200
    assert first.json() == {
        "checks_added": 5,
        "usd_micros_added": FIVE,
        "balance_usd_micros": FIVE,
        "checks_remaining": 5,
    }
    assert (await redeem(client, "FRIENDS", new_wallet())).status_code == 200
    third = await redeem(client, "FRIENDS", new_wallet())
    assert (third.status_code, third.json()["error_code"]) == (409, "INVITE_USED")
    # Nothing says which code an account used: only the invite flag.
    row = await pool.fetchrow("SELECT * FROM wallets WHERE pubkey = $1", key.account)
    assert set(row.keys()) == {"pubkey", "balance_usd_micros", "trial_granted", "invite_redeemed", "purchased"}
    assert (row["invite_redeemed"], row["purchased"]) == (True, False)


async def test_new_accounts_only_and_one_invite_or_code_each(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await make(client, "DEV", uses=10)
    await make(client, "ACW", uses=10)
    buyer = str(Keypair().pubkey())
    await ledger.credit(pool, buyer, 1_000_000, purchase=True)
    refused = await redeem(client, "DEV", bearer(buyer))
    assert (refused.status_code, refused.json()["error_code"]) == (403, "INVITE_NOT_FOR_THIS_ACCOUNT")
    fresh = new_wallet()
    assert (await redeem(client, "DEV", fresh)).status_code == 200
    again = await redeem(client, "ACW", fresh)  # a second code for the same account
    assert (again.status_code, again.json()["error_code"]) == (403, "INVITE_NOT_FOR_THIS_ACCOUNT")
    assert await pool.fetchval("SELECT uses FROM intro_codes WHERE code = 'ACW'") == 0


async def test_unknown_switched_off_and_more_uses(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    unknown = await redeem(client, "NOPE", new_wallet())
    assert (unknown.status_code, unknown.json()["error_code"]) == (404, "UNKNOWN_INVITE")
    await make(client, "LINKEDIN", uses=1)
    off = await client.post("/v1/admin/codes/linkedin", json={"active": False}, headers=ADMIN)
    assert off.json()["active"] is False
    ended = await redeem(client, "LINKEDIN", new_wallet())
    assert (ended.status_code, ended.json()["error_code"]) == (410, "INVITE_ENDED")
    await client.post("/v1/admin/codes/LINKEDIN", json={"active": True}, headers=ADMIN)
    assert (await redeem(client, "LINKEDIN", new_wallet())).status_code == 200
    assert (await redeem(client, "LINKEDIN", new_wallet())).status_code == 409
    more = await client.post("/v1/admin/codes/LINKEDIN", json={"add_uses": 5}, headers=ADMIN)
    assert (more.json()["uses"], more.json()["max_uses"]) == (1, 6)
    assert (await redeem(client, "LINKEDIN", new_wallet())).status_code == 200
    missing = await client.post("/v1/admin/codes/OTHER", json={"active": False}, headers=ADMIN)
    assert missing.status_code == 404


async def test_the_daily_cap_spans_all_codes(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "intro_daily_checks_cap", 12)
    await make(client, "ALPHA", uses=10)
    await make(client, "BRAVO", uses=10)
    assert (await redeem(client, "ALPHA", new_wallet())).status_code == 200  # 5
    assert (await redeem(client, "BRAVO", new_wallet())).status_code == 200  # 10
    capped = await redeem(client, "ALPHA", new_wallet())  # 15 > 12
    assert (capped.status_code, capped.json()["error_code"]) == (429, "INTRO_DAILY_CAP")
    assert "tomorrow" in capped.json()["detail"]
    assert await pool.fetchval("SELECT COUNT(*) FROM wallets") == 2  # the refused account got nothing
    # Invites from packs are not capped.
    code = invites.new_code()
    await pool.execute("INSERT INTO invites (code_hash, issued_on) VALUES ($1, CURRENT_DATE)", invites.digest(code))
    assert (await redeem(client, code, new_wallet())).status_code == 200


async def test_no_daily_cap_by_default(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    assert settings.intro_daily_checks_cap is None  # an emergency brake, off unless set
    await make(client, "MANY", uses=100)
    replies = [await redeem(client, "MANY", new_wallet()) for _ in range(60)]  # 300 checks: past the old cap of 250
    assert {r.status_code for r in replies} == {200}
    listed = (await client.get("/v1/admin/codes", headers=ADMIN)).json()
    assert listed["today"] == {"checks_given": 300, "cap": None}


async def test_a_zero_cap_pauses_every_code(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "intro_daily_checks_cap", 0)
    await make(client, "PAUSED", uses=10)
    refused = await redeem(client, "PAUSED", new_wallet())
    assert (refused.status_code, refused.json()["error_code"]) == (429, "INTRO_DAILY_CAP")


async def test_racing_for_the_last_use(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await make(client, "LAST", uses=1)
    replies = await asyncio.gather(*(redeem(client, "LAST", new_wallet()) for _ in range(4)))
    assert sorted(r.status_code for r in replies) == [200, 409, 409, 409]
    assert await pool.fetchval("SELECT uses FROM intro_codes") == 1


async def test_the_operator_list_counts_uses_today_and_buyers(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await make(client, "FRIENDS", uses=40)
    came = str(Keypair().pubkey())
    assert (await redeem(client, "FRIENDS", bearer(came))).status_code == 200
    await ledger.credit(pool, came, 5_000_000, purchase=True)  # later bought a pack
    await ledger.credit(pool, str(Keypair().pubkey()), 5_000_000, purchase=True)  # organic buyer
    await ledger.credit(pool, str(Keypair().pubkey()), 200_000)  # a refund: not a purchase

    listed = (await client.get("/v1/admin/codes", headers=ADMIN)).json()
    assert [(c["code"], c["uses"], c["max_uses"]) for c in listed["codes"]] == [("FRIENDS", 1, 40)]
    assert listed["today"] == {"checks_given": 5, "cap": settings.intro_daily_checks_cap}
    assert listed["accounts"] == {"via_code": 1, "via_code_bought": 1, "organic_bought": 1}
    assert (await client.get("/v1/admin/codes")).status_code == 401


async def test_a_refunded_card_purchase_is_no_buyer(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
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
    listed = (await client.get("/v1/admin/codes", headers=ADMIN)).json()
    assert listed["accounts"]["organic_bought"] == 3


async def test_names_are_checked_and_unique(client: httpx.AsyncClient) -> None:
    assert (await make(client, "FRIENDS", uses=1)).status_code == 201
    assert (await make(client, "friends", uses=1)).status_code == 409
    assert (await make(client, "no spaces", uses=1)).status_code in {400, 422}


async def test_a_shared_link_opens_checkout_with_the_code(client: httpx.AsyncClient) -> None:
    shared = await client.get("/key", params={"code": " friends"})
    assert (shared.status_code, shared.headers["location"]) == (302, "/checkout?for=key&code=FRIENDS")
    junk = await client.get("/key", params={"code": "<script>"})
    assert junk.headers["location"] == "/checkout?for=key"
