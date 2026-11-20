"""The client against a fake Unfret (httpx.MockTransport)."""

import hashlib
import json
from typing import Any

import httpx
import pytest

from unfret_client import (
    AsyncUnfret,
    InvalidPassError,
    NoChecksLeftError,
    PageUnreadableError,
    RateLimitedError,
    Unfret,
    decide,
    solve,
)

VERDICT = {
    "action": "warn",
    "assessed": True,
    "risk_score": 0.5,
    "confidence": 0.8,
    "flags": [{"type": "timer_resets", "evidence": "The countdown restarts.", "weight": 1.0, "label": "Fake timer"}],
    "policy_applied": "balanced",
    "tier": "deep",
    "band": "careful",
    "charged": True,
    "free_checks_left": 19,
}


def fake(
    status: int = 200, body: dict[str, Any] | None = None, seen: list[httpx.Request] | None = None
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=VERDICT if body is None else body)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("band", "action"),
    [("reliable", "allow"), ("concerns", "warn"), ("careful", "block"), ("junk_or_scam", "block")],
)
def test_the_four_verdicts_map_to_actions(band: str, action: str) -> None:
    assert decide({**VERDICT, "band": band}) == action


def test_the_policy_is_configurable() -> None:
    assert decide({**VERDICT, "band": "careful"}, {"careful": "warn"}) == "warn"
    assert decide({**VERDICT, "band": "reliable"}, {"careful": "warn"}) == "allow"  # the rest stays


def test_an_unassessed_page_is_a_warning_by_default() -> None:
    page = {**VERDICT, "assessed": False, "band": None, "risk_score": None}
    assert decide(page) == "warn"
    assert decide(page, unassessed="block") == "block"


def test_without_a_band_unfrets_own_action_is_used() -> None:
    assert decide({**VERDICT, "band": None, "action": "allow"}) == "allow"


def test_check_sends_the_pass_and_returns_a_decision() -> None:
    seen: list[httpx.Request] = []
    decision = Unfret("ufk_x", base_url="https://dev.unfret.net/", http=fake(seen=seen)).check("https://s.test/p")

    assert decision.action == "block" and decision.band == "careful" and decision.label == "Think twice"
    assert decision.reasons == ["Fake timer: The countdown restarts."]
    assert decision.verdict["free_checks_left"] == 19
    request = seen[0]
    assert str(request.url) == "https://dev.unfret.net/v1/check"
    assert request.headers["Authorization"] == "Bearer ufk_x"
    assert json.loads(request.content) == {"url": "https://s.test/p", "tier": "deep"}


def test_page_content_is_sent_when_given() -> None:
    seen: list[httpx.Request] = []
    Unfret("ufk_x", http=fake(seen=seen)).check("https://s.test/p", tier="quick", page_content="<h1>x</h1>")
    assert json.loads(seen[0].content) == {"url": "https://s.test/p", "tier": "quick", "page_content": "<h1>x</h1>"}


def test_the_pass_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNFRET_PASS", "ufk_env")
    monkeypatch.setenv("UNFRET_API_URL", "https://dev.unfret.net")
    client = Unfret(http=fake())
    assert client.unfret_pass == "ufk_env" and client.base_url == "https://dev.unfret.net"


def test_without_a_pass_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNFRET_PASS", raising=False)
    seen: list[httpx.Request] = []
    with pytest.raises(InvalidPassError):
        Unfret(http=fake(seen=seen)).check("https://s.test/p")
    assert seen == []


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"detail": "Unknown pass.", "error_code": "INVALID_API_KEY"}, InvalidPassError),
        (402, {"detail": "Not enough credits.", "error_code": "INSUFFICIENT_CREDITS"}, NoChecksLeftError),
        (
            429,
            {"detail": "Back tomorrow.", "error_code": "FREE_CHECKS_PAUSED", "metadata": {"retry_after_s": 60}},
            RateLimitedError,
        ),
        (422, {"detail": "Could not fetch.", "error_code": "PAGE_UNAVAILABLE"}, PageUnreadableError),
    ],
)
def test_refusals_raise(status: int, body: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error) as raised:
        Unfret("ufk_x", http=fake(status, body)).check("https://s.test/p")
    assert raised.value.code == body["error_code"]  # type: ignore[attr-defined]
    if status == 429:
        assert raised.value.retry_after == 60  # type: ignore[attr-defined]


def test_solve_finds_the_proof_of_work() -> None:
    solution = solve("abc", 8)
    assert hashlib.sha256(f"abc:{solution}".encode()).digest()[0] == 0


def test_a_new_developer_pass_is_used_from_then_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/keys/challenge":
            assert request.url.params["developer"] == "true"  # a developer pass needs the harder challenge
            return httpx.Response(200, json={"challenge": "c", "bits": 12, "expires_at": 0, "developer": True})
        if request.url.path == "/v1/keys":
            body = json.loads(request.content)
            assert body["developer"] is True and body["challenge"] == "c"
            digest = hashlib.sha256(f"c:{body['solution']}".encode()).digest()
            assert digest[0] == 0 and digest[1] < 16  # solved at the challenge's 12 bits
            return httpx.Response(201, json={"key": "ufk_new", "key_id": "x", "free_checks": 20})
        assert request.headers["Authorization"] == "Bearer ufk_new"
        return httpx.Response(200, json=VERDICT)

    client = Unfret(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.new_developer_pass()["free_checks"] == 20
    assert client.check("https://s.test/p").action == "block"


async def test_the_async_client() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**VERDICT, "band": "reliable"})

    async with AsyncUnfret("ufk_x", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        decision = await client.check("https://s.test/p")
    assert decision.action == "allow" and decision.label == "Looks fine"
