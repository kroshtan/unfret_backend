import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from tests.conftest import unavailable
from unfret import result_cache
from unfret.config import settings
from unfret.credits import siws
from unfret.db import init_db
from unfret.graph.state import Finding
from unfret.main import app

POSTGRES_URI = os.getenv("UNFRET_TEST_POSTGRES_URI")
SESSION_SECRET = "test-session-secret"
# Every table any migration creates (plus the pre-migration ones), dropped before each test.
DROP_ALL = (
    "DROP TABLE IF EXISTS analyze_log, accounts, wallets, scan_cursor, fact_cache, usage_counters, "
    "price_observations, shop_links, shops, api_keys, fiat_payments, fiat_checkouts, report_votes, "
    "report_quota, reports, labelled_pages, vat_quotes, crypto_sales, image_prints, listing_verdicts, "
    "showcase_cases, shared_results, invites, schema_migrations CASCADE"
)


class FakeGraph:
    """Stand-in for the compiled LangGraph so API tests never call an LLM."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = {"score": 80, "explanation": "Looks legitimate.", "skill_results": []}
        self.error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(state)
        if self.error:
            raise self.error
        return self.result

    async def astream(
        self, state: dict[str, Any], config: dict[str, Any] | None = None, stream_mode: str = "updates"
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(state)
        yield {"classify": {"page_type": "storefront"}}
        yield {"listing": {"findings": [Finding(skill="listing", type="discount", evidence="85% off.")]}}
        if self.error:
            raise self.error
        yield {"judge": {"score": self.result["score"], "explanation": self.result["explanation"]}}


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """Fresh schema per test. Without a test database integration tests are skipped (and fail in CI)."""
    if not POSTGRES_URI:
        unavailable("UNFRET_TEST_POSTGRES_URI not set")
    pool = await asyncpg.create_pool(POSTGRES_URI)
    await pool.execute(DROP_ALL)
    await init_db(pool)
    yield pool
    await pool.close()


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
async def client(
    pool: asyncpg.Pool, graph: FakeGraph, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    # Bypass the lifespan (which builds the real graph) and inject test doubles; sign-in on, purchases off. The
    # app's attributes are put back after the test (tests/conftest.py, restore_app_state).
    monkeypatch.setattr(settings, "session_secret", SecretStr(SESSION_SECRET))
    app.db_pool = pool
    app.graph = graph  # type: ignore[assignment]
    app.credits = None
    app.x402 = None
    result_cache.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


def bearer(address: str) -> dict[str, str]:
    token, _ = siws.issue_token(SESSION_SECRET, address, 3600)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def wallet(pool: asyncpg.Pool) -> Callable[[int], Awaitable[tuple[str, dict[str, str]]]]:
    """A signed-in wallet with ``lamports`` of credits: its address and auth headers."""

    async def _create(lamports: int = 0) -> tuple[str, dict[str, str]]:
        address = str(Keypair().pubkey())
        if lamports:
            await pool.execute("INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2)", address, lamports)
        return address, bearer(address)

    return _create
