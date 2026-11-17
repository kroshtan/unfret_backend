"""The outflow audit's state in Postgres (migration 23): a restart continues from the cursor and keeps the alarm."""

from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from tests.unit.x402_helpers import DEV_FORMER_COLD, FakeRpc, configured, devnet_tx, make_x402
from unfret.config import settings
from unfret.main import app
from unfret.migrations import MIGRATIONS
from unfret.x402.service import X402
from unfret.x402.solana import RpcError

HOT = Keypair()
COLD = Keypair().pubkey()
THIEF = str(Keypair().pubkey())
FORMER = {"former_cold_addresses": [str(DEV_FORMER_COLD)], "former_cold_until": "2026-11-16T06:00:00Z"}


class CountingRpc(FakeRpc):
    def __init__(self) -> None:
        super().__init__()
        self.fetched: list[str] = []

    async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
        self.fetched.append(signature)
        return self.transactions.get(signature)


def sweep() -> dict[str, Any]:
    return devnet_tx("sol_sweep_to_former_cold", HOT.pubkey())


def theft() -> dict[str, Any]:
    return devnet_tx("sol_sweep_to_former_cold", HOT.pubkey(), {str(DEV_FORMER_COLD): THIEF})


async def started(pool: asyncpg.Pool, rpc: FakeRpc) -> X402:
    """A process starting: a new service, attached to the database."""
    x402 = make_x402(configured(HOT, COLD, **FORMER), rpc)
    await x402.attach(pool)
    return x402


async def test_the_first_run_on_a_fresh_database_reads_the_newest_page(pool: asyncpg.Pool) -> None:
    rpc = CountingRpc()
    rpc.signatures = ["s2", "s1"]
    rpc.transactions.update(s2=sweep(), s1=sweep())
    x402 = await started(pool, rpc)
    assert x402._audit_cursor is None
    assert await x402.audit_outflows() == []
    assert rpc.fetched == ["s1", "s2"]
    assert (
        await pool.fetchval("SELECT signature FROM x402_audit_cursor WHERE hot_wallet = $1", str(HOT.pubkey())) == "s2"
    )


async def test_a_restart_continues_from_the_cursor(pool: asyncpg.Pool) -> None:
    rpc = CountingRpc()
    rpc.signatures = ["old"]
    rpc.transactions["old"] = sweep()
    await (await started(pool, rpc)).audit_outflows()

    rpc.fetched.clear()
    rpc.signatures = ["new", "old"]
    rpc.transactions["new"] = sweep()
    restarted = await started(pool, rpc)
    assert restarted._audit_cursor == "old"
    assert await restarted.audit_outflows() == []
    assert rpc.fetched == ["new"]  # the history before the cursor isn't read again


async def test_an_alarm_survives_a_restart_until_the_operator_clears_it(pool: asyncpg.Pool) -> None:
    rpc = CountingRpc()
    rpc.signatures = ["theft"]
    rpc.transactions["theft"] = theft()
    first = await started(pool, rpc)
    assert await first.audit_outflows() == [f"system transfer to {THIEF}"]

    second = await started(pool, rpc)
    assert (await second.health()).status == "compromise_suspected"
    assert [(f.signature, f.reason) for f in second.flagged()] == [("theft", f"system transfer to {THIEF}")]
    assert await second.clear_alerts() == [f"system transfer to {THIEF}"]

    third = await started(pool, rpc)
    assert (await third.health()).status == "ok"
    # Audited again (a cursor lost, a rolling deploy): what the operator cleared doesn't come back.
    await pool.execute("DELETE FROM x402_audit_cursor")
    fourth = await started(pool, rpc)
    assert await fourth.audit_outflows() == []
    assert (await fourth.health()).status == "ok"
    # A new outflow from the same wallet raises the alarm as ever.
    rpc.signatures = ["theft2", "theft"]
    rpc.transactions["theft2"] = theft()
    assert await fourth.audit_outflows() == [f"system transfer to {THIEF}"]
    assert (await fourth.health()).status == "compromise_suspected"


async def test_without_the_database_the_alarm_is_raised_anyway(pool: asyncpg.Pool) -> None:
    rpc = CountingRpc()
    rpc.signatures = ["old"]
    rpc.transactions["old"] = sweep()
    x402 = await started(pool, rpc)
    await x402.audit_outflows()

    class BrokenPool:
        def acquire(self) -> Any:
            raise ConnectionRefusedError

    working, x402._pool = x402._pool, BrokenPool()
    rpc.signatures = ["theft", "old"]
    rpc.transactions["theft"] = theft()
    assert await x402.audit_outflows() == [f"system transfer to {THIEF}"]
    assert (await x402.health()).status == "compromise_suspected"
    hot = str(HOT.pubkey())
    assert await pool.fetchval("SELECT signature FROM x402_audit_cursor WHERE hot_wallet = $1", hot) == "old"

    x402._pool = working  # the database is back: the next audit saves the flag and moves the cursor
    assert await x402.audit_outflows() == []
    assert await pool.fetchval("SELECT signature FROM x402_audit_cursor WHERE hot_wallet = $1", hot) == "theft"
    assert await pool.fetchval("SELECT count(*) FROM x402_audit_flags WHERE cleared_at IS NULL") == 1


async def test_the_migration_can_run_again(pool: asyncpg.Pool) -> None:
    [(_, _, sql)] = [m for m in MIGRATIONS if m[0] == 23]
    await pool.execute(sql)
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations WHERE version = 23") == 1


async def test_only_the_operator_sees_which_transactions_raised_the_alarm(
    client: httpx.AsyncClient, pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    rpc = CountingRpc()
    rpc.signatures = ["theft"]
    rpc.transactions["theft"] = theft()
    x402 = await started(pool, rpc)
    await x402.audit_outflows()
    app.x402 = x402
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    operator = {"Authorization": "Bearer operator-token"}

    public = (await client.get("/v1/health/ready")).json()
    assert (public["x402"], public["x402_alert_count"], public["x402_alerts"]) == ("compromise_suspected", 1, [])
    assert "theft" not in str(public) and THIEF not in str(public)
    assert (await client.get("/v1/admin/x402/alerts")).status_code == 401

    alerts = (await client.get("/v1/admin/x402/alerts", headers=operator)).json()
    assert [(f["signature"], f["reason"]) for f in alerts["flagged"]] == [("theft", f"system transfer to {THIEF}")]
    ready = (await client.get("/v1/health/ready", headers=operator)).json()
    assert ready["x402_alerts"] == ["outflow_to_non_cold"]


async def test_a_pass_that_fails_half_way_saves_what_it_found(pool: asyncpg.Pool) -> None:
    class FlakyRpc(CountingRpc):
        async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
            if signature == "flaky":
                raise RpcError("429 Too Many Requests")
            return await super().parsed_transaction(signature, commitment)

    rpc = FlakyRpc()
    rpc.signatures = ["flaky", "theft", "old"]
    rpc.transactions.update(old=sweep(), theft=theft(), flaky=sweep())
    x402 = await started(pool, rpc)
    with pytest.raises(RpcError):
        await x402.audit_outflows()
    restarted = await started(pool, rpc)
    assert restarted._audit_cursor == "theft"
    assert [(f.signature, f.reason) for f in restarted.flagged()] == [("theft", f"system transfer to {THIEF}")]
