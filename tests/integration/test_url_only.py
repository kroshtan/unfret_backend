"""
Checks that send only a URL (the web link checker): the server fetches the page before charging anything.

A page it can't read (an error, a shop refusing automatic visitors, a bot check, an empty shell) is a free 422 and
never reaches the analysis; nothing about the URL is logged.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
import httpx
import pytest
from structlog.testing import capture_logs

from tests.integration.conftest import FakeGraph
from unfret import page_fetch
from unfret.config import settings
from unfret.net import UnsafeURLError
from unfret.routes import analyze

PRICE = settings.analysis_price_usd_micros
URL = "https://secret-shop.test/products/wool-sweater?ref=share"
PRODUCT_HTML = (
    "<html><head><title>Wool sweater</title></head><body><main><h1>Wool sweater</h1>"
    + "<p>A warm sweater knitted from merino wool, made in Portugal. Free returns within 30 days.</p>" * 8
    + "</main></body></html>"
)
Wallet = Callable[[int], Awaitable[tuple[str, dict[str, str]]]]


async def balance(pool: asyncpg.Pool, address: str) -> int | None:
    value = await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address)
    return None if value is None else int(value)


def status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", URL)
    return httpx.HTTPStatusError("refused", request=request, response=httpx.Response(code, request=request))


@pytest.fixture
def shop(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The shop's answer to the server's fetch: ``body`` (bytes) or ``error``; ``calls`` counts the fetches."""
    answer: dict[str, Any] = {"body": PRODUCT_HTML.encode(), "error": None, "calls": []}

    async def fake_fetch(url: str, **_: Any) -> bytes:
        answer["calls"].append(url)
        if answer["error"] is not None:
            raise answer["error"]
        body: bytes = answer["body"]
        return body

    monkeypatch.setattr(page_fetch, "fetch_public", fake_fetch)
    monkeypatch.setattr(analyze, "_fetch_rate", analyze.RateLimit(1000))
    return answer


async def test_a_url_alone_is_fetched_analysed_and_charged(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Wallet, shop: dict[str, Any]
) -> None:
    address, headers = await wallet(2 * PRICE)

    response = await client.post("/v1/analyze", json={"url": URL}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["charged"] is True and body["report_token"]
    assert shop["calls"] == [URL]
    assert graph.calls[0]["page_content"] == PRODUCT_HTML and graph.calls[0]["url"] == URL
    assert await balance(pool, address) == PRICE

    again = await client.post("/v1/analyze", json={"url": URL}, headers=headers)  # cached: no fetch, no charge
    assert again.json()["charged"] is False
    assert len(shop["calls"]) == 1
    assert await balance(pool, address) == PRICE


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ({"error": status_error(403)}, "PAGE_BLOCKED"),
        ({"error": status_error(429)}, "PAGE_BLOCKED"),
        ({"error": status_error(503)}, "PAGE_BLOCKED"),
        ({"error": status_error(404)}, "PAGE_UNAVAILABLE"),
        ({"error": httpx.ConnectError("no route")}, "PAGE_UNAVAILABLE"),
        ({"error": TimeoutError()}, "PAGE_UNAVAILABLE"),
        ({"error": UnsafeURLError("private address")}, "INVALID_URL"),
        (
            {"body": b"<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>"},
            "PAGE_BLOCKED",
        ),
        ({"body": b"<html><body><div id='root'></div><script src='/app.js'></script></body></html>"}, "PAGE_TOO_THIN"),
    ],
)
@pytest.mark.parametrize("path", ["/v1/analyze", "/v1/analyze/stream"])
async def test_a_page_that_cant_be_read_costs_nothing(  # noqa: PLR0917 - fixtures and parameters
    client: httpx.AsyncClient,
    graph: FakeGraph,
    pool: asyncpg.Pool,
    wallet: Wallet,
    shop: dict[str, Any],
    answer: dict[str, Any],
    code: str,
    path: str,
) -> None:
    address, headers = await wallet(PRICE)
    shop.update(answer)

    with capture_logs() as logs:
        response = await client.post(path, json={"url": URL}, headers=headers)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")  # a plain error, not a stream
    assert response.json()["error_code"] == code
    assert "No check was used." in response.json()["detail"]
    assert graph.calls == []
    assert await balance(pool, address) == PRICE
    assert logs and "secret-shop" not in json.dumps(logs, default=str)


async def test_an_empty_balance_gets_402_without_a_fetch(
    client: httpx.AsyncClient, wallet: Wallet, shop: dict[str, Any]
) -> None:
    _, headers = await wallet(PRICE - 1)

    response = await client.post("/v1/analyze/stream", json={"url": URL}, headers=headers)

    assert response.status_code == 402
    assert response.json()["error_code"] == "INSUFFICIENT_CREDITS"
    assert shop["calls"] == []


async def test_fetches_are_rate_limited_per_account(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, wallet: Wallet, shop: dict[str, Any]
) -> None:
    # Unreadable pages are free, so the free fetches are bounded.
    monkeypatch.setattr(analyze, "_fetch_rate", analyze.RateLimit(2))
    shop["error"] = status_error(403)
    _, headers = await wallet(PRICE)

    codes = [(await client.post("/v1/analyze", json={"url": URL}, headers=headers)).status_code for _ in range(3)]

    assert codes == [422, 422, 429]
    assert len(shop["calls"]) == 2


async def test_the_stream_of_a_url_only_check(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet, shop: dict[str, Any]
) -> None:
    address, headers = await wallet(PRICE)

    response = await client.post("/v1/analyze/stream", json={"url": URL}, headers=headers)

    assert response.headers["content-type"].startswith("text/event-stream")
    events = [block for block in response.text.split("\n\n") if block]
    assert events[0].startswith("event: step") and events[-1].startswith("event: result")
    result = json.loads(events[-1].split("data: ", 1)[1])
    assert result["charged"] is True and result["score"] == 80
    assert await balance(pool, address) == 0


async def test_the_checked_url_is_not_logged(
    client: httpx.AsyncClient, wallet: Wallet, shop: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    _, headers = await wallet(2 * PRICE)
    caplog.set_level(logging.DEBUG)

    with capture_logs() as logs:
        await client.post("/v1/analyze/stream", json={"url": URL}, headers=headers)
        shop["error"] = status_error(403)
        await client.post("/v1/analyze", json={"url": URL + "&x=1"}, headers=headers)

    assert logs  # something was logged (usage, the refusal) ...
    assert "secret-shop" not in json.dumps(logs, default=str)  # ... but never the page
    assert "secret-shop" not in caplog.text


async def test_a_url_only_check_can_be_reported_with_the_page_fetched_again(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet, shop: dict[str, Any]
) -> None:
    _, headers = await wallet(2 * PRICE)
    result = (await client.post("/v1/analyze", json={"url": URL}, headers=headers)).json()

    response = await client.post(
        "/v1/reports",
        json={
            "report_token": result["report_token"],
            "reason": "real_brand",
            "consent": True,
            "request": {"url": URL},
            "explanation": result["explanation"],
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
    assert len(shop["calls"]) == 2  # once for the check, once for the report
    data = json.loads(await pool.fetchval("SELECT payload FROM reports"))
    assert data["url"] == "https://secret-shop.test/products/wool-sweater"  # without the query string
    assert "merino wool" in data["text"]
