import base64
import json
import struct
from decimal import Decimal
from typing import Any

import httpx
import pytest
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from veridex_mcp.payer import (
    Payer,
    PaymentFailedError,
    SpendGuard,
    SpendRefusedError,
    build_payment,
    choose_requirement,
)
from veridex_mcp.price import SOL_USD_FEED_ID, PriceError, SolUsd
from veridex_mcp.solana import CAIP2, SYSTEM_PROGRAM, USDC_MINTS, WSOL_MINT, Rpc, associated_token_address

AGENT = Keypair()
FEE_PAYER = Keypair().pubkey()
TREASURY = Keypair().pubkey()
BLOCKHASH = "EZ3rST5dvHmbanh75jc4PuLfV96vp9fEYBVeNk4FfM1k"


def requirement(amount: int = 100_000, **extra: Any) -> dict[str, Any]:
    return {
        "scheme": "exact",
        "network": CAIP2["solana-devnet"],
        "amount": str(amount),
        "asset": str(WSOL_MINT),
        "payTo": str(TREASURY),
        "maxTimeoutSeconds": 60,
        "extra": {"feePayer": str(FEE_PAYER), "paymentFlow": "upfront", **extra},
    }


def b64(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


class FakeRpc(Rpc):
    def __init__(self, wsol: int = 10**9, sol: int = 10**9) -> None:
        super().__init__("http://rpc.invalid")
        self.wsol, self.sol = wsol, sol
        self.sent: list[VersionedTransaction] = []

    async def latest_blockhash(self) -> str:
        return BLOCKHASH

    async def balance(self, address: Pubkey) -> int:
        return self.sol

    async def token_balance(self, account: Pubkey) -> int:
        return self.wsol

    async def send_and_confirm(self, raw_tx: bytes, timeout_s: float = 60) -> str:
        self.sent.append(VersionedTransaction.from_bytes(raw_tx))
        return "wrapsig"


class FixedPrice(SolUsd):
    """A SOL/USD price that never touches the network (None: unavailable)."""

    def __init__(self, usd_per_sol: str | None = "100") -> None:
        super().__init__(Rpc("http://rpc.invalid"))
        self.value = usd_per_sol

    async def usd_per_sol(self) -> Decimal:
        if self.value is None:
            raise PriceError("stale")
        return Decimal(self.value)


def test_guard_caps_dollars_per_call_and_per_session() -> None:
    guard = SpendGuard(max_per_call=150_000, session_cap=200_000)  # $0.15, $0.20
    with pytest.raises(SpendRefusedError, match=r"per-call maximum of \$0.15"):
        guard.reserve(150_001)
    guard.reserve(150_000)
    with pytest.raises(SpendRefusedError, match="session cap"):
        guard.reserve(60_000)
    guard.release(150_000)
    guard.reserve(60_000)
    assert guard.spent == 60_000


def price_update(
    price: int = 10_000_000_000, conf: int = 1_000_000, publish_time: int = 1_000, full: bool = True
) -> bytes:
    level = b"\x01" if full else b"\x00\x05"
    message = SOL_USD_FEED_ID + struct.pack("<qQiqqqQ", price, conf, -8, publish_time, publish_time, price, conf)
    return bytes(40) + level + message + bytes(8)


def test_the_agents_own_price_is_checked_before_use() -> None:
    reader = SolUsd(Rpc("http://rpc.invalid"))
    assert reader.check(price_update(), now=1_010) == Decimal(100)
    for data, now, reason in [
        (price_update(), 1_121, "stale"),
        (price_update(full=False), 1_010, "verified"),
        (price_update(conf=300_000_000), 1_010, "uncertain"),  # $3 on $100: over 2%
        (price_update(price=0), 1_010, "positive"),
    ]:
        with pytest.raises(PriceError, match=reason):
            reader.check(data, now=now)


def test_chooses_only_wsol_exact_on_our_network() -> None:
    usdc = {**requirement(), "asset": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
    mainnet = {**requirement(), "network": CAIP2["solana-mainnet"]}
    with pytest.raises(PaymentFailedError):
        choose_requirement({"accepts": [usdc, mainnet]}, CAIP2["solana-devnet"])
    assert choose_requirement({"accepts": [usdc, requirement()]}, CAIP2["solana-devnet"]) == requirement()


def test_payment_layout_and_signatures() -> None:
    tx = build_payment(requirement(), AGENT, BLOCKHASH)
    message = tx.message
    assert message.account_keys[0] == FEE_PAYER
    assert len(message.instructions) == 4
    assert tx.signatures[1].verify(AGENT.pubkey(), to_bytes_versioned(message))
    transfer = message.instructions[2]
    accounts = [message.account_keys[i] for i in bytes(transfer.accounts)]
    assert accounts[2] == associated_token_address(TREASURY)
    assert int.from_bytes(bytes(transfer.data)[1:9], "little") == 100_000
    # The fee payer is referenced by no instruction: it only pays the fee.
    assert all(0 not in bytes(ix.accounts) and ix.program_id_index != 0 for ix in message.instructions)


def test_payment_uses_seller_memo_when_given() -> None:
    tx = build_payment(requirement(memo="inv-42"), AGENT, BLOCKHASH)
    assert bytes(tx.message.instructions[3].data) == b"inv-42"


async def test_wraps_only_the_shortfall() -> None:
    rpc = FakeRpc(wsol=30_000)
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=rpc,
        network="solana-devnet",
        guard=SpendGuard(10**7, 10**8),
        sol_usd=FixedPrice(),
    )

    await payer.ensure_wsol(100_000)

    (wrap,) = rpc.sent
    keys = wrap.message.account_keys
    system_ix = next(ix for ix in wrap.message.instructions if keys[ix.program_id_index] == SYSTEM_PROGRAM)
    assert int.from_bytes(bytes(system_ix.data)[4:12], "little") == 70_000


async def test_no_wrap_when_wsol_suffices_and_refusal_when_sol_is_short() -> None:
    rpc = FakeRpc(wsol=100_000)
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=rpc,
        network="solana-devnet",
        guard=SpendGuard(10**7, 10**8),
        sol_usd=FixedPrice(),
    )
    await payer.ensure_wsol(100_000)
    assert rpc.sent == []

    poor = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=FakeRpc(wsol=0, sol=50_000),
        network="solana-devnet",
        guard=SpendGuard(10**7, 10**8),
        sol_usd=FixedPrice(),
    )
    with pytest.raises(PaymentFailedError, match="not enough SOL"):
        await poor.ensure_wsol(100_000)


def api(amount: int, *, settle: bool = True) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            required = {"x402Version": 2, "accepts": [requirement(amount)]}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(required)}, json=required)
        if not settle:
            settlement = {"success": False, "errorReason": "settlement_failed", "transaction": ""}
            return httpx.Response(402, headers={"PAYMENT-RESPONSE": b64(settlement)}, json={"detail": "no"})
        settlement = {"success": True, "transaction": "5ig", "network": CAIP2["solana-devnet"]}
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": b64(settlement)}, json={"action": "warn"})

    return httpx.MockTransport(handle)


async def test_check_pays_and_counts_spend_in_dollars() -> None:
    guard = SpendGuard(max_per_call=250_000, session_cap=2_500_000)
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=FakeRpc(),
        network="solana-devnet",
        guard=guard,
        sol_usd=FixedPrice("100"),
        transport=api(100_000),
    )

    result = await payer.check({"url": "https://shop.test"})

    payment = result.pop("payment")
    assert payment  # the agent's own signature on the payment, to look it up
    assert result == {
        "status": "ok",
        "ok": True,
        "http_status": 200,
        "transaction": "5ig",
        "paid_lamports": 100_000,
        "paid": {"currency": "SOL", "amount": 100_000, "usd_micros": 10_000},
        "verdict": {"action": "warn"},
    }
    assert guard.spent == 10_000  # 0.0001 SOL at $100 = $0.01


async def test_failed_settlement_releases_the_reservation() -> None:
    guard = SpendGuard(max_per_call=250_000, session_cap=2_500_000)
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=FakeRpc(),
        network="solana-devnet",
        guard=guard,
        sol_usd=FixedPrice(),
        transport=api(100_000, settle=False),
    )

    result = await payer.check({"url": "https://shop.test"})

    assert (result["status"], result["http_status"], result["paid_lamports"], guard.spent) == ("not_paid", 402, 0, 0)


def flaky(answers: list[Any]) -> httpx.MockTransport:
    """The 402, then each paid attempt gets the next answer: a Response, or an exception to raise."""

    def handle(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            required = {"x402Version": 2, "accepts": [requirement(100_000)]}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(required)}, json=required)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, httpx.Response)
        return answer

    return httpx.MockTransport(handle)


def flaky_payer(guard: SpendGuard, answers: list[Any]) -> Payer:
    return Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=FakeRpc(),
        network="solana-devnet",
        guard=guard,
        sol_usd=FixedPrice("100"),
        transport=flaky(answers),
    )


SETTLED = httpx.Response(
    200, headers={"PAYMENT-RESPONSE": b64({"success": True, "transaction": "5ig"})}, json={"action": "allow"}
)


async def test_a_timeout_is_retried_with_the_same_payment() -> None:
    guard = SpendGuard(250_000, 2_500_000)
    result = await flaky_payer(guard, [httpx.ReadTimeout("slow"), SETTLED]).check({"url": "u"})
    assert (result["status"], result["ok"], guard.spent) == ("ok", True, 10_000)


@pytest.mark.parametrize(
    "answers",
    [
        [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")],  # never answered
        [httpx.Response(524, text="<html>origin timeout</html>")] * 2,  # a proxy gave up
        [httpx.Response(200, text="not json")],  # an unreadable answer
    ],
)
async def test_an_unknown_outcome_keeps_the_spend_counted(answers: list[Any]) -> None:
    guard = SpendGuard(250_000, 2_500_000)
    result = await flaky_payer(guard, answers).check({"url": "u"})
    assert result["status"] == "unknown" and not result["ok"]
    assert guard.spent == 10_000  # the money may have moved
    assert "next_step" in result


async def test_a_pending_settlement_keeps_the_spend_counted() -> None:
    guard = SpendGuard(250_000, 2_500_000)
    receipt = {"success": False, "transaction": "5ig", "errorReason": "settlement_pending"}
    pending = httpx.Response(
        402, headers={"PAYMENT-RESPONSE": b64(receipt)}, json={"error_code": "SETTLEMENT_PENDING"}
    )
    result = await flaky_payer(guard, [pending]).check({"url": "u"})
    assert (result["status"], guard.spent) == ("pending", 10_000)


async def test_a_refusal_before_payment_gives_the_spend_back() -> None:
    guard = SpendGuard(250_000, 2_500_000)
    blocked = httpx.Response(422, json={"error_code": "PAGE_BLOCKED"})
    result = await flaky_payer(guard, [blocked]).check({"url": "u"})
    assert (result["status"], result["error"]["error_code"], guard.spent) == ("not_paid", "PAGE_BLOCKED", 0)


async def test_overpriced_check_is_refused_before_paying() -> None:
    rpc = FakeRpc()
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=rpc,
        network="solana-devnet",
        guard=SpendGuard(max_per_call=100_000, session_cap=2_500_000),  # $0.10
        sol_usd=FixedPrice("100"),
        transport=api(1_500_000),  # 0.0015 SOL = $0.15
    )
    with pytest.raises(SpendRefusedError):
        await payer.check({"url": "https://shop.test"})
    assert rpc.sent == []


async def test_sol_is_refused_without_the_agents_own_price() -> None:
    rpc = FakeRpc()
    for price in (None, FixedPrice(None)):
        payer = Payer(
            api_url="http://api",
            agent=AGENT,
            rpc=rpc,
            network="solana-devnet",
            guard=SpendGuard(250_000, 2_500_000),
            sol_usd=price,
            transport=api(100_000),
        )
        with pytest.raises(PaymentFailedError, match="pay in USDC"):
            await payer.check({"url": "https://shop.test"})
    assert rpc.sent == []


# ── Paying in USDC ────────────────────────────────────────────────────────────

USDC = USDC_MINTS["solana-devnet"]


def usdc_requirement(amount: int = 150_000) -> dict[str, Any]:
    return {**requirement(amount), "asset": str(USDC)}


class TokenRpc(FakeRpc):
    def __init__(self, usdc: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.usdc = usdc

    async def token_balance(self, account: Pubkey) -> int:
        return self.usdc if account == associated_token_address(AGENT.pubkey(), USDC) else self.wsol


def offers(sol: int = 1_500_000, usdc: int = 150_000) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            required = {"x402Version": 2, "accepts": [requirement(sol), usdc_requirement(usdc)]}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(required)}, json=required)
        paid = json.loads(base64.b64decode(request.headers["PAYMENT-SIGNATURE"]))["accepted"]["asset"]
        settlement = {"success": True, "transaction": "5ig", "network": CAIP2["solana-devnet"]}
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": b64(settlement)}, json={"paid_in": paid})

    return httpx.MockTransport(handle)


def guard() -> SpendGuard:
    return SpendGuard(max_per_call=250_000, session_cap=2_500_000)


def usdc_payer(rpc: FakeRpc, spend: SpendGuard, **kwargs: Any) -> Payer:
    return Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=rpc,
        network="solana-devnet",
        guard=spend,
        sol_usd=FixedPrice("100"),
        transport=offers(**kwargs),
    )


async def test_pays_in_usdc_when_offered_and_held() -> None:
    spend = guard()
    rpc = TokenRpc(usdc=1_000_000)
    result = await usdc_payer(rpc, spend).check({"url": "https://shop.test", "tier": "deep"})
    assert result["verdict"] == {"paid_in": str(USDC)}
    assert result["paid"] == {"currency": "USDC", "amount": 150_000, "usd_micros": 150_000}
    assert result["paid_lamports"] == 0
    assert spend.spent == 150_000
    assert rpc.sent == []  # no wrapping for USDC


async def test_falls_back_to_sol_when_usdc_is_short() -> None:
    short = guard()
    assert (await usdc_payer(TokenRpc(usdc=100_000), short).check({"url": "u"}))["paid"]["currency"] == "SOL"
    assert short.spent == 150_000  # 0.0015 SOL at $100, valued by the agent itself


async def test_the_dollar_cap_applies_whatever_the_currency() -> None:
    with pytest.raises(SpendRefusedError):  # $0.30 in USDC: over the $0.25 cap, no SOL fallback around it
        await usdc_payer(TokenRpc(usdc=10**9), guard(), usdc=300_000).check({"url": "u"})
    with pytest.raises(SpendRefusedError):  # 0.003 SOL at $100 = $0.30, whatever the server claims
        await usdc_payer(TokenRpc(usdc=0), guard(), sol=3_000_000).check({"url": "u"})


async def test_prefer_sol_always_pays_in_sol() -> None:
    payer = usdc_payer(TokenRpc(usdc=10**9), guard())
    payer.prefer = "SOL"
    assert (await payer.check({"url": "u"}))["paid"]["currency"] == "SOL"


def test_usdc_payment_transfers_usdc_with_six_decimals() -> None:
    tx = build_payment(usdc_requirement(), AGENT, BLOCKHASH)
    transfer = tx.message.instructions[2]
    accounts = [tx.message.account_keys[i] for i in bytes(transfer.accounts)]
    source, mint, destination = (
        associated_token_address(AGENT.pubkey(), USDC),
        USDC,
        associated_token_address(TREASURY, USDC),
    )
    assert accounts[:3] == [source, mint, destination]
    assert bytes(transfer.data)[9] == 6
