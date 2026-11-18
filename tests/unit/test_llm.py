"""The provider-call limit: concurrency, jittered retries on rate limits, and pass-through of other errors."""

import asyncio
import os
from typing import Any

import httpx2
import openai
import pytest
from langchain_core.runnables import RunnableLambda
from langsmith import utils as langsmith_utils
from pydantic import BaseModel

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


async def test_a_runaway_attempt_is_abandoned_and_tried_again(backoffs: list[int]) -> None:
    # Now and then the model keeps writing until it is cut off; a fresh attempt answers in the usual time.
    attempts = {"n": 0}

    async def call() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            await asyncio.sleep(3600)
        return "ok"

    assert await llm.throttled(call, attempt_timeout_s=0.05) == "ok"
    assert attempts["n"] == 2
    assert backoffs == [0]


async def test_the_last_attempt_out_of_time_raises_a_timeout() -> None:
    attempts = {"n": 0}

    async def call() -> str:
        attempts["n"] += 1
        await asyncio.sleep(3600)
        return "never"

    with pytest.raises(TimeoutError):
        await llm.throttled(call, attempt_timeout_s=0.01)
    assert attempts["n"] == settings.llm_max_retries + 1


async def test_without_an_attempt_timeout_a_timeout_is_not_retried() -> None:
    attempts = {"n": 0}

    async def call() -> str:
        attempts["n"] += 1
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await llm.throttled(call)
    assert attempts["n"] == 1


async def test_waiting_for_a_slot_does_not_count_against_the_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_max_concurrency", 1)
    llm._limits.clear()
    attempts = {"n": 0}

    async def slow() -> str:
        await asyncio.sleep(0.1)
        return "first"

    async def quick() -> str:
        attempts["n"] += 1
        return "second"

    first = asyncio.create_task(llm.throttled(slow))
    await asyncio.sleep(0)
    assert await llm.throttled(quick, attempt_timeout_s=0.05) == "second"  # waited 0.1 s for the slot
    assert attempts["n"] == 1
    assert await first == "first"


async def test_structured_calls_pass_the_attempt_timeout_on(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []
    real = llm.throttled

    async def spy(call: object, *, attempt_timeout_s: float | None = None) -> object:
        seen.append(attempt_timeout_s)
        return await real(call, attempt_timeout_s=attempt_timeout_s)  # type: ignore[arg-type]

    class _Model:
        def with_structured_output(self, schema: object, method: str) -> RunnableLambda:
            return RunnableLambda(lambda _: "parsed")

    monkeypatch.setattr(llm, "throttled", spy)
    monkeypatch.setattr(llm, "get_llm", _Model)

    assert await llm.get_structured_llm(BaseModel, attempt_timeout_s=7.0).ainvoke("page") == "parsed"
    assert await llm.get_structured_llm(BaseModel).ainvoke("page") == "parsed"
    # Without one of its own, a call gets the shared attempt timeout: classification, triage, headline, LLM judge.
    assert seen == [7.0, settings.llm_attempt_timeout_s]


async def test_embeddings_get_the_shared_attempt_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_attempt_timeout_s", 0.05)
    attempts = {"n": 0}

    class _Inner:
        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            attempts["n"] += 1
            if attempts["n"] == 1:
                await asyncio.sleep(3600)  # a stuck first attempt
            return [[1.0] for _ in texts]

    embeddings = llm._ThrottledEmbeddings(_Inner())  # type: ignore[arg-type]
    assert await embeddings.aembed_documents(["a", "b"]) == [[1.0], [1.0]]
    assert attempts["n"] == 2


def _worst_case(attempt_timeout_s: float) -> float:
    # With the shipped defaults: every attempt runs to its timeout, with the longest backoff between them.
    defaults = {name: field.default for name, field in type(settings).model_fields.items()}
    attempts = defaults["llm_max_retries"] + 1
    assert attempts >= 2
    backoff = sum(defaults["llm_backoff_base_s"] * 2**a for a in range(attempts - 1))
    return float(attempts * attempt_timeout_s + backoff)


def test_two_attempts_of_every_llm_step_fit_within_its_timeout() -> None:
    defaults = {name: field.default for name, field in type(settings).model_fields.items()}
    shared = _worst_case(defaults["llm_attempt_timeout_s"])
    # Inside a skill (its own timeout): the content read, and the review embeddings.
    assert _worst_case(defaults["content_attempt_timeout_s"]) < defaults["skill_timeout_s"]
    assert shared < defaults["skill_timeout_s"]
    # The graph's own steps (classification, triage, headline, LLM judge) share the analysis's timeout with the skill
    # round: a step that runs away twice still fits next to a skill round that runs to its limit.
    assert shared + defaults["skill_timeout_s"] < defaults["analysis_timeout_s"]
    # Each is shorter than the client's own timeout, which would otherwise end the attempt first.
    assert max(defaults["llm_attempt_timeout_s"], defaults["content_attempt_timeout_s"]) < defaults["llm_timeout_s"]


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

    async def spy(call: object, **kwargs: Any) -> object:
        seen.append("limited")
        return await real(call, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(llm, "throttled", spy)
    wrapped = llm._throttle(RunnableLambda(lambda x: x * 2))

    assert await wrapped.ainvoke(21) == 42
    assert seen == ["limited"]


def test_langsmith_tracing_is_always_off() -> None:
    # Set when unfret.llm is imported, whatever the environment said.
    assert all(os.environ[v] == "false" for v in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"))
    assert not langsmith_utils.tracing_is_enabled()
