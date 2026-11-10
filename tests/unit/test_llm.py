"""The provider-call limit: concurrency, jittered retries on rate limits, and pass-through of other errors."""

import asyncio

import httpx2
import openai
import pytest
from langchain_core.runnables import RunnableLambda

from unfret import llm
from unfret.config import settings

_BACKOFF = llm._backoff  # before the fixture replaces it


def _rate_limit() -> openai.RateLimitError:
    response = httpx2.Response(429, request=httpx2.Request("POST", "https://llm.test/v1/chat/completions"))
    return openai.RateLimitError("slow down", response=response, body=None)


@pytest.fixture(autouse=True)
def backoffs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    attempts: list[int] = []

    def no_wait(attempt: int) -> float:
        attempts.append(attempt)
        return 0.0

    monkeypatch.setattr(llm, "_backoff", no_wait)
    monkeypatch.setattr(settings, "llm_max_retries", 2)
    return attempts


def test_backoff_is_jittered_within_a_growing_window() -> None:
    samples = {attempt: [_BACKOFF(attempt) for _ in range(200)] for attempt in (0, 1, 2)}
    for attempt, values in samples.items():
        assert all(0 <= v <= settings.llm_backoff_base_s * 2**attempt for v in values)
        assert len(set(values)) > 150  # jittered, not a fixed interval


async def test_rate_limits_are_retried_with_backoff(backoffs: list[int]) -> None:
    attempts = {"n": 0}

    async def call() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _rate_limit()
        return "ok"

    assert await llm.throttled(call) == "ok"
    assert attempts["n"] == 3
    assert backoffs == [0, 1]


async def test_other_errors_are_not_retried() -> None:
    attempts = {"n": 0}

    async def call() -> str:
        attempts["n"] += 1
        raise ValueError("bad schema")

    with pytest.raises(ValueError, match="bad schema"):
        await llm.throttled(call)
    assert attempts["n"] == 1


async def test_retries_give_up_with_the_provider_error() -> None:
    async def call() -> str:
        raise _rate_limit()

    with pytest.raises(openai.RateLimitError):
        await llm.throttled(call)


async def test_calls_in_flight_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_max_concurrency", 2)
    llm._limits.clear()
    running, peak = 0, 0
    gate = asyncio.Event()

    async def call() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await gate.wait()
        running -= 1

    tasks = [asyncio.create_task(llm.throttled(call)) for _ in range(5)]
    await asyncio.sleep(0.01)
    assert running == 2
    gate.set()
    await asyncio.gather(*tasks)

    assert peak == 2


async def test_wrapped_runnables_go_through_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = llm.throttled

    async def spy(call: object) -> object:
        seen.append("limited")
        return await real(call)  # type: ignore[arg-type]

    monkeypatch.setattr(llm, "throttled", spy)
    wrapped = llm._throttle(RunnableLambda(lambda x: x * 2))

    assert await wrapped.ainvoke(21) == 42
    assert seen == ["limited"]
