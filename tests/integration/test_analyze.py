import asyncio
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from veridex.config import settings

PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater", "flags": ["domain_age"]}
PRICE = settings.analysis_price_lamports
DUTCH_VERDICT = {
    "band": "reliable",
    "label": "Ziet er betrouwbaar uit",
    "headline": "Er viel niets zorgwekkends op aan deze pagina.",
    "reasons": [],
    "positives": [{"type": "nothing_found", "title": "Geen waarschuwingssignalen", "detail": "Niets verdachts."}],
    "language": "nl",
}


async def balance(pool: asyncpg.Pool, address: str) -> int | None:
    value = await pool.fetchval("SELECT balance_lamports FROM wallets WHERE pubkey = $1", address)
    return None if value is None else int(value)


async def test_analysis_is_paid_from_the_balance(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(3 * PRICE)

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "score": 80,
        "explanation": "Looks legitimate.",
        "balance_lamports": 2 * PRICE,
        "charged": True,
    }
    assert graph.calls == [{**PAGE, "language": "en"}]
    assert await balance(pool, address) == 2 * PRICE


async def test_nothing_about_the_page_is_stored(client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=headers)

    tables = {r["tablename"] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert tables == {"wallets", "scan_cursor", "fact_cache", "usage_counters", "schema_migrations"}
    assert await pool.fetchval("SELECT COUNT(*) FROM fact_cache") == 0


async def test_insufficient_credits(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    _, headers = await wallet(PRICE - 1)

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 402
    body = response.json()
    assert body["error_code"] == "INSUFFICIENT_CREDITS"
    assert body["metadata"] == {"balance_lamports": PRICE - 1, "analysis_lamports": PRICE}
    assert graph.calls == []


async def test_unknown_wallet_has_no_credits(client: httpx.AsyncClient, wallet: Any) -> None:
    _, headers = await wallet(0)
    response = await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert response.status_code == 402
    assert response.json()["metadata"]["balance_lamports"] == 0


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic YTpi"}])
async def test_sign_in_required(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_concurrent_analyses_never_overspend(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(3 * PRICE)

    async def slow(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return graph.result

    graph.ainvoke = slow  # type: ignore[method-assign]
    bodies = [{**PAGE, "url": f"https://shop.test/{i}"} for i in range(10)]  # distinct, so no cache hits
    responses = await asyncio.gather(*(client.post("/v1/analyze", json=b, headers=headers) for b in bodies))

    assert sorted(r.status_code for r in responses) == [200] * 3 + [402] * 7
    assert await balance(pool, address) == 0


async def test_unanalysable_pages_are_refunded(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(PRICE)
    graph.result = {"score": -1, "explanation": "No product referenced on this page"}

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 200
    assert response.json()["balance_lamports"] == PRICE
    assert await balance(pool, address) == PRICE


async def test_graph_failure_is_refunded_and_leaks_nothing(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(PRICE)
    graph.error = RuntimeError("openai key sk-secret invalid")

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 500
    assert "sk-secret" not in response.text
    assert await balance(pool, address) == PRICE


async def test_slow_analysis_times_out_and_is_refunded(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    address, headers = await wallet(PRICE)
    monkeypatch.setattr(settings, "analysis_timeout_s", 0.05)

    async def hang(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    monkeypatch.setattr(graph, "ainvoke", hang)
    response = await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert response.status_code == 504
    assert response.json()["error_code"] == "GATEWAY_TIMEOUT"
    assert await balance(pool, address) == PRICE


async def test_a_page_is_required(client: httpx.AsyncClient, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    assert (await client.post("/v1/analyze", json={"page_content": ""}, headers=headers)).status_code == 422
    assert (await client.post("/v1/analyze", json={"url": "https://a.test"}, headers=headers)).status_code == 422


async def test_page_facts_alone_are_enough(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    body = {"url": "https://shop.test/x", "page_facts": {"platform": "amazon", "title": "Earbuds"}}

    assert (await client.post("/v1/analyze", json=body, headers=headers)).status_code == 200
    assert graph.calls[0]["page_content"] == ""
    assert graph.calls[0]["page_facts"].title == "Earbuds"


async def test_oversized_page_rejected(client: httpx.AsyncClient, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    huge = {**PAGE, "page_content": "x" * (settings.max_page_bytes + 1)}
    assert (await client.post("/v1/analyze", json=huge, headers=headers)).status_code == 422
    huger = {**PAGE, "page_content": "x" * (settings.max_page_bytes + 200_000)}
    assert (await client.post("/v1/analyze", json=huger, headers=headers)).status_code == 413


async def test_page_facts_are_forwarded_to_the_graph(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    body = {**PAGE, "page_facts": {"platform": "shopify", "title": "Sweater", "price": 49.0}}

    assert (await client.post("/v1/analyze", json=body, headers=headers)).status_code == 200
    facts = graph.calls[0]["page_facts"]
    assert (facts.platform, facts.title, facts.price) == ("shopify", "Sweater", 49.0)


async def test_oversized_page_facts_are_rejected(client: httpx.AsyncClient, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    body = {**PAGE, "page_facts": {"variations": ["x"] * 101}}

    assert (await client.post("/v1/analyze", json=body, headers=headers)).status_code == 422


async def test_same_listing_is_served_from_memory_for_free(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(2 * PRICE)
    again = {**PAGE, "url": f"{PAGE['url']}?utm_source=mail#reviews"}

    first = await client.post("/v1/analyze", json=PAGE, headers=headers)
    second = await client.post("/v1/analyze", json=again, headers=headers)

    assert first.json()["score"] == second.json()["score"] == 80
    assert (first.json()["charged"], second.json()["charged"]) == (True, False)
    assert second.json()["balance_lamports"] == PRICE
    assert len(graph.calls) == 1
    assert await balance(pool, address) == PRICE  # one check used, not two


async def test_cache_hit_needs_no_balance_and_streams_at_once(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert await balance(pool, address) == 0

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)  # nothing left, still served

    events = [block.split("\n") for block in response.text.strip().split("\n\n")]
    assert [e[0] for e in events] == ["event: result"]
    assert '"charged": false' in events[0][1]
    assert len(graph.calls) == 1


async def test_language_follows_accept_language_and_has_its_own_cache_entry(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Any
) -> None:
    _, headers = await wallet(3 * PRICE)
    graph.result = {**graph.result, "verdict": DUTCH_VERDICT}

    dutch = await client.post("/v1/analyze", json=PAGE, headers={**headers, "Accept-Language": "nl-NL,nl;q=0.9"})
    await client.post("/v1/analyze", json=PAGE, headers={**headers, "Accept-Language": "en-GB,en"})

    assert [c["language"] for c in graph.calls] == ["nl", "en"]  # a different language is a new analysis
    assert dutch.json()["verdict"] == DUTCH_VERDICT


async def test_checkout_pages_bypass_the_cache(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    _, headers = await wallet(2 * PRICE)
    body = {**PAGE, "checkout_content": "<p>Total: €40</p>"}

    await client.post("/v1/analyze", json=body, headers=headers)
    await client.post("/v1/analyze", json=body, headers=headers)

    assert len(graph.calls) == 2
    assert graph.calls[0]["checkout_content"] == "<p>Total: €40</p>"


async def test_assessment_is_returned(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    assessment = {"risk_score": 0.2, "confidence": 0.6, "flags": [], "model_version": "v1-test"}
    graph.result = {**graph.result, "assessment": assessment}

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.json()["assessment"] == assessment


async def test_stream_sends_findings_then_result(client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Any) -> None:
    address, headers = await wallet(PRICE)

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)

    assert response.headers["content-type"].startswith("text/event-stream")
    events = [block.split("\n") for block in response.text.strip().split("\n\n")]
    names = [lines[0].removeprefix("event: ") for lines in events]
    assert names == ["step", "step", "result"]
    assert events[0][1] == 'data: {"skill": "classify", "findings": [], "page_type": "storefront"}'
    assert events[1][1].startswith('data: {"skill": "listing", "findings": [{')
    assert '"evidence": "85% off."' in events[1][1]
    assert events[2][1] == (
        'data: {"score": 80, "explanation": "Looks legitimate.", "balance_lamports": 0, "charged": true}'
    )
    assert await balance(pool, address) == 0


async def test_stream_needs_credits(client: httpx.AsyncClient, wallet: Any) -> None:
    _, headers = await wallet(0)
    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)
    assert response.status_code == 402


async def test_stream_failure_is_refunded_without_details(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(PRICE)
    graph.error = RuntimeError("key sk-secret invalid")

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)

    assert "event: error" in response.text
    assert "sk-secret" not in response.text
    assert await balance(pool, address) == PRICE
