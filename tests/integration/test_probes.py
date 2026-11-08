import httpx
import pytest

from veridex.main import app


async def test_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


async def test_ready_when_database_answers(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_not_ready_when_database_fails(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenPool:
        async def fetchval(self, query: str) -> int:
            raise ConnectionRefusedError

    monkeypatch.setattr(app, "db_pool", BrokenPool())
    response = await client.get("/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["error_code"] == "SERVICE_UNAVAILABLE"
