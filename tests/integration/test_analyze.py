import asyncio
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from veridex.config import settings

PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater", "flags": ["domain_age"]}


async def test_analyze_returns_score_and_logs(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, create_account: Any
) -> None:
    headers = await create_account("alice")

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"score": 80, "explanation": "Looks legitimate."}
    assert graph.calls == [PAGE]
    assert await pool.fetchval("SELECT url FROM analyze_log WHERE username = 'alice'") == PAGE["url"]


async def test_unanalysed_pages_do_not_count(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, create_account: Any
) -> None:
    headers = await create_account("alice")
    graph.result = {"score": -1, "explanation": "No product referenced on this page"}

    assert (await client.post("/v1/analyze", json=PAGE, headers=headers)).status_code == 200
    assert await pool.fetchval("SELECT COUNT(*) FROM analyze_log") == 0


async def test_free_daily_limit(client: httpx.AsyncClient, create_account: Any) -> None:
    headers = await create_account("alice")
    for _ in range(5):
        assert (await client.post("/v1/analyze", json=PAGE, headers=headers)).status_code == 200

    limited = await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["error_code"] == "TOO_MANY_REQUESTS"


async def test_admin_is_unlimited(client: httpx.AsyncClient, create_account: Any) -> None:
    headers = await create_account("root", status="admin")
    for _ in range(7):
        assert (await client.post("/v1/analyze", json=PAGE, headers=headers)).status_code == 200


async def test_graph_failure_does_not_leak_details(
    client: httpx.AsyncClient, graph: FakeGraph, create_account: Any
) -> None:
    headers = await create_account("alice")
    graph.error = RuntimeError("openai key sk-secret invalid")

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert response.status_code == 500
    assert "sk-secret" not in response.text


async def test_empty_page_rejected(client: httpx.AsyncClient, create_account: Any) -> None:
    headers = await create_account("alice")
    assert (await client.post("/v1/analyze", json={"page_content": ""}, headers=headers)).status_code == 422


async def test_slow_analysis_times_out(
    client: httpx.AsyncClient, graph: FakeGraph, create_account: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await create_account("alice")
    monkeypatch.setattr(settings, "analysis_timeout_s", 0.05)

    async def hang(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setattr(graph, "ainvoke", hang)
    response = await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert response.status_code == 504
    assert response.json()["error_code"] == "GATEWAY_TIMEOUT"


async def test_oversized_page_rejected(client: httpx.AsyncClient, create_account: Any) -> None:
    headers = await create_account("alice")
    huge = {**PAGE, "page_content": "x" * (settings.max_page_bytes + 1)}
    assert (await client.post("/v1/analyze", json=huge, headers=headers)).status_code == 422
    huger = {**PAGE, "page_content": "x" * (settings.max_page_bytes + 200_000)}
    assert (await client.post("/v1/analyze", json=huger, headers=headers)).status_code == 413


async def test_page_facts_are_forwarded_to_the_graph(
    client: httpx.AsyncClient, graph: FakeGraph, create_account: Any
) -> None:
    headers = await create_account("alice")
    body = {**PAGE, "page_facts": {"platform": "shopify", "title": "Sweater", "price": 49.0}}

    assert (await client.post("/v1/analyze", json=body, headers=headers)).status_code == 200
    facts = graph.calls[0]["page_facts"]
    assert (facts.platform, facts.title, facts.price) == ("shopify", "Sweater", 49.0)


async def test_oversized_page_facts_are_rejected(client: httpx.AsyncClient, create_account: Any) -> None:
    headers = await create_account("alice")
    body = {**PAGE, "page_facts": {"variations": ["x"] * 101}}

    assert (await client.post("/v1/analyze", json=body, headers=headers)).status_code == 422


async def test_same_listing_is_served_from_memory(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, create_account: Any
) -> None:
    headers = await create_account("alice")
    again = {**PAGE, "url": f"{PAGE['url']}?utm_source=mail#reviews"}

    first = await client.post("/v1/analyze", json=PAGE, headers=headers)
    second = await client.post("/v1/analyze", json=again, headers=headers)

    assert first.json() == second.json()
    assert len(graph.calls) == 1
    assert await pool.fetchval("SELECT COUNT(*) FROM analyze_log") == 2  # still counts towards the quota


async def test_checkout_pages_bypass_the_cache(
    client: httpx.AsyncClient, graph: FakeGraph, create_account: Any
) -> None:
    headers = await create_account("alice")
    body = {**PAGE, "checkout_content": "<p>Total: €40</p>"}

    await client.post("/v1/analyze", json=body, headers=headers)
    await client.post("/v1/analyze", json=body, headers=headers)

    assert len(graph.calls) == 2
    assert graph.calls[0]["checkout_content"] == "<p>Total: €40</p>"


async def test_stream_sends_findings_then_result(
    client: httpx.AsyncClient, pool: asyncpg.Pool, create_account: Any
) -> None:
    headers = await create_account("alice")

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)

    assert response.headers["content-type"].startswith("text/event-stream")
    events = [block.split("\n") for block in response.text.strip().split("\n\n")]
    names = [lines[0].removeprefix("event: ") for lines in events]
    assert names == ["page_type", "finding", "result"]
    assert '"evidence": "85% off."' in events[1][1]
    assert events[2][1] == 'data: {"score": 80, "explanation": "Looks legitimate."}'
    assert await pool.fetchval("SELECT COUNT(*) FROM analyze_log") == 1


async def test_stream_reports_failure_without_details(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, create_account: Any
) -> None:
    headers = await create_account("alice")
    graph.error = RuntimeError("key sk-secret invalid")

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)

    assert "event: error" in response.text
    assert "sk-secret" not in response.text
    assert await pool.fetchval("SELECT COUNT(*) FROM analyze_log") == 0
