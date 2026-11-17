"""The Base outflow audit's state in Postgres: the same tables as Solana's, keyed <network>:<payTo>."""

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
    assert saved == str(rpc.head - 5)

    rpc.head += 30
    rpc.log_calls.clear()
    second = await started(pool, rpc)
    assert second._cursor == int(saved)
    await second.audit_outflows()
    assert rpc.log_calls == [(int(saved) + 1, rpc.head - 5)]


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
