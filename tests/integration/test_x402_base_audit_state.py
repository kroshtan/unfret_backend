"""
The Base hot wallet's state in Postgres: the audit's cursor and flags (the same tables as Solana's, keyed
<network>:<hot wallet>), and the sweep held by one process at a time.
"""

import asyncpg
from eth_account import Account

from tests.unit.x402_base_helpers import (
    NETWORK,
    PAY_TO,
    FakeBaseRpc,
    FakeFacilitator,
    base_settings,
    make_base,
    outflow_log,
)
from unfret.x402.base import BaseX402
from unfret.x402.evm import TRANSFER_TOPIC

THIEF = Account.create().address
KEY = f"{NETWORK}:{PAY_TO.address.lower()}"


async def started(pool: asyncpg.Pool, rpc: FakeBaseRpc) -> BaseX402:
    """A process starting: a new service, attached to the database."""
    base = make_base(base_settings(), rpc, FakeFacilitator())
    await base.attach(pool)
    return base


async def test_a_restart_continues_from_the_saved_block(pool: asyncpg.Pool) -> None:
    rpc = FakeBaseRpc()
    first = await started(pool, rpc)
    await first.audit_outflows()
    saved = await pool.fetchval("SELECT signature FROM x402_audit_cursor WHERE hot_wallet = $1", KEY)
    assert saved == f"{rpc.head - 5}:0"  # the block, and the hot wallet's nonce at it

    rpc.head += 30
    rpc.log_calls.clear()
    second = await started(pool, rpc)
    assert (second._cursor, second._nonce) == (rpc.head - 35, 0)
    await second.audit_outflows()
    assert rpc.log_calls == [(rpc.head - 34, rpc.head - 5)]


async def test_after_a_restart_the_nonce_is_still_accounted_for(pool: asyncpg.Pool) -> None:
    rpc = FakeBaseRpc()
    first = await started(pool, rpc)
    await first.audit_outflows()
    rpc.head += 10
    rpc.balance = 5_000_000
    await first.sweep_once()  # a sweep: the nonce moves, and its log explains it
    rpc.nonce += 1  # and one transaction it didn't make
    second = await started(pool, rpc)
    rpc.head += 10
    [issue] = await second.audit_outflows()
    assert "signed 1 transaction(s)" in issue


async def test_a_cursor_saved_before_the_nonce_was_kept_is_read(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "INSERT INTO x402_audit_cursor (hot_wallet, signature) VALUES ($1, $2)", KEY, str(FakeBaseRpc().head - 20)
    )
    rpc = FakeBaseRpc()
    rpc.nonce = 4
    base = await started(pool, rpc)
    assert (base._cursor, base._nonce) == (rpc.head - 20, None)
    assert await base.audit_outflows() == []  # no baseline yet: this audit sets it
    assert base._nonce == 4


async def test_one_sweep_at_a_time_across_processes(pool: asyncpg.Pool) -> None:
    rpc = FakeBaseRpc()
    rpc.balance = 5_000_000
    base = await started(pool, rpc)
    async with pool.acquire() as other:  # another process holds the sweep
        assert await other.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", f"x402_base_sweep:{KEY}")
        assert await base.sweep_once() is None
        assert rpc.sent == []
        await other.execute("SELECT pg_advisory_unlock(hashtext($1))", f"x402_base_sweep:{KEY}")
    assert await base.sweep_once() is not None
    assert len(rpc.sent) == 1


async def test_an_alarm_survives_a_restart_until_cleared(pool: asyncpg.Pool) -> None:
    rpc = FakeBaseRpc()
    base = await started(pool, rpc)
    await base.audit_outflows()
    rpc.head += 10
    rpc.logs_by_block[rpc.head - 6] = [outflow_log(TRANSFER_TOPIC, PAY_TO.address, THIEF, 3, tx="0x" + "77" * 32)]
    assert len(await base.audit_outflows()) == 1

    restarted = await started(pool, rpc)
    assert (await restarted.health()).status == "compromise_suspected"
    assert [flag.signature for flag in restarted.flagged()] == ["0x" + "77" * 32]

    await restarted.clear_alerts()
    again = await started(pool, rpc)
    assert (await again.health()).status == "ok"
    # Audited again (a lost cursor, say): the cleared transaction doesn't alarm a second time.
    again._cursor = None
    rpc.head += 1
    assert await again.audit_outflows() == []


async def test_solana_and_base_flags_dont_mix(pool: asyncpg.Pool) -> None:
    rpc = FakeBaseRpc()
    base = await started(pool, rpc)
    await base.audit_outflows()
    rpc.head += 10
    rpc.logs_by_block[rpc.head - 6] = [outflow_log(TRANSFER_TOPIC, PAY_TO.address, THIEF, 3)]
    await base.audit_outflows()
    keys = {row["hot_wallet"] for row in await pool.fetch("SELECT hot_wallet FROM x402_audit_flags")}
    assert keys == {KEY}
