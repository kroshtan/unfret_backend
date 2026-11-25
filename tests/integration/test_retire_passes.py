"""Retiring passes (the 0.13.0 fresh start): counts in a dry run, exactly the right rows on confirm, keep, refusals."""

from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from unfret.config import settings
from unfret.credits import keys, ledger, retire
from unfret.reports import account_tag

ADMIN = {"Authorization": "Bearer operator-token"}
CHECK = settings.analysis_price_usd_micros
TODAY = datetime.now(UTC).date()
EARLIER = TODAY - timedelta(days=3)


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))


async def make_pass(pool: asyncpg.Pool, made_on: date, *, free: int = 0, balance: int = 0) -> keys.IssuedKey:
    issued = await keys.create(pool, free_checks=free)
    await pool.execute("UPDATE api_keys SET created_on = $2 WHERE id = $1", issued.key_id, made_on)
    if balance:
        await ledger.credit(pool, issued.account, balance)
    return issued


async def retire_call(client: httpx.AsyncClient, **body: object) -> dict[str, object]:
    body.setdefault("before", datetime.now(UTC).isoformat())
    response = await client.post("/v1/admin/passes/retire", json=body, headers=ADMIN)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


async def snapshot(pool: asyncpg.Pool) -> dict[str, int]:
    tables = (
        "api_keys",
        "wallets",
        "fiat_checkouts",
        "fiat_payments",
        "vat_quotes",
        "reports",
        "report_votes",
        "report_quota",
    )
    return {t: int(await pool.fetchval(f"SELECT count(*) FROM {t}")) for t in tables}  # noqa: S608 - fixed names


async def attach_everything(pool: asyncpg.Pool, account: str) -> None:
    """Every kind of row that can name a pass: checkout, quote, report, report limits."""
    await pool.execute(
        "INSERT INTO fiat_checkouts (provider, checkout_id, account) VALUES ('polar', $1, $2)",
        "co-" + account,
        account,
    )
    await pool.execute(
        "INSERT INTO vat_quotes (id, checks, country, country_mismatch, vat_rate_bps, net_usd_micros, vat_usd_micros, "
        "beneficiary) VALUES ($1, 25, 'NL', FALSE, 2100, 5000000, 1050000, $2)",
        ("q" + account.removeprefix("key:") * 2)[:20],
        account,
    )
    report = await pool.fetchval(
        "INSERT INTO reports (page_key, domain, account, reason, score, payload) "
        "VALUES ($1, 'shop.example', $2, 'scam', 80, '{}') RETURNING id",
        "page-" + account,
        account,
    )
    tag = account_tag(settings.session_secret.get_secret_value(), account)
    await pool.execute("INSERT INTO report_votes (report_id, account_tag) VALUES ($1, $2)", report, tag)
    await pool.execute("INSERT INTO report_quota (account_tag, month, count) VALUES ($1, '2026-11', 1)", tag)


async def test_a_dry_run_counts_and_changes_nothing(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    old = await make_pass(pool, EARLIER, balance=5 * CHECK)
    await attach_everything(pool, old.account)
    await make_pass(pool, EARLIER)
    await make_pass(pool, TODAY)  # today, no free checks: made before the moment
    await make_pass(pool, TODAY, free=5)  # today with free checks: the new flow's, kept
    await ledger.credit(pool, str(Keypair().pubkey()), 1)
    await pool.execute("UPDATE wallets SET balance_usd_micros = 0 WHERE pubkey NOT LIKE 'key:%'")
    before = await snapshot(pool)

    result = await retire_call(client)

    assert await snapshot(pool) == before
    assert result["retired"] is False and result["refused"] == []
    assert {k: result[k] for k in ("passes", "passes_earlier_days", "passes_same_day", "kept_same_day_free")} == {
        "passes": 3,
        "passes_earlier_days": 2,
        "passes_same_day": 1,
        "kept_same_day_free": 1,
    }
    assert (result["with_balance"], result["balance_checks"]) == (1, 5)
    assert (result["card_checkouts"], result["crypto_quotes"]) == (1, 1)
    assert (result["open_reports"], result["report_limits"]) == (1, 2)
    assert (result["wallets"], result["wallets_with_balance"]) == (1, 0)
    text = str(result)
    assert old.key_id not in text and "ufk_" not in text  # counts only


async def test_confirm_retires_exactly_the_old_passes_and_what_names_them(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    old = await make_pass(pool, EARLIER, balance=3 * CHECK, free=0)
    await attach_everything(pool, old.account)
    reviewer = await make_pass(pool, EARLIER, balance=2 * CHECK)
    await attach_everything(pool, reviewer.account)
    new = await make_pass(pool, TODAY, free=5)
    later = await make_pass(pool, TODAY + timedelta(days=1))  # the clock says later: never touched
    wallet = str(Keypair().pubkey())
    await ledger.credit(pool, wallet, CHECK)
    await ledger.debit(pool, wallet, CHECK)  # a wallet row with nothing on it
    await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ('key:gonegonego', 7)")  # an orphan

    result = await retire_call(client, keep=[reviewer.key_id], confirm=True)

    assert result["retired"] is True
    assert (result["passes"], result["kept_named"], result["wallets"]) == (1, 1, 1)
    assert result["orphan_rows"] == 1
    left = {r["id"] for r in await pool.fetch("SELECT id FROM api_keys")}
    assert left == {reviewer.key_id, new.key_id, later.key_id}
    for table, column in (
        ("wallets", "pubkey"),
        ("fiat_checkouts", "account"),
        ("vat_quotes", "beneficiary"),
        ("reports", "account"),
    ):
        named = {r[0] for r in await pool.fetch(f"SELECT {column} FROM {table}")}  # noqa: S608 - fixed names
        assert old.account not in named and wallet not in named and "key:gonegonego" not in named
        assert reviewer.account in named  # the kept pass keeps everything
    secret = settings.session_secret.get_secret_value()
    tags = {bytes(r[0]) for r in await pool.fetch("SELECT account_tag FROM report_quota")}
    assert tags == {account_tag(secret, reviewer.account)}
    assert await ledger.balance(pool, reviewer.account) == 2 * CHECK
    counters = dict(await pool.fetch("SELECT name, count FROM usage_counters WHERE name LIKE '%_retired'"))
    assert counters == {retire.PASSES_COUNTER: 1, retire.WALLETS_COUNTER: 1}

    rejected = await client.get("/v1/credits", headers={"Authorization": f"Bearer {old.key}"})
    assert (rejected.status_code, rejected.json()["error_code"]) == (401, "INVALID_API_KEY")
    kept = await client.get("/v1/credits", headers={"Authorization": f"Bearer {reviewer.key}"})
    assert kept.status_code == 200


async def test_a_second_run_finds_nothing_left(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await make_pass(pool, EARLIER, balance=CHECK)
    assert (await retire_call(client, confirm=True))["passes"] == 1
    after = await snapshot(pool)

    again = await retire_call(client, confirm=True)

    assert (again["passes"], again["wallets"], again["orphan_rows"], again["retired"]) == (0, 0, 0, True)
    assert await snapshot(pool) == after
    assert await pool.fetchval("SELECT count FROM usage_counters WHERE name = $1", retire.PASSES_COUNTER) == 1


async def test_a_bare_date_retires_the_days_before_it_only(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await make_pass(pool, EARLIER)
    await make_pass(pool, TODAY)

    result = await retire_call(client, before=TODAY.isoformat(), confirm=True)

    assert (result["passes"], result["same_day"], result["passes_later"]) == (1, False, 1)
    assert await pool.fetchval("SELECT count(*) FROM api_keys") == 1


async def test_keeping_an_unknown_pass_refuses_everything(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await make_pass(pool, EARLIER)

    result = await retire_call(client, keep=["aaaaaaaaaa"], confirm=True)

    assert result["retired"] is False and result["keep_unknown"] == 1
    assert "check --keep" in str(result["refused"])
    assert await pool.fetchval("SELECT count(*) FROM api_keys") == 1


async def test_a_wallet_with_a_balance_is_retired_only_when_asked(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    await make_pass(pool, EARLIER)
    wallet = str(Keypair().pubkey())
    await ledger.credit(pool, wallet, 2 * CHECK)  # an x402 refund, say

    refused = await retire_call(client, confirm=True)

    assert refused["retired"] is False
    assert (refused["wallets_with_balance"], refused["wallet_balance_checks"]) == (1, 2)
    assert "--include-wallet-balances" in str(refused["refused"])
    assert await pool.fetchval("SELECT count(*) FROM api_keys") == 1
    assert await ledger.balance(pool, wallet) == 2 * CHECK

    done = await retire_call(client, confirm=True, include_wallet_balances=True)

    assert done["retired"] is True
    assert await ledger.balance(pool, wallet) is None


async def test_an_account_that_bought_is_retired_only_when_asked(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    buyer = await make_pass(pool, EARLIER)
    await ledger.credit(pool, buyer.account, 25 * CHECK, purchase=True)
    await pool.execute(
        "INSERT INTO fiat_payments (provider, order_id, account, amount_cents, credited_usd_micros) "
        "VALUES ('polar', 'o1', $1, 600, $2)",
        buyer.account,
        25 * CHECK,
    )

    refused = await retire_call(client, confirm=True)

    assert refused["purchases"] == 1 and "--include-purchases" in str(refused["refused"])
    assert await keys.exists(pool, buyer.key_id)

    done = await retire_call(client, confirm=True, include_purchases=True)

    assert done["retired"] is True
    assert await pool.fetchval("SELECT count(*) FROM fiat_payments") == 0


async def test_the_moment_may_not_be_in_the_future_and_needs_the_operator(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    response = await client.post("/v1/admin/passes/retire", json={"before": future}, headers=ADMIN)
    assert response.status_code == 422 and "future" in response.text
    bad_keep = {"before": TODAY.isoformat(), "keep": ["ufk_x"]}
    assert (await client.post("/v1/admin/passes/retire", json=bad_keep, headers=ADMIN)).status_code == 422
    assert (await client.post("/v1/admin/passes/retire", json={"before": TODAY.isoformat()})).status_code == 401


async def test_every_column_that_names_an_account_is_known_to_retiring(pool: asyncpg.Pool) -> None:
    # A new table naming a pass must be handled by unfret.credits.retire, or retired passes would leave it behind.
    rows = await pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name IN ('account', 'pubkey', 'beneficiary', 'account_tag', 'buyer_hash', 'payer', 'key_id')"
    )
    assert {(r["table_name"], r["column_name"]) for r in rows} == retire.ACCOUNT_COLUMNS
