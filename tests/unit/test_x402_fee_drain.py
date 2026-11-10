"""
The fee drain (H2): payments that could pass the simulation and fail on chain are refused, and what remains is
bounded by the failed-settlement breaker.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import (
    FakeRpc,
    configured,
    cpi_payment_instructions,
    expected,
    make_x402,
    payment_header,
    payment_instructions,
    sign_payment,
)
from unfret.main import app
from unfret.visitors import network_of, visitor_key
from unfret.x402.breaker import WINDOW_S, SettlementBreaker
from unfret.x402.payment import PaymentError
from unfret.x402.service import X402
from unfret.x402.solana import LIGHTHOUSE_PROGRAM, TransactionFailedError

HOT = Keypair()
CLIENT = Keypair()
COLD = Keypair().pubkey()
CLOCK = Pubkey.from_string("SysvarC1ock11111111111111111111111111111111")


def service(rpc: FakeRpc | None = None) -> X402:
    return make_x402(configured(HOT, COLD), rpc or FakeRpc())


def header(ixs: list[Instruction], amount: int = 100_000) -> str:
    return payment_header(sign_payment(ixs, HOT.pubkey(), [CLIENT]), expected(HOT.pubkey(), amount))


def precheck(x402: X402, ixs: list[Instruction], tier: str = "quick", amount: int = 100_000) -> None:
    x402.precheck(header(ixs, amount), tier, [expected(HOT.pubkey(), amount)])


def with_account(ixs: list[Instruction], program: Pubkey, account: Pubkey | None = None) -> list[Instruction]:
    extra = Instruction(program, b"\x00", [AccountMeta(account, False, False)] if account else [])
    return [*ixs[:3], extra, ixs[3]]


def test_a_slot_dependent_sysvar_is_refused() -> None:
    ixs = with_account(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), LIGHTHOUSE_PROGRAM, CLOCK)
    with pytest.raises(PaymentError, match="slot_dependent_sysvar_not_allowed"):
        precheck(service(), ixs)


def test_an_unknown_program_is_refused_before_simulating() -> None:
    ixs = with_account(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), Keypair().pubkey())
    with pytest.raises(PaymentError, match="program_not_allowed"):
        precheck(service(), ixs)


def test_lighthouse_assertions_and_squads_payments_are_allowed() -> None:
    precheck(service(), with_account(payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000), LIGHTHOUSE_PROGRAM))
    precheck(service(), cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000))


def test_address_lookup_tables_are_refused() -> None:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    table = AddressLookupTableAccount(Keypair().pubkey(), [ixs[2].accounts[0].pubkey])  # the source token account
    message = MessageV0.try_compile(HOT.pubkey(), ixs, [table], Hash.default())
    assert message.address_table_lookups
    signature = CLIENT.sign_message(to_bytes_versioned(message))
    tx = VersionedTransaction.populate(message, [Signature.default(), signature])
    with pytest.raises(PaymentError, match="address_lookup_tables_not_allowed"):
        service().precheck(payment_header(tx, expected(HOT.pubkey())), "quick", [expected(HOT.pubkey())])


def test_the_priority_fee_is_capped_at_five_percent_of_the_price() -> None:
    # quick at $100/SOL: 100_000 lamports, so at most 5_000 in priority fee; 200_000 units x 50_000 µL = 10_000.
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, cu_price=50_000)
    with pytest.raises(PaymentError, match="priority_fee_too_high"):
        precheck(service(), ixs)
    precheck(service(), payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000, cu_price=25_000))  # 5_000
    x402 = service()
    assert x402.max_priority_fee([expected(HOT.pubkey(), 1_500_000)]) == 75_000  # deep
    assert x402.max_priority_fee([]) == 5_000  # no wSOL quote: the fallback


async def test_a_program_running_by_cpi_must_be_allowed_too() -> None:
    rpc = FakeRpc()
    x402 = service(rpc)
    ixs = cpi_payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
    pre = x402.precheck(header(ixs), "quick", [expected(HOT.pubkey())])
    rpc.sim_inner = [0]  # index 0 is the hot wallet: not a program we allow
    with pytest.raises(PaymentError, match="program_not_allowed"):
        await x402.settle(pre)
    assert rpc.sent == []


# ── The breaker ───────────────────────────────────────────────────────────────


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_the_breaker_opens_per_payer_and_closes_after_the_window() -> None:
    clock = Clock()
    breaker = SettlementBreaker(per_payer=3, per_ip=5, global_limit=20, clock=clock)
    for _ in range(3):
        assert breaker.open_for("payer", "1.2.3.4") is None
        breaker.record_failure("payer", "1.2.3.4")
        clock.now += 10
    assert breaker.open_for("payer", "5.6.7.8") == pytest.approx(WINDOW_S - 30)  # wherever it pays from
    assert breaker.open_for("other", "5.6.7.8") is None
    clock.now += WINDOW_S
    assert breaker.open_for("payer", "1.2.3.4") is None


def test_the_breaker_counts_ipv6_per_64_and_globally() -> None:
    breaker = SettlementBreaker(per_payer=99, per_ip=2, global_limit=4, clock=Clock())
    breaker.record_failure("a", "2001:db8:1:2::1")
    breaker.record_failure("b", "2001:db8:1:2:ffff::9")
    assert breaker.open_for("c", "2001:db8:1:2::abcd") is not None  # the same /64
    assert breaker.open_for("c", "2001:db8:1:3::1") is None
    breaker.record_failure("c", "9.9.9.9")
    breaker.record_failure("d", "8.8.8.8")
    assert breaker.open_for("e", "7.7.7.7") is not None  # four failures in total
    assert breaker.global_failures == 4


def test_visitor_networks() -> None:
    assert network_of("2001:db8::1") == "2001:db8::/64"
    assert network_of("::ffff:1.2.3.4") == "1.2.3.4"
    assert network_of("1.2.3.4") == "1.2.3.4"
    assert network_of("unknown") == "unknown"
    assert visitor_key("2001:db8::1") == visitor_key("2001:db8::2") != visitor_key("1.2.3.4")


@pytest.fixture
async def failing() -> AsyncIterator[tuple[httpx.AsyncClient, X402]]:
    rpc = FakeRpc()
    rpc.fail_with = TransactionFailedError("transaction failed: {'InstructionError': [2, 'Custom']}")
    x402 = service(rpc)
    old = app.x402
    app.x402 = x402
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, x402
    finally:
        app.x402 = old


async def test_payments_pause_after_repeated_failed_settlements(
    failing: tuple[httpx.AsyncClient, X402], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, x402 = failing

    class Graph:
        async def ainvoke(self, *_: object, **__: object) -> dict[str, object]:
            raise AssertionError("no analysis without a settled payment")

    monkeypatch.setattr(app, "quick_graph", Graph())
    body = {"url": "https://shop.test/p/1", "page_content": "<h1>x</h1>", "tier": "quick", "policy": "balanced"}
    codes = []
    for i in range(4):
        ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), 100_000)
        ixs[3] = Instruction(ixs[3].program_id, f"nonce-{i}".encode(), [])  # a fresh memo: a new transaction
        response = await client.post("/v1/check", json=body, headers={"PAYMENT-SIGNATURE": header(ixs)})
        codes.append(response.status_code)
    assert codes == [402, 402, 402, 503]
    assert response.json()["error_code"] == "SETTLEMENTS_PAUSED"
    assert int(response.headers["Retry-After"]) > 0
    assert x402.breaker.global_failures == 3
