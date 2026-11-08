import base64
import os
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest

from veridex.auth import hash_password
from veridex.db import init_db
from veridex.main import app

POSTGRES_URI = os.getenv("VERIDEX_TEST_POSTGRES_URI")


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


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """Fresh schema per test. Integration tests are skipped when no test database is configured."""
    if not POSTGRES_URI:
        pytest.skip("VERIDEX_TEST_POSTGRES_URI not set")
    pool = await asyncpg.create_pool(POSTGRES_URI)
    await pool.execute("DROP TABLE IF EXISTS analyze_log, accounts")
    await init_db(pool)
    yield pool
    await pool.close()


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
async def client(pool: asyncpg.Pool, graph: FakeGraph) -> AsyncIterator[httpx.AsyncClient]:
    # Bypass the lifespan (which builds the real graph) and inject test doubles.
    app.db_pool = pool
    app.graph = graph  # type: ignore[assignment]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def create_account(pool: asyncpg.Pool) -> Any:
    async def _create(username: str, status: str = "free", password: str = "password123") -> dict[str, str]:
        await pool.execute(
            "INSERT INTO accounts (username, hashed_password, contact_email, subscription_status) "
            "VALUES ($1, $2, $3, $4)",
            username,
            await hash_password(password),
            f"{username}@example.com",
            status,
        )
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    return _create
