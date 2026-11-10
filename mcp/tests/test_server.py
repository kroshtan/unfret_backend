import pytest

from unfret_mcp import server as server_module
from unfret_mcp.server import explain_flag, server


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
