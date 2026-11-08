import base64
import json
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


def test_guard_refuses_over_per_call_max_and_session_cap() -> None:
    guard = SpendGuard(max_per_call=1_500_000, session_cap=2_000_000)
    with pytest.raises(SpendRefusedError, match="per-call"):
        guard.reserve(1_500_001)
    guard.reserve(1_500_000)
    with pytest.raises(SpendRefusedError, match="session cap"):
        guard.reserve(600_000)
    guard.release(1_500_000)
    guard.reserve(600_000)
    assert guard.spent == 600_000


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
    payer = Payer(api_url="http://api", agent=AGENT, rpc=rpc, network="solana-devnet", guard=SpendGuard(10**7, 10**8))

    await payer.ensure_wsol(100_000)

    (wrap,) = rpc.sent
    keys = wrap.message.account_keys
    system_ix = next(ix for ix in wrap.message.instructions if keys[ix.program_id_index] == SYSTEM_PROGRAM)
    assert int.from_bytes(bytes(system_ix.data)[4:12], "little") == 70_000


async def test_no_wrap_when_wsol_suffices_and_refusal_when_sol_is_short() -> None:
    rpc = FakeRpc(wsol=100_000)
    payer = Payer(api_url="http://api", agent=AGENT, rpc=rpc, network="solana-devnet", guard=SpendGuard(10**7, 10**8))
    await payer.ensure_wsol(100_000)
    assert rpc.sent == []

    poor = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=FakeRpc(wsol=0, sol=50_000),
        network="solana-devnet",
        guard=SpendGuard(10**7, 10**8),
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


async def test_check_pays_and_counts_spend() -> None:
    guard = SpendGuard(max_per_call=2_000_000, session_cap=20_000_000)
    payer = Payer(
        api_url="http://api", agent=AGENT, rpc=FakeRpc(), network="solana-devnet", guard=guard, transport=api(100_000)
    )

    result = await payer.check({"url": "https://shop.test"})

    assert result == {
        "status": 200,
        "transaction": "5ig",
        "paid_lamports": 100_000,
        "paid": {"currency": "SOL", "amount": 100_000},
        "verdict": {"action": "warn"},
    }
    assert guard.spent == 100_000


async def test_failed_settlement_releases_the_reservation() -> None:
    guard = SpendGuard(max_per_call=2_000_000, session_cap=20_000_000)
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=FakeRpc(),
        network="solana-devnet",
        guard=guard,
        transport=api(100_000, settle=False),
    )

    result = await payer.check({"url": "https://shop.test"})

    assert (result["status"], result["paid_lamports"], guard.spent) == (402, 0, 0)


async def test_overpriced_check_is_refused_before_paying() -> None:
    rpc = FakeRpc()
    payer = Payer(
        api_url="http://api",
        agent=AGENT,
        rpc=rpc,
        network="solana-devnet",
        guard=SpendGuard(max_per_call=1_000_000, session_cap=20_000_000),
        transport=api(1_500_000),
    )
    with pytest.raises(SpendRefusedError):
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
    return SpendGuard(
        max_per_call=2_000_000, session_cap=20_000_000, usdc_max_per_call=250_000, usdc_session_cap=2_500_000
    )


def usdc_payer(rpc: FakeRpc, spend: SpendGuard, **kwargs: Any) -> Payer:
    return Payer(
        api_url="http://api", agent=AGENT, rpc=rpc, network="solana-devnet", guard=spend, transport=offers(**kwargs)
    )


async def test_pays_in_usdc_when_offered_and_held() -> None:
    spend = guard()
    rpc = TokenRpc(usdc=1_000_000)
    result = await usdc_payer(rpc, spend).check({"url": "https://shop.test", "tier": "deep"})
    assert result["verdict"] == {"paid_in": str(USDC)}
    assert result["paid"] == {"currency": "USDC", "amount": 150_000}
    assert result["paid_lamports"] == 0
    assert (spend.usdc_spent, spend.spent) == (150_000, 0)
    assert rpc.sent == []  # no wrapping for USDC


async def test_falls_back_to_sol_when_usdc_is_short_or_over_its_cap() -> None:
    short = guard()
    assert (await usdc_payer(TokenRpc(usdc=100_000), short).check({"url": "u"}))["paid"]["currency"] == "SOL"
    assert (short.usdc_spent, short.spent) == (0, 1_500_000)
    capped = guard()
    result = await usdc_payer(TokenRpc(usdc=10**9), capped, usdc=300_000).check({"url": "u"})  # over the $0.25 cap
    assert result["paid"]["currency"] == "SOL"


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


def test_usdc_guard_is_separate_from_the_sol_guard() -> None:
    spend = guard()
    spend.reserve(250_000, "USDC")
    with pytest.raises(SpendRefusedError, match="per-call"):
        spend.reserve(250_001, "USDC")
    spend.reserve(2_000_000, "SOL")
    assert (spend.usdc_spent, spend.spent) == (250_000, 2_000_000)
