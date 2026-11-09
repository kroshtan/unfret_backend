import asyncio
import json
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from veridex.config import settings
from veridex.routes import analyze
from veridex.schemas.requests import AnalyzeRequest

PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater", "flags": ["domain_age"]}
PRICE = settings.analysis_price_lamports
DUTCH_VERDICT = {
    "band": "reliable",
    "label": "Ziet er goed uit",
    "headline": "Er viel niets zorgwekkends op aan deze pagina.",
    "reasons": [],
    "positives": [{"type": "nothing_found", "title": "Geen waarschuwingssignalen", "detail": "Niets verdachts."}],
    "language": "nl",
}


async def balance(pool: asyncpg.Pool, address: str) -> int | None:
    value = await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address)
    return None if value is None else int(value)


async def test_analysis_is_paid_from_the_balance(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(3 * PRICE)

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body.pop("report_token")  # lets this account report the result later; nothing is stored now
    assert body == {
        "score": 80,
        "explanation": "Looks legitimate.",
        "balance_usd_micros": 2 * PRICE,
        "balance_lamports": 2 * PRICE,
        "charged": True,
    }
    assert graph.calls == [{**PAGE, "language": "en"}]
    assert await balance(pool, address) == 2 * PRICE


async def test_nothing_about_the_page_is_stored(client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Any) -> None:
    _, headers = await wallet(PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=headers)

    tables = {r["tablename"] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert tables == {
        "wallets",
        "scan_cursor",
        "fact_cache",
        "usage_counters",
        "price_observations",
        "shops",
        "shop_links",
        "api_keys",
        "fiat_payments",
        "fiat_checkouts",
        "reports",
        "report_votes",
        "report_quota",
        "labelled_pages",
        "vat_quotes",
        "crypto_sales",
        "schema_migrations",
    }
    for table in ("vat_quotes", "crypto_sales"):  # only a crypto purchase fills them
        assert await pool.fetchval(f"SELECT COUNT(*) FROM {table}") == 0  # noqa: S608
    for table in ("reports", "report_votes", "report_quota", "labelled_pages"):  # only a consented report fills them
        assert await pool.fetchval(f"SELECT COUNT(*) FROM {table}") == 0  # noqa: S608
    assert await pool.fetchval("SELECT COUNT(*) FROM fact_cache") == 0
    # Price observations are keyed by a product hash and hold no URL; this page has no public product ID anyway.
    assert await pool.fetchval("SELECT COUNT(*) FROM price_observations") == 0
    # Shop fingerprints hold facts about web shops, never the page or the caller (test_storage greps them).


async def test_insufficient_credits(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    _, headers = await wallet(PRICE - 1)

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.status_code == 402
    body = response.json()
    assert body["error_code"] == "INSUFFICIENT_CREDITS"
    assert body["metadata"] == {
        "balance_usd_micros": PRICE - 1,
        "analysis_usd_micros": PRICE,
        "balance_lamports": PRICE - 1,  # deprecated aliases, for extensions up to 0.5.0
        "analysis_lamports": PRICE,
    }
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


async def test_a_different_checkout_page_is_a_new_analysis(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Any
) -> None:
    _, headers = await wallet(2 * PRICE)

    await client.post("/v1/analyze", json={**PAGE, "checkout_content": "<p>Total: €40</p>"}, headers=headers)
    await client.post("/v1/analyze", json={**PAGE, "checkout_content": "<p>Total: €95</p>"}, headers=headers)

    assert len(graph.calls) == 2
    assert graph.calls[0]["checkout_content"] == "<p>Total: €40</p>"


async def test_crafted_input_never_reaches_other_callers(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Any
) -> None:
    # Regression (security audit H1): the cache was keyed on the URL alone, so one caller's crafted "clean" page
    # for a scam shop's URL was served to everyone checking that URL for an hour.
    _, attacker = await wallet(PRICE)
    _, victim = await wallet(PRICE)
    graph.result = {**graph.result, "score": 95}
    await client.post("/v1/analyze", json={**PAGE, "page_content": "<h1>Totally legit</h1>"}, headers=attacker)
    graph.result = {**graph.result, "score": 12}

    response = await client.post("/v1/analyze", json=PAGE, headers=victim)

    assert response.json()["score"] == 12
    assert len(graph.calls) == 2


async def test_product_ids_in_the_query_are_different_listings(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Any
) -> None:
    _, headers = await wallet(2 * PRICE)
    url = "https://shop.example/index.php?route=product/product&product_id="

    await client.post("/v1/analyze", json={**PAGE, "url": f"{url}42"}, headers=headers)
    await client.post("/v1/analyze", json={**PAGE, "url": f"{url}43"}, headers=headers)

    assert len(graph.calls) == 2


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
    assert '"type": "discount"' in events[1][1]
    assert "85% off" not in events[1][1]  # steps are progress only: the evidence comes with the charged result
    result = json.loads(events[2][1].removeprefix("data: "))
    assert result.pop("report_token")
    assert result == {
        "score": 80,
        "explanation": "Looks legitimate.",
        "balance_usd_micros": 0,
        "balance_lamports": 0,
        "charged": True,
    }
    assert await balance(pool, address) == 0


async def test_hanging_up_mid_stream_refunds_and_reveals_no_evidence(
    graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    # Regression (audit M5): the refund in the stream's `finally` ran in a scope the disconnect was cancelling,
    # and the steps carried every finding's evidence: read them, hang up, and the check was free.
    address, _ = await wallet(PRICE)
    request: Any = SimpleNamespace(app=SimpleNamespace(db_pool=pool))
    left = await analyze._debit(request, address)
    body = AnalyzeRequest.model_validate(PAGE)
    events = analyze._live_events(request, body, address, graph, {}, key="k", left=left)

    first = await anext(events)
    second = await anext(events)
    await events.aclose()  # the client went away before the result

    assert "85% off" not in first + second
    assert await balance(pool, address) == PRICE


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
