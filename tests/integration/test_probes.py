import httpx
import pytest
from pydantic import SecretStr

from unfret import scoring
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


async def test_both_probes_report_the_build_commit(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "git_sha", "0123abcd" * 5)
    assert (await client.get("/v1/health")).json()["commit"] == "0123abcd" * 5
    assert (await client.get("/v1/health/ready")).json()["commit"] == "0123abcd" * 5


async def test_both_probes_report_the_scorers(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = {"features": [], "means": [], "scales": [], "coefficients": [], "intercept": 0}
    full = scoring.Model.from_dict({**empty, "version": "v9-private"})
    quick = scoring.Model.from_dict({**empty, "version": "v9-quick-private"})
    monkeypatch.setattr(
        scoring, "load_model", lambda path=scoring.MODEL_PATH: quick if path == scoring.MODEL_QUICK_PATH else full
    )
    for probe in ("/v1/health", "/v1/health/ready"):
        body = (await client.get(probe)).json()
        assert (body["model_version"], body["quick_model_version"]) == ("v9-private", "v9-quick-private")


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
