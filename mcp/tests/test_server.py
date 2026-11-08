from veridex_mcp.server import explain_flag, server


async def test_tools_are_registered() -> None:
    names = {tool.name for tool in await server.list_tools()}
    assert names == {"check_store", "explain_flag"}


def test_explain_flag_known_and_unknown() -> None:
    assert "AliExpress" in explain_flag("reverse_image_search")
    assert "scam" not in explain_flag("page_content").lower()
    assert "made_up" in explain_flag("made_up")
