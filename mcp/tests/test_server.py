from typing import Any

import httpx
import pytest
from solders.keypair import Keypair

from unfret_mcp import server as server_module
from unfret_mcp.server import explain_flag, server
from unfret_mcp.solana import CAIP2


async def test_tools_are_registered() -> None:
    names = {tool.name for tool in await server.list_tools()}
    assert names == {"check_store", "explain_flag", "get_pricing"}
    check = next(t for t in await server.list_tools() if t.name == "check_store")
    assert all(p.get("description") for p in check.input_schema["properties"].values())


def test_explain_flag_known_and_unknown() -> None:
    assert "AliExpress" in explain_flag("reverse_image_search")
    assert "scam" not in explain_flag("page_content").lower()
    assert "made_up" in explain_flag("made_up")


def test_settings_read_the_unfret_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNFRET_API_URL", raising=False)
    assert server_module._api_url() == "https://unfret.net"
    monkeypatch.setenv("UNFRET_API_URL", "https://new.example/")
    assert server_module._api_url() == "https://new.example"


def test_the_payer_defaults_to_mainnet(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("UNFRET_MCP_NETWORK", "UNFRET_MCP_RPC_URL", "UNFRET_MCP_KEYPAIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNFRET_MCP_SECRET_KEY", str(Keypair()))
    monkeypatch.setattr(server_module, "_payer", None)
    assert server_module.payer().network == CAIP2["solana-mainnet"]


def test_an_unknown_network_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNFRET_MCP_NETWORK", "solana-testnet")
    monkeypatch.setattr(server_module, "_payer", None)
    with pytest.raises(RuntimeError, match="UNFRET_MCP_NETWORK"):
        server_module.payer()


async def test_every_tool_has_a_title_and_explicit_hints() -> None:
    for tool in await server.list_tools():
        assert tool.title and tool.annotations is not None and tool.annotations.title == tool.title
        hints = tool.annotations.model_dump(by_alias=True)
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert isinstance(hints[hint], bool), (tool.name, hint)
    check = next(t for t in await server.list_tools() if t.name == "check_store")
    assert check.annotations is not None and check.annotations.read_only_hint is False


def _fake_api(monkeypatch: pytest.MonkeyPatch, status: int, answer: dict[str, Any]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=answer)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        server_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    return seen


async def test_with_a_pass_no_wallet_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("UNFRET_MCP_KEYPAIR", "UNFRET_MCP_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("UNFRET_PASS", "ufk_abc")
    monkeypatch.setattr(server_module, "_payer", None)
    verdict = {"action": "allow", "tier": "deep", "charged": True, "free_check": True, "free_checks_left": 19}
    seen = _fake_api(monkeypatch, 200, verdict)

    result = await server_module.check_store("https://shop.test/a", tier="deep")

    assert result["status"] == "ok" and result["verdict"] == {"action": "allow", "tier": "deep"}
    assert result["charged"] is True and result["free_checks_left"] == 19
    assert seen[0].headers["Authorization"] == "Bearer ufk_abc"
    assert "PAYMENT-SIGNATURE" not in seen[0].headers
    assert server_module._payer is None  # the wallet was never set up


async def test_an_empty_pass_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNFRET_PASS", "ufk_abc")
    _fake_api(monkeypatch, 402, {"detail": "Not enough credits.", "error_code": "INSUFFICIENT_CREDITS"})

    result = await server_module.check_store("https://shop.test/a", tier="deep")

    assert result["status"] == "not_paid" and result["error_code"] == "INSUFFICIENT_CREDITS"
    assert "quick" in result["next_step"] and "/checkout" in result["next_step"]
