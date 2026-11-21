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

from tests.conftest import page_fetcher
from tests.integration.conftest import FakeGraph
from tests.integration.test_free_checks import free_pass
from tests.integration.test_keys import bearer
from unfret import page_fetch, shares
from unfret.config import settings
from unfret.credits import free
from unfret.main import app
from unfret.net import FetchedPage, RefusedError, UnsafeURLError
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

    monkeypatch.setattr(page_fetch, "fetch_public_page", page_fetcher(fake_fetch))
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


async def test_a_url_checked_by_someone_else_costs_a_check_and_no_fetch(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Wallet, shop: dict[str, Any]
) -> None:
    _, first = await wallet(PRICE)
    address, other = await wallet(PRICE)
    _, empty = await wallet(0)
    await client.post("/v1/analyze", json={"url": URL}, headers=first)

    cached = await client.post("/v1/analyze", json={"url": URL}, headers=other)
    refused = await client.post("/v1/analyze", json={"url": URL}, headers=empty)

    assert cached.json()["charged"] is True
    assert await balance(pool, address) == 0
    assert refused.status_code == 402 and refused.json()["error_code"] == "INSUFFICIENT_CREDITS"
    assert len(shop["calls"]) == 1 and len(graph.calls) == 1


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ({"error": status_error(403)}, "PAGE_BLOCKED"),
        ({"error": status_error(429)}, "PAGE_BLOCKED"),
        ({"error": status_error(503)}, "PAGE_BLOCKED"),
        ({"error": status_error(404)}, "PAGE_UNAVAILABLE"),
        ({"error": httpx.ConnectError("no route")}, "PAGE_UNAVAILABLE"),
        ({"error": TimeoutError()}, "PAGE_UNAVAILABLE"),
        ({"error": httpx.ReadTimeout("no answer")}, "PAGE_BLOCKED"),
        (
            {
                "error": RefusedError(
                    httpx.Response(400, request=httpx.Request("GET", URL)), b"<script>window.KPSDK={}"
                )
            },
            "PAGE_BLOCKED",
        ),
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


@pytest.mark.parametrize("path", ["/v1/analyze", "/v1/analyze/stream"])
async def test_a_blocked_page_keeps_the_free_check_and_runs_nothing(  # noqa: PLR0917 - fixtures and parameters
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    graph: FakeGraph,
    pool: asyncpg.Pool,
    shop: dict[str, Any],
    path: str,
) -> None:
    # The link checker's first check is on a free pass; a shop that turns the server away answers at once.
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(analyze, "_free_outcomes", analyze.RateLimit(1, window_s=3600))
    issued = await free_pass(client)
    shop["error"] = status_error(403)

    for _ in range(2):  # and again: a refusal never counts as an uncharged analysis either
        response = await client.post(path, json={"url": URL}, headers=bearer(issued["key"]))
        assert response.status_code == 422
        assert response.json()["error_code"] == "PAGE_BLOCKED"

    assert graph.calls == []
    assert await free.left(pool, issued["key_id"]) == settings.free_checks_per_pass
    assert await free.used_today(pool) == 0


async def test_a_blocked_page_answers_a_quick_check_without_running_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, shop: dict[str, Any]
) -> None:
    quick = FakeGraph()
    monkeypatch.setattr(app, "quick_graph", quick)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    shop["body"] = b"<html><title>Pardon Our Interruption</title><body>Please verify you are human.</body></html>"

    response = await client.post("/v1/analyze/stream", json={"url": URL, "tier": "quick"})

    assert response.status_code == 422
    assert response.json()["error_code"] == "PAGE_BLOCKED"
    assert quick.calls == []


SHORT = "https://short.test/d/AbC123"
LANDED = "https://secret-shop.test/products/wool-sweater?variant=7&utm_source=share&tag=aff-21#reviews"
PAGE_ADDRESS = "https://secret-shop.test/products/wool-sweater?variant=7"


@pytest.fixture
def short_link(monkeypatch: pytest.MonkeyPatch, shop: dict[str, Any]) -> dict[str, Any]:
    """The shop's page behind a short link: fetching :data:`SHORT` lands on :data:`LANDED` (redirects mocked)."""

    async def fetch(url: str, **_: Any) -> FetchedPage:
        shop["calls"].append(url)
        return FetchedPage(PRODUCT_HTML.encode(), LANDED if url == SHORT else url)

    monkeypatch.setattr(page_fetch, "fetch_public_page", fetch)
    return shop


@pytest.mark.parametrize("path", ["/v1/analyze", "/v1/analyze/stream"])
async def test_a_short_link_is_checked_under_the_pages_own_address(
    client: httpx.AsyncClient, graph: FakeGraph, wallet: Wallet, short_link: dict[str, Any], path: str
) -> None:
    _, headers = await wallet(2 * PRICE)

    with capture_logs() as logs:
        response = await client.post(path, json={"url": SHORT}, headers=headers)

    assert response.status_code == 200
    assert graph.calls[0]["url"] == PAGE_ADDRESS  # facts, platform, product id, price store: the page's address
    assert "short.test" not in json.dumps(logs, default=str) and "secret-shop" not in json.dumps(logs, default=str)
    # The result is the page's: the page's own address asked for later is the same result, no second analysis.
    again = await client.post("/v1/analyze", json={"url": PAGE_ADDRESS}, headers=headers)
    assert again.status_code == 200 and again.json()["charged"] is False
    assert len(graph.calls) == 1


async def test_a_short_links_share_token_names_the_page_and_its_report_token_the_link(
    client: httpx.AsyncClient, pool: asyncpg.Pool, wallet: Wallet, short_link: dict[str, Any]
) -> None:
    _, headers = await wallet(2 * PRICE)
    result = (await client.post("/v1/analyze", json={"url": SHORT}, headers=headers)).json()

    shown = {k: v for k, v in result.items() if k != "share_token"}
    secret = settings.session_secret.get_secret_value()
    shares.check_token(secret, result["share_token"], url=PAGE_ADDRESS, response=shown, ttl_s=3600)
    with pytest.raises(shares.ShareError):
        shares.check_token(secret, result["share_token"], url=SHORT, response=shown, ttl_s=3600)
    # The caller reports with the request as they sent it: the short link.
    report = {
        "report_token": result["report_token"],
        "reason": "real_brand",
        "consent": True,
        "request": {"url": SHORT},
        "explanation": result["explanation"],
    }
    response = await client.post("/v1/reports", json=report, headers=headers)
    assert response.status_code == 201, response.text
    data = json.loads(await pool.fetchval("SELECT payload FROM reports"))
    assert data["url"] == "https://secret-shop.test/products/wool-sweater"  # the page, never the short link


async def test_a_page_checked_before_is_found_behind_a_short_link(
    client: httpx.AsyncClient, graph: FakeGraph, pool: asyncpg.Pool, wallet: Wallet, short_link: dict[str, Any]
) -> None:
    address, headers = await wallet(2 * PRICE)
    await client.post("/v1/analyze", json={"url": PAGE_ADDRESS}, headers=headers)

    again = await client.post("/v1/analyze", json={"url": SHORT}, headers=headers)

    assert again.status_code == 200 and again.json()["charged"] is False  # this account's own repeat
    assert len(graph.calls) == 1 and short_link["calls"] == [PAGE_ADDRESS, SHORT]
    assert await balance(pool, address) == PRICE
