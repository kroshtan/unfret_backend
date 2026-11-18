"""
x402 on Base, the hot wallet: its key, payTo derived from it, the one transaction it signs (a USDC sweep to cold),
when sweeps run, their gas, failures and backoff, and what the outflow audit makes of the hot wallet's nonce and code.

Every key here is a throwaway made for the test run; fees and the gas estimate are recorded Base Sepolia answers.
"""

import json
import math
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from pydantic import SecretStr

from tests.unit.x402_base_helpers import (
    COLD,
    PAY_TO,
    RECORDED_RPC,
    SEPOLIA_USDC,
    FakeBaseRpc,
    FakeFacilitator,
    base_settings,
    hot_key,
    make_base,
    outflow_log,
    sweep_receipt,
)
from unfret.config import Settings
from unfret.x402.base import BaseSweepError, BaseX402
from unfret.x402.evm import TRANSFER_TOPIC, keccak256, parse_address
from unfret.x402.evm_wallet import (
    BaseHotWallet,
    BaseSweep,
    build_usdc_sweep,
    check_sweep,
    load_hot_key,
    transfer_data,
)
from unfret.x402.solana import RpcError

THIEF = Account.create().address
FORMER = Account.create().address
CUT_OFF = "2026-11-16T06:00:00Z"
CUT_OFF_TS = int(datetime(2026, 11, 16, 6, tzinfo=UTC).timestamp())
CAPS: dict[str, Any] = {"chain_id": 84532, "max_gas": 120_000, "max_fee": 10**9}


@pytest.fixture
def rpc() -> FakeBaseRpc:
    return FakeBaseRpc()


@pytest.fixture
def facilitator() -> FakeFacilitator:
    return FakeFacilitator()


@pytest.fixture
def base(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> BaseX402:
    return make_base(base_settings(), rpc, facilitator)


def sweep(amount: int = 5_000_000, **overrides: Any) -> BaseSweep:
    values: dict[str, Any] = {
        "chain_id": 84532,
        "nonce": 0,
        "amount": amount,
        "gas": 60_000,
        "max_fee_per_gas": 11_000_000,
        "max_priority_fee_per_gas": 1_000_000,
    }
    values.update(overrides)
    return build_usdc_sweep(COLD.address, SEPOLIA_USDC, **values)


def hot() -> BaseHotWallet:
    return BaseHotWallet(load_hot_key(hot_key()), COLD.address, SEPOLIA_USDC, **CAPS)


# --- the key and payTo ---------------------------------------------------------------------------------------------


def test_the_key_loads_with_or_without_0x() -> None:
    assert load_hot_key(hot_key()).address == PAY_TO.address
    assert load_hot_key(hot_key()[2:] + "\n").address == PAY_TO.address


@pytest.mark.parametrize("secret", ["", "not-a-key", "0x" + "00" * 32, "0x" + "ff" * 32, "0x" + "ab" * 31, "[1, 2]"])
def test_a_bad_key_is_refused_without_echoing_it(secret: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid key$") as refused:
        load_hot_key(secret)
    assert refused.value.__cause__ is None  # nothing chained that could carry the secret


def test_the_hot_wallet_shows_its_address_only() -> None:
    wallet = hot()
    assert repr(wallet) == f"BaseHotWallet({PAY_TO.address})"
    assert hot_key()[2:] not in repr(wallet) + str(vars(wallet).get("address"))


def test_the_hot_wallet_cant_be_cold() -> None:
    with pytest.raises(ValueError, match="cold"):
        BaseHotWallet(load_hot_key(hot_key()), PAY_TO.address.lower(), SEPOLIA_USDC, **CAPS)


async def test_pay_to_is_the_keys_address(base: BaseX402) -> None:
    assert base.configured
    assert base.pay_to == PAY_TO.address
    assert base.requirements("quick")["payTo"] == PAY_TO.address
    assert base.cold == COLD.address


def test_a_matching_pay_to_is_accepted_as_a_check(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> None:
    assert make_base(base_settings(base_pay_to=PAY_TO.address.lower()), rpc, facilitator).configured


def test_the_key_never_reaches_the_log(capsys: pytest.CaptureFixture[str], rpc: FakeBaseRpc) -> None:
    for key in (hot_key(), "0x" + "ff" * 32, hot_key()[:-1] + "z"):
        BaseX402(base_settings(x402_base_hot_key=SecretStr(key), base_pay_to=THIEF), rpc=rpc)
    out = capsys.readouterr().out
    assert "x402_base_misconfigured" in out
    assert hot_key()[2:].lower() not in out.lower() and "ff" * 32 not in out


def test_the_summary_says_off_without_the_key_or_cold() -> None:
    assert Settings(x402_base_enabled=True).summary()["x402_base"] == "off"
    assert Settings(x402_base_enabled=True, x402_base_hot_key=SecretStr(hot_key())).summary()["x402_base"] == "off"
    assert base_settings().summary()["x402_base"] == "base-sepolia"
    assert hot_key()[2:] not in json.dumps(base_settings().summary())


# --- the one transaction it signs ----------------------------------------------------------------------------------


def test_a_signed_sweep_is_usdc_transfer_to_cold_and_nothing_else() -> None:
    signed = hot().sign(sweep(5_000_000, nonce=7))
    raw = bytes.fromhex(signed.raw[2:])
    assert Account.recover_transaction(signed.raw) == PAY_TO.address
    assert signed.hash == "0x" + keccak256(raw).hex()
    tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    assert (tx["type"], tx["chainId"], tx["nonce"], tx["value"]) == (2, 84532, 7, 0)
    assert parse_address("0x" + tx["to"].hex().removeprefix("0x")) == SEPOLIA_USDC
    assert "0x" + tx["data"].hex().removeprefix("0x") == transfer_data(COLD.address, 5_000_000)
    assert (signed.nonce, signed.amount) == (7, 5_000_000)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"to": THIEF}, "USDC contract"),
        ({"value": 1}, "ETH"),
        ({"data": transfer_data(THIEF, 5_000_000)}, "transfer"),
        ({"data": transfer_data(COLD.address, 0)}, "nothing"),
        ({"data": "0x095ea7b3" + transfer_data(COLD.address, 5)[10:]}, "transfer"),  # approve(cold, 5)
        ({"data": transfer_data(COLD.address, 5) + "00"}, "transfer"),
        ({"accessList": [{"address": THIEF, "storageKeys": []}]}, "access list"),
        ({"chainId": 8453}, "chain"),
        ({"type": 0}, "type"),
        ({"gas": 120_001}, "gas"),
        ({"maxFeePerGas": 10**9 + 1, "maxPriorityFeePerGas": 1}, "fee"),
        ({"maxPriorityFeePerGas": 12_000_000}, "fee"),  # tip above the cap
        ({"nonce": -1}, "numbers"),
        ({"value": True}, "numbers"),
        ({"gasPrice": 1}, "fields"),
    ],
)
def test_the_signer_rechecks_every_field(change: dict[str, Any], message: str) -> None:
    item = sweep()
    item.tx.update(change)  # a sweep altered after it was built
    with pytest.raises(PermissionError, match=message):
        hot().sign(item)
    with pytest.raises(PermissionError, match=message):
        check_sweep(item.tx, COLD.address, SEPOLIA_USDC, chain_id=84532, max_gas=120_000, max_fee=10**9)


def test_only_sweeps_can_be_signed() -> None:
    with pytest.raises(TypeError):
        BaseSweep(tx=sweep().tx, amount=1, _token=object())
    with pytest.raises(PermissionError):
        hot().sign(sweep().tx)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="positive"):
        sweep(0)


# --- when it sweeps ------------------------------------------------------------------------------------------------


async def test_nothing_is_swept_below_the_threshold(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 4_999_999
    assert await base.sweep_once() is None
    assert rpc.sent == []


async def test_at_the_threshold_everything_goes_to_cold(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance, rpc.nonce = 5_000_000, 3
    tx = await base.sweep_once()
    [sent] = rpc.sent
    assert tx == sent["hash"]
    assert (sent["from"], sent["amount"], sent["nonce"]) == (PAY_TO.address, 5_000_000, 3)
    assert rpc.balance == 0 and rpc.nonce == 4
    assert await base.sweep_once() is None  # nothing left


async def test_dust_waits_a_day(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> None:
    base = make_base(base_settings(base_usdc_sweep_max_interval_s=60.0), rpc, facilitator)
    rpc.balance = 500_000
    assert await base.sweep_once() is None
    base._last_sweep = time.monotonic() - 61
    assert await base.sweep_once() is not None
    rpc.balance = 499_999  # under the dust amount: never on its own
    base._last_sweep = time.monotonic() - 61
    assert await base.sweep_once() is None


async def test_gas_is_the_networks_fee_with_a_margin(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 5_000_000
    await base.sweep_once()
    [sent] = rpc.sent
    base_fee = int(RECORDED_RPC["eth_getBlockByNumber"]["baseFeePerGas"], 16)
    tip = int(RECORDED_RPC["eth_maxPriorityFeePerGas"], 16)
    estimate = int(RECORDED_RPC["eth_estimateGas_usdc_transfer"], 16)
    assert (sent["maxFeePerGas"], sent["maxPriorityFeePerGas"]) == (2 * base_fee + tip, tip)
    assert sent["gas"] == math.ceil(estimate * 1.3)


async def test_without_eth_for_gas_it_waits(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 5_000_000
    rpc.eth = 1_000
    assert await base.sweep_once() is None
    assert rpc.sent == []
    assert "fee_reserve_low" in (await base.health()).alerts


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda r: setattr(r, "base_fee_wei", 10**9), "gas costs"),  # above base_max_fee_per_gas_wei
        (lambda r: setattr(r, "gas_estimate", 120_001), "gas"),  # something odd: a transfer doesn't need that
        (lambda r: setattr(r, "pending", r.nonce + 1), "unmined"),  # one at a time
    ],
)
async def test_it_refuses_to_sweep(base: BaseX402, rpc: FakeBaseRpc, setup: Any, message: str) -> None:
    rpc.balance = 5_000_000
    setup(rpc)
    with pytest.raises(BaseSweepError, match=message):
        await base.sweep_once()
    assert rpc.sent == []


async def test_a_sweep_not_mined_in_time_blocks_the_next(
    base: BaseX402, rpc: FakeBaseRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("unfret.x402.base._RECEIPT_POLL_S", 0.0)
    base.settings.x402_confirm_timeout_s = 0.01
    rpc.balance, rpc.mine = 5_000_000, False
    tx = await base.sweep_once()
    assert tx == rpc.sent[0]["hash"]
    with pytest.raises(BaseSweepError, match="unmined"):
        await base.sweep_once()
    assert len(rpc.sent) == 1


async def test_a_resent_sweep_the_node_already_has_is_fine(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 5_000_000
    real = FakeBaseRpc.send_raw

    async def already_known(self: FakeBaseRpc, raw: str) -> str:
        await real(self, raw)
        raise RpcError("eth_sendRawTransaction: already known")

    rpc.send_raw = already_known.__get__(rpc)  # type: ignore[method-assign]
    assert await base.sweep_once() == rpc.sent[0]["hash"]


async def test_a_refused_broadcast_is_an_error(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 5_000_000
    rpc.send_error = RpcError("eth_sendRawTransaction: insufficient funds for gas * price + value")
    with pytest.raises(RpcError, match="insufficient"):
        await base.sweep_once()


async def test_the_receipt_is_checked_on_chain(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance, rpc.revert = 5_000_000, True
    with pytest.raises(BaseSweepError, match="reverted"):
        await base.sweep_once()
    rpc.revert = False
    real = FakeBaseRpc.send_raw

    async def elsewhere(self: FakeBaseRpc, raw: str) -> str:
        tx = await real(self, raw)
        self.receipts[tx] = sweep_receipt(tx, 5_000_000, block=self.head, to=THIEF)  # a receipt that lies
        return tx

    rpc.send_raw = elsewhere.__get__(rpc)  # type: ignore[method-assign]
    rpc.balance = 5_000_000
    with pytest.raises(BaseSweepError, match="didn't move"):
        await base.sweep_once()


async def test_failures_back_off_and_alert_then_recover(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 5_000_000
    rpc.send_error = RpcError("eth_sendRawTransaction: nonce too low")
    await base.sweep_step()
    assert base._sweep_failures == 1
    first = base._next_sweep - time.monotonic()
    assert first == pytest.approx(base.settings.sweep_interval_s * 2, abs=1)
    await base.sweep_step()  # still backing off: not even tried
    assert base._sweep_failures == 1
    for failures in (2, 3):
        base._next_sweep = 0.0
        await base.sweep_step()
        assert base._sweep_failures == failures
    assert base._next_sweep - time.monotonic() == pytest.approx(base.settings.sweep_interval_s * 8, abs=1)
    base._balance = None
    assert "sweep_failing" in (await base.health()).alerts
    rpc.send_error = None
    base._next_sweep = 0.0
    await base.sweep_step()
    assert base._sweep_failures == 0 and rpc.balance == 0
    base._balance = None
    assert "sweep_failing" not in (await base.health()).alerts


async def test_the_backoff_stops_at_an_hour(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance, rpc.down = 5_000_000, True
    base._sweep_failures = 40
    await base.sweep_step()
    assert base._next_sweep - time.monotonic() == pytest.approx(3600, abs=1)


# --- the audit: the nonce, code, former cold addresses -------------------------------------------------------------


async def test_the_audits_own_sweeps_explain_the_nonce(base: BaseX402, rpc: FakeBaseRpc) -> None:
    await base.audit_outflows()  # first run: the baseline
    rpc.head += 10
    rpc.balance = 5_000_000
    await base.sweep_once()  # its Transfer log lands at the head, the nonce moves by one
    rpc.head += 10
    assert await base.audit_outflows() == []
    assert base._nonce == 1


async def test_a_transaction_the_server_didnt_sign_alarms(base: BaseX402, rpc: FakeBaseRpc) -> None:
    await base.audit_outflows()
    rpc.head += 10
    rpc.nonce += 1  # e.g. the key sent ETH somewhere: no USDC log, only the nonce shows it
    issues = await base.audit_outflows()
    assert len(issues) == 1 and "signed 1 transaction(s) that weren't USDC sweeps to cold" in issues[0]
    health = await base.health()
    assert (health.status, health.alerts) == ("compromise_suspected", ("outflow_to_non_cold",))
    assert await base.accepts("quick") == []


async def test_a_sweep_to_cold_signed_elsewhere_is_harmless_but_a_second_transaction_isnt(
    base: BaseX402, rpc: FakeBaseRpc
) -> None:
    await base.audit_outflows()
    rpc.head += 10
    tx = "0x" + "5e" * 32
    rpc.receipts[tx] = sweep_receipt(tx, 1_000_000, block=rpc.head - 6)
    rpc.logs_by_block[rpc.head - 6] = rpc.receipts[tx]["logs"]
    rpc.nonce += 2  # the sweep, and one more
    [issue] = await base.audit_outflows()
    assert "signed 1 transaction(s)" in issue


async def test_a_reverted_sweep_of_this_process_uses_a_nonce_without_alarm(base: BaseX402, rpc: FakeBaseRpc) -> None:
    await base.audit_outflows()
    rpc.head += 10
    rpc.balance, rpc.revert = 5_000_000, True
    with pytest.raises(BaseSweepError):
        await base.sweep_once()
    rpc.head += 10
    assert await base.audit_outflows() == []


async def test_code_at_the_hot_wallet_alarms(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.code_value = "0xef0100" + THIEF[2:].lower()  # an EIP-7702 delegation
    [issue] = await base.audit_outflows()
    assert "EIP-7702" in issue
    assert [flag.signature for flag in base.flagged()] == ["hot wallet code"]
    rpc.head += 10
    assert await base.audit_outflows() == []  # the same finding isn't raised twice
    assert len(base.flagged()) == 1


async def test_the_cursor_carries_the_nonce(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> None:
    first = make_base(base_settings(), rpc, facilitator)
    await first.audit_outflows()
    assert (first._cursor, first._nonce) == (rpc.head - 5, 0)


def former_settings(**overrides: Any) -> Settings:
    return base_settings(base_former_cold_addresses=FORMER, base_former_cold_until=CUT_OFF, **overrides)


@pytest.mark.parametrize(
    ("block_time", "flagged"),
    [
        (CUT_OFF_TS - 60, False),  # a sweep of the time it was cold
        (CUT_OFF_TS + 60, True),  # after the cut-off: not ours
        (None, True),  # no block time: can't tell, so it alarms
    ],
)
async def test_former_cold_addresses_count_only_before_the_cut_off(
    rpc: FakeBaseRpc, facilitator: FakeFacilitator, block_time: int | None, flagged: bool
) -> None:
    base = make_base(former_settings(), rpc, facilitator)
    await base.audit_outflows()
    rpc.head += 10
    tx = "0x" + "f0" * 32
    extra: dict[str, Any] = {"blockNumber": hex(rpc.head - 6)}
    if block_time is not None:
        extra["blockTimestamp"] = hex(block_time)
    receipt = sweep_receipt(tx, 2_000_000, block=rpc.head - 6, to=FORMER)
    rpc.receipts[tx] = receipt
    rpc.logs_by_block[rpc.head - 6] = [outflow_log(TRANSFER_TOPIC, PAY_TO.address, FORMER, 2_000_000, tx=tx, **extra)]
    rpc.nonce += 1
    issues = await base.audit_outflows()
    if flagged:
        assert issues[0] == (
            f"USDC transfer of 2000000 from the hot wallet to {FORMER.lower()} "
            "(a former cold address, after its cut-off)"
        )
    else:
        assert issues == []


async def test_a_former_cold_without_a_timestamp_in_the_log_is_looked_up(
    rpc: FakeBaseRpc, facilitator: FakeFacilitator
) -> None:
    base = make_base(former_settings(), rpc, facilitator)
    await base.audit_outflows()
    rpc.head += 10
    rpc.block_times[rpc.head - 6] = CUT_OFF_TS - 600
    rpc.logs_by_block[rpc.head - 6] = [
        outflow_log(TRANSFER_TOPIC, PAY_TO.address, FORMER, 2_000_000, blockNumber=hex(rpc.head - 6))
    ]
    assert await base.audit_outflows() == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"base_former_cold_addresses": "not-an-address"}, "is not an EVM address"),
        ({"base_former_cold_addresses": FORMER}, "needs base_former_cold_until"),
        ({"base_former_cold_addresses": FORMER, "base_former_cold_until": "2026-11-16T06:00:00"}, "time zone"),
        (
            {"base_former_cold_addresses": COLD.address.lower(), "base_former_cold_until": CUT_OFF},
            "can't also be a former cold address",
        ),
        ({"base_usdc_sweep_dust_units": 5_000_001}, "dust"),
    ],
)
def test_contradictory_base_settings_are_refused(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        base_settings(**overrides)


def test_former_cold_addresses_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNFRET_BASE_FORMER_COLD_ADDRESSES", f" {FORMER.lower()}, ,{FORMER}")
    monkeypatch.setenv("UNFRET_BASE_FORMER_COLD_UNTIL", "")
    monkeypatch.setenv("UNFRET_BASE_FORMER_COLD_UNTIL", CUT_OFF)
    settings = Settings()
    assert settings.base_former_cold_addresses == [FORMER]
    assert settings.base_former_cold_until is not None and settings.base_former_cold_until.timestamp() == CUT_OFF_TS
