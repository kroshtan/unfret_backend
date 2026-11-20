import asyncio
import json
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from unfret.config import settings
from unfret.routes import analyze
from unfret.schemas.requests import AnalyzeRequest
from unfret.visitors import RateLimit

PAGE = {"page_content": "<h1>Sweater</h1>", "url": "https://shop.test/sweater", "flags": ["domain_age"]}
PRICE = settings.analysis_price_lamports


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
    assert body.pop("share_token")  # lets whoever sees it share it later, anonymously
    assert body == {
        "score": 80,
        "explanation": "Looks legitimate.",
        "balance_usd_micros": 2 * PRICE,
        "balance_lamports": 2 * PRICE,
        "charged": True,
        "tier": "deep",
        "charge_note": "This check used one of your checks; 2 are left.",
    }
    assert graph.calls == [PAGE]
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
        "report_opinion_days",  # second-opinion calls per day: a count, nothing about a page
        "labelled_pages",
        "vat_quotes",
        "crypto_sales",
        "image_prints",
        "listing_verdicts",
        "shared_results",
        "invites",
        "market_prices",
        "review_fingerprints",
        "ad_prints",
        "ad_contacts",
        "x402_audit_cursor",  # hot-wallet transactions only (the outflow audit), never a page or a caller
        "x402_audit_flags",
        "affiliates",  # the operator's affiliates and codes; a pass, never a page or a check
        "bonus_codes",
        "affiliate_passes",
        "affiliate_earnings",
        "affiliate_reversals",
        "affiliate_payouts",
        "objections",  # what a shop owner sends from /objection, never a check
        "schema_migrations",
    }
    for table in ("vat_quotes", "crypto_sales", "invites", "affiliate_passes", "affiliate_earnings", "objections"):
        assert await pool.fetchval(f"SELECT COUNT(*) FROM {table}") == 0  # noqa: S608
    assert await pool.fetchval("SELECT COUNT(*) FROM shared_results") == 0  # only a confirmed share adds one
    for table in ("reports", "report_votes", "report_quota", "labelled_pages", "report_opinion_days"):
        assert await pool.fetchval(f"SELECT COUNT(*) FROM {table}") == 0  # noqa: S608
    assert await pool.fetchval("SELECT COUNT(*) FROM fact_cache") == 0
    # Price observations are keyed by a product hash and hold no URL; this page has no public product ID anyway.
    assert await pool.fetchval("SELECT COUNT(*) FROM price_observations") == 0
    assert await pool.fetchval("SELECT COUNT(*) FROM market_prices") == 0  # no barcode either
    assert await pool.fetchval("SELECT COUNT(*) FROM review_fingerprints") == 0  # hashes only, and no reviews here
    # Shop fingerprints hold facts about web shops, never the page or the caller (test_storage greps them).
    # Ad fingerprints are hashes of peer-to-peer ads only (a text hash, a listing hash, keyed contact hashes).
    for table in ("ad_prints", "ad_contacts"):
        assert await pool.fetchval(f"SELECT COUNT(*) FROM {table}") == 0  # noqa: S608


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
    assert response.json()["charge_note"] == "This page couldn't be assessed, so no check was used."
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
    assert (await client.post("/v1/analyze", json={"url": " "}, headers=headers)).status_code == 422
    assert (await client.post("/v1/analyze", json={}, headers=headers)).status_code == 422
    # A URL alone is enough: the server fetches it (tests/integration/test_url_only.py).


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


async def test_the_same_accounts_repeat_needs_no_balance_and_streams_at_once(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    address, headers = await wallet(PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert await balance(pool, address) == 0

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=headers)  # nothing left, paid already

    events = [block.split("\n") for block in response.text.strip().split("\n\n")]
    assert [e[0] for e in events] == ["event: result"]
    assert '"charged": false' in events[0][1]
    assert len(graph.calls) == 1


async def test_a_cached_result_costs_another_account_a_check(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    # Security audit M6: a result someone else paid for within the hour was served to anyone, free of charge.
    _, first = await wallet(PRICE)
    address, other = await wallet(2 * PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=first)

    cached = await client.post("/v1/analyze", json=PAGE, headers=other)
    repeat = await client.post("/v1/analyze/stream", json=PAGE, headers=other)

    assert cached.status_code == 200
    body = cached.json()
    assert (body["score"], body["charged"], body["balance_usd_micros"]) == (80, True, PRICE)
    assert body["report_token"] and body["share_token"]
    assert '"charged": false' in repeat.text  # its own repeat: paid once
    assert len(graph.calls) == 1  # served from the cache, not analysed again
    assert await balance(pool, address) == PRICE


async def test_a_cached_result_streams_at_once_and_charged(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    _, first = await wallet(PRICE)
    address, other = await wallet(PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=first)

    response = await client.post("/v1/analyze/stream", json=PAGE, headers=other)

    events = [block.split("\n") for block in response.text.strip().split("\n\n")]
    assert [e[0] for e in events] == ["event: result"]
    assert '"charged": true' in events[0][1]
    assert len(graph.calls) == 1
    assert await balance(pool, address) == 0


@pytest.mark.parametrize("path", ["/v1/analyze", "/v1/analyze/stream"])
async def test_an_account_without_checks_cant_tell_a_page_was_checked(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Any, path: str
) -> None:
    # Security audit M6: a 200 from the cache (vs a 402) told an empty account that someone checked the page lately.
    _, first = await wallet(PRICE)
    await client.post("/v1/analyze", json=PAGE, headers=first)
    _, empty = await wallet(0)

    cached = await client.post(path, json=PAGE, headers=empty)
    unchecked = await client.post(path, json={**PAGE, "url": "https://shop.test/never-checked"}, headers=empty)

    assert cached.status_code == unchecked.status_code == 402
    assert cached.json() == unchecked.json()
    assert len(graph.calls) == 1


async def test_a_cached_page_that_couldnt_be_assessed_is_free_but_needs_a_check(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    graph.result = {"score": -1, "explanation": "Not a product page.", "skill_results": []}
    _, first = await wallet(PRICE)
    address, other = await wallet(PRICE)
    _, empty = await wallet(0)
    await client.post("/v1/analyze", json=PAGE, headers=first)

    cached = await client.post("/v1/analyze", json=PAGE, headers=other)
    refused = await client.post("/v1/analyze", json=PAGE, headers=empty)

    assert (cached.json()["score"], cached.json()["charged"]) == (-1, False)
    assert await balance(pool, address) == PRICE
    assert refused.status_code == 402
    assert len(graph.calls) == 1


async def test_accept_language_changes_nothing(client: httpx.AsyncClient, graph: FakeGraph, wallet: Any) -> None:
    # The app is in English: the header picks no language, and the same page in another one is the same analysis.
    _, headers = await wallet(3 * PRICE)

    dutch = await client.post("/v1/analyze", json=PAGE, headers={**headers, "Accept-Language": "nl-NL,nl;q=0.9"})
    english = await client.post("/v1/analyze", json=PAGE, headers={**headers, "Accept-Language": "en-GB,en"})

    assert graph.calls == [PAGE]  # one analysis, no language in its input; the second is the cached result
    assert (dutch.json()["charged"], english.json()["charged"]) == (True, False)


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


async def test_the_response_says_what_kind_of_page_was_checked(
    client: httpx.AsyncClient, graph: Any, wallet: Any
) -> None:
    _, headers = await wallet(PRICE)
    graph.result = {**graph.result, "page_type": "p2p_listing"}

    response = await client.post("/v1/analyze", json=PAGE, headers=headers)

    assert response.json()["page_type"] == "p2p_listing"


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
    assert result.pop("report_token") and result.pop("share_token")
    assert result == {
        "score": 80,
        "explanation": "Looks legitimate.",
        "page_type": "storefront",  # as the classify step said
        "balance_usd_micros": 0,
        "balance_lamports": 0,
        "charged": True,
        "tier": "deep",
        "charge_note": "This check used one of your checks; none are left.",
    }
    assert await balance(pool, address) == 0


async def test_hanging_up_mid_stream_refunds_and_reveals_no_evidence(
    graph: FakeGraph, pool: asyncpg.Pool, wallet: Any
) -> None:
    # Regression (audit M5): the refund in the stream's `finally` ran in a scope the disconnect was cancelling,
    # and the steps carried every finding's evidence: read them, hang up, and the check was free.
    address, _ = await wallet(PRICE)
    request: Any = SimpleNamespace(app=SimpleNamespace(db_pool=pool))
    charge = await analyze._debit(request, address)
    body = AnalyzeRequest.model_validate(PAGE)
    events = analyze._live_events(request, body, charge, graph, {}, key="k")

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


@pytest.mark.parametrize("path", ["/v1/analyze", "/v1/analyze/stream"])
async def test_uncharged_analyses_are_limited_per_account(
    *,
    client: httpx.AsyncClient,
    graph: FakeGraph,
    pool: asyncpg.Pool,
    wallet: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    # A page that can't be assessed is refunded, but its analysis still cost LLM calls: one check's balance must not
    # buy an endless run of them.
    monkeypatch.setattr(analyze, "_free_outcomes", RateLimit(2, window_s=3600))
    address, headers = await wallet(PRICE)
    graph.result = {"score": -1, "explanation": "No product referenced on this page"}

    for n in range(2):
        assert (
            await client.post(path, json={**PAGE, "url": f"https://shop.test/{n}"}, headers=headers)
        ).status_code == 200
    refused = await client.post(path, json={**PAGE, "url": "https://shop.test/2"}, headers=headers)

    assert (refused.status_code, refused.json()["error_code"]) == (429, "TOO_MANY_REQUESTS")
    assert len(graph.calls) == 2
    assert await balance(pool, address) == PRICE


async def test_a_hang_up_counts_as_an_uncharged_analysis(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyze, "_free_outcomes", RateLimit(1, window_s=3600))
    address, headers = await wallet(PRICE)
    graph.error = asyncio.CancelledError()  # type: ignore[assignment]  # the client went away mid-analysis

    with pytest.raises(asyncio.CancelledError):
        await client.post("/v1/analyze", json=PAGE, headers=headers)
    assert await balance(pool, address) == PRICE  # refunded, as before
    graph.error = None
    refused = await client.post("/v1/analyze", json={**PAGE, "url": "https://shop.test/again"}, headers=headers)
    assert (refused.status_code, refused.json()["error_code"]) == (429, "TOO_MANY_REQUESTS")


async def test_our_own_failures_do_not_count_against_the_account(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyze, "_free_outcomes", RateLimit(1, window_s=3600))
    _, headers = await wallet(PRICE)
    graph.error = RuntimeError("provider down")
    assert (await client.post("/v1/analyze", json=PAGE, headers=headers)).status_code == 500
    stream = await client.post("/v1/analyze/stream", json={**PAGE, "url": "https://shop.test/s"}, headers=headers)
    assert "INTERNAL_ERROR" in stream.text
    graph.error = None
    assert (
        await client.post("/v1/analyze", json={**PAGE, "url": "https://shop.test/ok"}, headers=headers)
    ).status_code == 200
