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
