"""SolanaRpc over a mock transport: error wrapping, confirmation polling, and what a pending payment leads to."""

import asyncio
import json
from typing import Any

import httpx
import pytest
from solders.pubkey import Pubkey

from unfret.net import fetch_public
from unfret.routes import check
from unfret.x402.solana import RpcError, SettlementPendingError, SolanaRpc, TransactionFailedError


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


async def test_simulation_returns_its_error_and_logs() -> None:
    value = {
        "err": {"InstructionError": [1, {"Custom": 1}]},
        "accounts": None,
        "logs": ["Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA failed: custom program error: 0x1"],
    }
    error, accounts, inner, logs = await rpc(lambda r: answer({"value": value})).simulate_accounts(b"tx", [])
    assert (error, accounts, inner, logs) == (value["err"], [], [], value["logs"])


async def test_one_connection_pool_is_reused() -> None:
    client = rpc(lambda r: answer(42))
    assert await client.call("getSlot", []) == 42
    pool = client._client
    assert await client.call("getSlot", []) == 42
    assert client._client is pool
    await client.aclose()
    assert client._client is None


async def test_confirmation_polling_survives_hiccups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.x402.solana._POLL_S", 0)
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
    monkeypatch.setattr("unfret.x402.solana._POLL_S", 0)

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

    monkeypatch.setattr("unfret.net._assert_public", public)
    with pytest.raises(TimeoutError):
        await fetch_public("https://shop.test/", total_timeout=0.1, transport=Trickle())


class Recorder:
    """A JSON-RPC endpoint answering each method from ``results``, recording (method, params) of every request."""

    def __init__(self, **results: Any) -> None:
        self.results = results
        self.requests: list[tuple[str, list[Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert (body["jsonrpc"], body["id"]) == ("2.0", 1)
        self.requests.append((body["method"], body["params"]))
        result = self.results[body["method"]]
        return answer(result.pop(0) if isinstance(result, list) else result)


OWNER = Pubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")
TOKEN_ACCOUNT = Pubkey.from_string("So11111111111111111111111111111111111111112")


def token(amount: int) -> dict[str, Any]:
    return {"lamports": 2_039_280, "data": {"parsed": {"info": {"tokenAmount": {"amount": str(amount)}}}}}


def statuses(*values: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [{"value": [v]} for v in values]


async def test_send_and_confirm_submits_once_then_polls_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.x402.solana._POLL_S", 0)
    finalized = {"err": None, "confirmationStatus": "finalized"}
    endpoint = Recorder(sendTransaction="5ig", getSignatureStatuses=statuses(None, finalized))

    assert await rpc(endpoint).send_and_confirm(b"\x01\x02", timeout_s=5) == "5ig"

    assert endpoint.requests == [
        ("sendTransaction", ["AQI=", {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}]),
        ("getSignatureStatuses", [["5ig"], {"searchTransactionHistory": True}]),
        ("getSignatureStatuses", [["5ig"], {"searchTransactionHistory": True}]),
    ]


async def test_a_dropped_transaction_is_resent_while_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.x402.solana._POLL_S", 0)
    landed = {"err": None, "confirmationStatus": "confirmed"}
    endpoint = Recorder(sendTransaction="5ig", getSignatureStatuses=statuses(*[None] * 9, landed))

    assert await rpc(endpoint).send_and_confirm(b"\x01", timeout_s=5) == "5ig"

    resends = [params for method, params in endpoint.requests[1:] if method == "sendTransaction"]
    assert resends == [["AQ==", {"encoding": "base64", "skipPreflight": True}]]  # once, after 8 unanswered polls


async def test_a_refused_submission_is_an_rpc_error_not_pending() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": -32002, "message": "Blockhash not found"}})

    with pytest.raises(RpcError, match="sendTransaction: Blockhash not found"):
        await rpc(refuse).send_and_confirm(b"tx", timeout_s=5)


async def test_simulate_raises_on_an_error_and_passes_otherwise() -> None:
    ok = Recorder(simulateTransaction={"value": {"err": None, "logs": []}})
    await rpc(ok).simulate(b"\x01")
    assert ok.requests == [
        ("simulateTransaction", ["AQ==", {"encoding": "base64", "sigVerify": False, "commitment": "confirmed"}])
    ]
    doomed = Recorder(simulateTransaction={"value": {"err": "BlockhashNotFound"}})
    with pytest.raises(RpcError, match="simulation failed: BlockhashNotFound"):
        await rpc(doomed).simulate(b"\x01")


async def test_simulate_accounts_reads_inner_programs_and_post_state() -> None:
    value = {
        "err": None,
        "accounts": [token(7), None],
        "innerInstructions": [{"index": 2, "instructions": [{"programIdIndex": 5}, {"programIdIndex": 3}]}],
        "logs": ["Program log: ok"],
    }
    endpoint = Recorder(simulateTransaction={"value": value})

    result = await rpc(endpoint).simulate_accounts(b"\x01", [TOKEN_ACCOUNT, OWNER])

    assert result == (None, [token(7), None], [5, 3], ["Program log: ok"])
    _, (_, options) = endpoint.requests[0]
    assert options["accounts"] == {"encoding": "jsonParsed", "addresses": [str(TOKEN_ACCOUNT), str(OWNER)]}
    assert (options["sigVerify"], options["replaceRecentBlockhash"], options["innerInstructions"]) == (
        False,
        False,
        True,
    )


async def test_signature_status_is_none_while_unknown() -> None:
    status = {"err": None, "confirmationStatus": "confirmed", "slot": 5}
    assert await rpc(Recorder(getSignatureStatuses={"value": [status]})).signature_status("s") == status
    assert await rpc(Recorder(getSignatureStatuses={"value": [None]})).signature_status("s") is None


async def test_balances_read_sol_and_tokens_in_one_call() -> None:
    endpoint = Recorder(getMultipleAccounts={"value": [{"lamports": 5_000}, token(42), None]})

    assert await rpc(endpoint).balances(OWNER, [TOKEN_ACCOUNT, OWNER]) == (5_000, [42, None])

    assert endpoint.requests == [
        (
            "getMultipleAccounts",
            [[str(OWNER), str(TOKEN_ACCOUNT), str(OWNER)], {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
    ]
    missing = Recorder(getMultipleAccounts={"value": [None]})
    assert await rpc(missing).balances(OWNER, []) == (0, [])  # a wallet that doesn't exist holds nothing


async def test_single_account_reads() -> None:
    endpoint = Recorder(
        getBalance={"value": 1_234},
        getAccountInfo=[{"value": token(9)}, {"value": None}],
        getMinimumBalanceForRentExemption=2_039_280,
        getLatestBlockhash={"value": {"blockhash": "Hash1", "lastValidBlockHeight": 9}},
        getSignaturesForAddress=[[{"signature": "a"}, {"signature": "b"}]],
    )
    client = rpc(endpoint)

    assert await client.get_balance(OWNER) == 1_234
    assert await client.token_balance(TOKEN_ACCOUNT) == 9
    assert await client.token_balance(TOKEN_ACCOUNT) is None
    assert await client.rent_exempt_minimum(165) == 2_039_280
    assert await client.latest_blockhash() == "Hash1"
    assert await client.recent_signatures(OWNER, limit=2) == ["a", "b"]
    assert endpoint.requests == [
        ("getBalance", [str(OWNER), {"commitment": "confirmed"}]),
        ("getAccountInfo", [str(TOKEN_ACCOUNT), {"encoding": "jsonParsed", "commitment": "confirmed"}]),
        ("getAccountInfo", [str(TOKEN_ACCOUNT), {"encoding": "jsonParsed", "commitment": "confirmed"}]),
        ("getMinimumBalanceForRentExemption", [165]),
        ("getLatestBlockhash", [{"commitment": "confirmed"}]),
        ("getSignaturesForAddress", [str(OWNER), {"limit": 2, "commitment": "confirmed"}]),
    ]


async def test_signature_page_and_transactions_pass_paging_and_commitment() -> None:
    entries = [{"signature": "b", "err": None}, {"signature": "a", "err": {"x": 1}}]
    endpoint = Recorder(getSignaturesForAddress=[entries, []], getTransaction=[{"meta": {"err": None}}, None])
    client = rpc(endpoint)

    assert await client.signature_page(OWNER, before="c", until="z", limit=2) == entries
    assert await client.signature_page(OWNER, commitment="confirmed") == []
    assert await client.parsed_transaction("b", commitment="finalized") == {"meta": {"err": None}}
    assert await client.parsed_transaction("gone") is None

    assert [params for _, params in endpoint.requests] == [
        [str(OWNER), {"limit": 2, "commitment": "finalized", "before": "c", "until": "z"}],
        [str(OWNER), {"limit": 1000, "commitment": "confirmed"}],
        ["b", {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}],
        ["gone", {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
    ]
