import httpx
import pytest
from pydantic import SecretStr

from unfret.config import settings
from unfret.main import app


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


async def test_ready_shows_balances_and_alerts_only_to_the_operator(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    public = (await client.get("/v1/health/ready")).json()
    assert public["hot_sol_lamports"] is None and public["x402_alerts"] == []
    assert "x402_alert_count" in public
    operator = await client.get("/v1/health/ready", headers={"Authorization": "Bearer operator-token"})
    assert operator.json()["x402_alert_count"] == len(operator.json()["x402_alerts"])
