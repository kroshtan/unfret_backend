"""The remote MCP server at /mcp, driven by the MCP SDK's own client over streamable HTTP (in-process)."""

import asyncio
import inspect
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import httpx2
import pytest
from mcp.client.streamable_http import streamable_http_client

from mcp import Client
from tests.conftest import page_fetcher
from tests.integration.conftest import FakeGraph
from tests.integration.test_check_pass import new_pass
from unfret import mcp_remote
from unfret.config import settings
from unfret.main import app
from unfret.routes import analyze, check

PAGE = {"url": "https://shop.test/sweater", "page_content": "<h1>Sweater</h1>"}


@pytest.fixture(autouse=True)
def easy_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(analyze, "_quick_rate", analyze.RateLimit(1000))
    monkeypatch.setattr(check, "_rate", check.RateLimit(1000))
    monkeypatch.setattr(mcp_remote, "_bad_passes", mcp_remote.RateLimit(1000))


@pytest.fixture
async def serving(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    # The transport's task group must be entered and left in one task, as the app's lifespan does.
    started, stop = asyncio.Event(), asyncio.Event()

    async def serve() -> None:
        async with mcp_remote.running(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(serve())
    await started.wait()
    yield client
    stop.set()
    await task


def mcp_client(key: str | None, mode: str = "auto") -> Client:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test", headers=headers)
    return Client(streamable_http_client("http://test/mcp", http_client=http), mode=mode)


def structured(result: Any) -> dict[str, Any]:
    assert not result.is_error, result
    data: dict[str, Any] = result.structured_content
    return data


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_every_tool_has_a_title_and_explicit_hints(serving: httpx.AsyncClient, mode: str) -> None:
    issued = await new_pass(serving)
    async with mcp_client(issued["key"], mode) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}

    assert set(tools) == {"check_store_quick", "check_store_deep", "get_checks_left", "get_pricing"}
    for tool in tools.values():
        assert tool.title and tool.annotations is not None and tool.annotations.title == tool.title
        hints = tool.annotations.model_dump(by_alias=True)
        for hint in ("readOnlyHint", "destructiveHint", "openWorldHint", "idempotentHint"):
            assert isinstance(hints[hint], bool), (tool.name, hint)
    deep, quick = tools["check_store_deep"].annotations, tools["check_store_quick"].annotations
    pricing = tools["get_pricing"].annotations
    assert deep is not None and quick is not None and pricing is not None
    assert deep.read_only_hint is False and deep.destructive_hint is False  # it spends a check, deletes nothing
    assert quick.read_only_hint is True and pricing.read_only_hint is True  # free: nothing changes
    assert deep.open_world_hint is True and quick.open_world_hint is True  # they read a third-party page
    assert "tier" not in tools["check_store_deep"].input_schema["properties"]


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_a_deep_check_is_paid_from_the_pass(serving: httpx.AsyncClient, graph: FakeGraph, mode: str) -> None:
    issued = await new_pass(serving, developer=True)

    async with mcp_client(issued["key"], mode) as session:
        checked = structured(await session.call_tool("check_store_deep", PAGE))
        left = structured(await session.call_tool("get_checks_left", {}))

    assert checked["status"] == "ok" and checked["ok"] is True
    assert checked["verdict"]["action"] in {"allow", "warn", "block"} and checked["verdict"]["tier"] == "deep"
    assert checked["charged"] is True and checked["free_check"] is True and checked["free_checks_left"] == 19
    assert "charged" not in checked["verdict"]
    assert left["checks_remaining"] == 19 and left["free_checks_remaining"] == 19
    assert left["buy_more_at"].endswith("/checkout")
    assert len(graph.calls) == 1


async def test_a_quick_check_is_free(serving: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    quick = FakeGraph()
    monkeypatch.setattr(app, "quick_graph", quick)
    issued = await new_pass(serving)  # empty

    async with mcp_client(issued["key"]) as session:
        checked = structured(await session.call_tool("check_store_quick", PAGE))

    assert checked["status"] == "ok" and checked["charged"] is False and checked["verdict"]["tier"] == "quick"


async def test_an_empty_pass_is_told_what_to_do(serving: httpx.AsyncClient, graph: FakeGraph) -> None:
    issued = await new_pass(serving)

    async with mcp_client(issued["key"]) as session:
        refused = structured(await session.call_tool("check_store_deep", PAGE))

    assert refused["status"] == "no_checks_left" and refused["ok"] is False
    assert refused["error_code"] == "INSUFFICIENT_CREDITS"
    assert "/checkout" in refused["next_step"] and "check_store_quick" in refused["next_step"]
    assert graph.calls == []


async def test_an_unreadable_page_asks_for_its_html(
    serving: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def blocked(url: str, **_: object) -> bytes:
        raise OSError("refused")

    monkeypatch.setattr(check, "fetch_public_page", page_fetcher(blocked))
    issued = await new_pass(serving, developer=True)

    async with mcp_client(issued["key"]) as session:
        refused = structured(await session.call_tool("check_store_deep", {"url": "https://shop.test/x"}))

    assert refused["status"] == "page_unreadable" and "page_content" in refused["next_step"]


async def test_pricing(serving: httpx.AsyncClient) -> None:
    issued = await new_pass(serving)

    async with mcp_client(issued["key"]) as session:
        pricing = structured(await session.call_tool("get_pricing", {}))

    assert pricing["status"] == "ok" and "free" in pricing["pricing"]


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        ({}, None),
        ({"Authorization": "Bearer ufk_aaaaaaaaaa_" + "a" * 52}, "invalid_token"),
        ({"Authorization": "Bearer a-wallet-session-token"}, "invalid_token"),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, None),
    ],
)
async def test_only_a_pass_gets_in(serving: httpx.AsyncClient, headers: dict[str, str], error: str | None) -> None:
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}

    refused = await serving.post("/mcp", json=initialize, headers={"Accept": "application/json", **headers})

    assert refused.status_code == 401
    challenge = refused.headers["WWW-Authenticate"]
    assert challenge.startswith('Bearer realm="Unfret"')
    assert (f'error="{error}"' in challenge) if error else ("error=" not in challenge)
    assert refused.json()["error_code"] in {"INVALID_API_KEY", "UNAUTHORIZED"}


async def test_too_many_bad_passes_from_one_network_are_slowed_down(
    serving: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_remote, "_bad_passes", mcp_remote.RateLimit(2))
    bad = {"Authorization": "Bearer nope"}
    assert [(await serving.post("/mcp", json={}, headers=bad)).status_code for _ in range(3)] == [401, 401, 429]


async def test_outside_the_lifespan_it_is_unavailable(client: httpx.AsyncClient) -> None:
    issued = await new_pass(client)
    response = await client.post("/mcp", json={}, headers={"Authorization": f"Bearer {issued['key']}"})
    assert response.status_code == 503


def test_the_hosted_server_never_pays_from_a_wallet() -> None:
    source = inspect.getsource(mcp_remote)
    for wallet_thing in ("PAYMENT-SIGNATURE", "solders", "Keypair", "unfret_mcp", "unfret.x402"):
        assert wallet_thing not in source
    assert json.dumps(mcp_remote.server.instructions or "").count("pass") >= 1
