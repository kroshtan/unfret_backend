import httpx2
import openai

from unfret.routes.check import _failure_site


def _fetch_the_shop() -> None:
    raise RuntimeError("Client error for url 'https://shop.example/secret-product'")


def test_the_failure_site_names_our_frames_but_never_the_message() -> None:
    try:
        _fetch_the_shop()
    except RuntimeError as exc:
        site = _failure_site(exc)
    assert site["raised_in"].endswith(":_fetch_the_shop")
    assert any(frame.endswith(":_fetch_the_shop") for frame in site["ours"]) or site["ours"] == []
    assert "shop.example" not in str(site)
    assert "provider" not in site


def test_a_provider_error_keeps_its_code_and_message_without_urls() -> None:
    response = httpx2.Response(403, request=httpx2.Request("POST", "https://llm.example/v1/chat/completions"))
    body = {"error": {"message": "Blocked: see https://shop.example/x and www.shop.example", "type": "forbidden"}}
    try:
        raise openai.PermissionDeniedError("denied", response=response, body=body)
    except openai.PermissionDeniedError as exc:
        site = _failure_site(exc)
    assert site["provider"] == {
        "status": 403,
        "code": None,
        "type": "forbidden",
        "message": "Blocked: see <url> and <url>",
    }
    assert "shop.example" not in str(site)
