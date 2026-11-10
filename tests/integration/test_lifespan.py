"""The app's lifespan wires what production runs: both graphs, the Postgres stores, x402, the scanners, Polar, jobs."""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair
from structlog.testing import capture_logs

from tests.integration.conftest import POSTGRES_URI
from tests.unit import test_polar
from unfret import main
from unfret.api import UnfretAPI
from unfret.config import Settings
from unfret.credits import CreditsDoor
from unfret.credits.polar import PolarProvider
from unfret.db import PostgresFactStore
from unfret.graph import SKILLS
from unfret.image_store import PostgresImageStore
from unfret.migrations import MIGRATIONS
from unfret.price_store import PostgresPriceStore
from unfret.shop_store import PostgresShopStore
from unfret.verdict_store import PostgresVerdictStore
from unfret.x402.service import X402
from unfret.x402.verdict import QUICK_SKILLS

STORES = {
    "unfret.fact_cache": PostgresFactStore,
    "unfret.price_store": PostgresPriceStore,
    "unfret.shop_store": PostgresShopStore,
    "unfret.graph.skills.reverse_image": PostgresImageStore,
    "unfret.verdict_store": PostgresVerdictStore,
}


def production_like() -> Settings:
    """A hot wallet and cold address (x402 and crypto purchases), sign-in, and Polar (card top-ups)."""
    return test_polar.settings(
        hot_wallet_key=SecretStr(str(Keypair())),
        cold_address=str(Keypair().pubkey()),
        session_secret=SecretStr("lifespan-secret"),
    )


@pytest.fixture
def started(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Stub the outside world: the pool is the test database's, and the background jobs only say they started.

    :return: The background jobs started, in order (a job removes itself once cancelled).
    """
    jobs: list[str] = []

    def job(name: str) -> Any:
        async def run(*_: Any, **__: Any) -> None:
            jobs.append(name)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                jobs.append(f"{name} cancelled")
                raise

        return run

    async def create_pool() -> asyncpg.Pool:
        return await asyncpg.create_pool(POSTGRES_URI)

    monkeypatch.setattr(main, "create_pool", create_pool)
    monkeypatch.setattr(X402, "run_forever", job("x402"))
    monkeypatch.setattr(CreditsDoor, "run_forever", job("credits"))
    monkeypatch.setattr(main, "housekeeping", job("housekeeping"))
    for module in STORES:  # put the in-memory stores back afterwards
        monkeypatch.setattr(sys.modules[module], "_store", sys.modules[module]._store)
    return jobs


def nodes(graph: Any) -> set[str]:
    return set(graph.nodes)


async def test_lifespan_wires_a_configured_service(
    started: list[str], pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "settings", production_like())
    api = UnfretAPI()

    async with main.lifespan(api):
        await asyncio.sleep(0)  # let the background jobs start
        assert api.db_pool is not None
        every_skill = {s.name for s in SKILLS}
        assert every_skill <= nodes(api.graph)  # the deep tier and /v1/analyze: every skill
        assert nodes(api.quick_graph) & every_skill == QUICK_SKILLS  # the x402 quick tier: its skills only
        assert {"content", "reviews", "reverse_image", "domain_age"}.isdisjoint(nodes(api.quick_graph))
        assert await api.db_pool.fetchval("SELECT COUNT(*) FROM schema_migrations") == len(MIGRATIONS)
        for module, store in STORES.items():
            assert isinstance(sys.modules[module]._store, store), module
        assert api.x402.configured
        assert api.credits.hot == api.x402.hot.pubkey  # purchases are paid to the hot wallet
        assert api.credits.scanner is not None and api.credits.usdc_scanner is not None
        assert isinstance(api.fiat, PolarProvider)
        assert sorted(started) == ["credits", "housekeeping", "x402"]
        db_pool, fiat, rpc = api.db_pool, api.fiat, api.x402.rpc

    assert sorted(started) == [
        "credits",
        "credits cancelled",
        "housekeeping",
        "housekeeping cancelled",
        "x402",
        "x402 cancelled",
    ]
    assert db_pool.is_closing()
    assert fiat.http.is_closed
    assert rpc._client is None or rpc._client.is_closed


async def test_lifespan_without_payments_runs_only_housekeeping(
    started: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "settings", Settings(_env_file=None))  # type: ignore[call-arg]
    monkeypatch.setattr(main.vat, "rates_checked_on", lambda: datetime.now(UTC).date() - timedelta(days=46))
    api = UnfretAPI()

    with capture_logs() as logs:
        async with main.lifespan(api):
            await asyncio.sleep(0)
            assert not api.x402.configured
            assert api.credits.scanner is None
            assert api.fiat is None
            assert started == ["housekeeping"]

    assert started == ["housekeeping", "housekeeping cancelled"]
    assert {"event": "vat_rates_stale", "checked_days_ago": 46, "log_level": "warning"} in logs


async def test_housekeeping_runs_every_job_even_when_one_fails(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    async def failing(*_: Any) -> None:
        ran.append("reports")
        raise RuntimeError("boom")

    async def fine(name: str) -> None:
        ran.append(name)

    monkeypatch.setattr(main.reports, "expire", failing)
    monkeypatch.setattr(main.fiat, "purge", lambda *_: fine("fiat"))
    monkeypatch.setattr(main.vat, "purge", lambda *_: fine("vat"))

    task = asyncio.create_task(main.housekeeping(pool, interval_s=0))
    while len(ran) < 6:  # two rounds
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ran[:6] == ["reports", "fiat", "vat"] * 2
