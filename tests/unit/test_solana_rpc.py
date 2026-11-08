"""SolanaRpc over a mock transport: error wrapping, confirmation polling, and what a pending payment leads to."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from veridex.net import fetch_public
from veridex.routes import check
from veridex.x402.solana import RpcError, SettlementPendingError, SolanaRpc, TransactionFailedError


def rpc(handler: Any) -> SolanaRpc:
    return SolanaRpc("http://rpc.test", transport=httpx.MockTransport(handler))


def answer(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def test_transport_and_decoding_failures_are_rpc_errors() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    for handler in (
        down,
        lambda r: httpx.Response(524, text="<html>origin timeout</html>"),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json=[1, 2]),
    ):
        with pytest.raises(RpcError):
            await rpc(handler).call("getSlot", [])
    with pytest.raises(RpcError, match="boom"):
        await rpc(lambda r: httpx.Response(200, json={"error": {"message": "boom"}})).call("getSlot", [])


async def test_one_connection_pool_is_reused() -> None:
    client = rpc(lambda r: answer(42))
    assert await client.call("getSlot", []) == 42
    pool = client._client
    assert await client.call("getSlot", []) == 42
    assert client._client is pool
    await client.aclose()
    assert client._client is None


async def test_confirmation_polling_survives_hiccups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.x402.solana._POLL_S", 0)
    statuses = iter([None, "error", {"confirmationStatus": "processed"}, {"confirmationStatus": "confirmed"}])

    def handle(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        if method == "sendTransaction":
            return answer("sig")
        status = next(statuses)
        if status == "error":
            return httpx.Response(502)
        return answer({"value": [status]})

    assert await rpc(handle).send_and_confirm(b"tx", timeout_s=5) == "sig"


async def test_a_landed_failure_and_a_timeout_are_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.x402.solana._POLL_S", 0)

    def failed(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["method"] == "sendTransaction":
            return answer("sig")
        return answer({"value": [{"err": {"InstructionError": [0, "Custom"]}, "confirmationStatus": "confirmed"}]})

    with pytest.raises(TransactionFailedError):
        await rpc(failed).send_and_confirm(b"tx", timeout_s=5)

    def unknown(request: httpx.Request) -> httpx.Response:
        return answer("sig" if json.loads(request.content)["method"] == "sendTransaction" else {"value": [None]})

    with pytest.raises(SettlementPendingError):
        await rpc(unknown).send_and_confirm(b"tx", timeout_s=0.05)


class Watched:
    def __init__(self, statuses: list[dict[str, Any] | None]) -> None:
        self.statuses = statuses
        self.rpc = self

    async def signature_status(self, signature: str) -> dict[str, Any] | None:
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


@pytest.mark.parametrize(
    ("statuses", "credited"),
    [
        ([None, {"err": None, "confirmationStatus": "confirmed"}], [("payer", 150_000)]),  # landed late: credited
        ([None, {"err": {"x": 1}, "confirmationStatus": "confirmed"}], []),  # failed: nothing was paid
        ([None], []),  # never seen: nothing was paid
    ],
)
async def test_a_pending_payment_is_credited_only_if_it_lands(
    statuses: list[dict[str, Any] | None], credited: list[tuple[str, int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    got: list[tuple[str, int]] = []

    async def credit(pool: object, payer: str, usd_micros: int) -> int:
        got.append((payer, usd_micros))
        return usd_micros

    real_sleep = asyncio.sleep

    async def no_wait(_: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(check, "_credit", credit)
    monkeypatch.setattr(check, "_PENDING_WATCH_S", 0.05)
    monkeypatch.setattr(check.asyncio, "sleep", no_wait)
    await check._credit_if_it_lands(Watched(statuses), None, "sig", "payer", 150_000)  # type: ignore[arg-type]
    assert got == credited


async def test_a_trickling_page_hits_the_total_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(url: str) -> str:
        return "93.184.216.34"

    async def slow_stream() -> Any:
        while True:
            await asyncio.sleep(0.02)
            yield b"x"

    class Trickle(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=slow_stream())

    monkeypatch.setattr("veridex.net._assert_public", public)
    with pytest.raises(TimeoutError):
        await fetch_public("https://shop.test/", total_timeout=0.1, transport=Trickle())
