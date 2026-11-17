"""x402 on Base: configuration, requirements, the local checks, settlement checked on chain, and the outflow audit."""

import base64
import time
from typing import Any

import httpx
import pytest
from eth_account import Account
from pydantic import SecretStr
from solders.keypair import Keypair

from tests.unit.x402_base_helpers import (
    COLD,
    NETWORK,
    PAY_TO,
    PAYER,
    RECORDED,
    SEPOLIA_USDC,
    FakeBaseRpc,
    FakeFacilitator,
    base_payload,
    base_settings,
    make_base,
    outflow_log,
    settled_receipt,
)
from tests.unit.x402_helpers import FakeRpc, configured, make_x402
from unfret.config import Settings
from unfret.x402.base import (
    BaseSettlementMismatchError,
    BaseX402,
    SettlementRefusedError,
    SettlementUnknownError,
)
from unfret.x402.evm import APPROVAL_TOPIC, TRANSFER_TOPIC, USDC
from unfret.x402.facilitator import FacilitatorError
from unfret.x402.payment import PaymentError

THIEF = Account.create().address
RESOURCE = {"url": "https://unfret.test/v1/check", "description": "Unfret", "mimeType": "application/json"}
TX = "0x" + "12" * 32


@pytest.fixture
def rpc() -> FakeBaseRpc:
    return FakeBaseRpc()


@pytest.fixture
def facilitator() -> FakeFacilitator:
    return FakeFacilitator()


@pytest.fixture
def base(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> BaseX402:
    return make_base(base_settings(), rpc, facilitator)


def cdp_secret() -> str:
    keypair = Keypair()
    return base64.b64encode(bytes(keypair)).decode()  # seed and public key, as CDP shows an Ed25519 secret


# --- configuration -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({}, "off"),  # the default: off
        ({"x402_base_enabled": True}, "off"),  # enabled but no payTo
        ({"base_pay_to": "0x" + "11" * 20}, "off"),  # payTo but not enabled
    ],
)
async def test_off_unless_enabled_with_a_pay_to(overrides: dict[str, Any], status: str) -> None:
    service = BaseX402(Settings(**overrides), rpc=FakeBaseRpc())
    assert (await service.health()).status == status
    assert not service.configured
    assert await service.accepts("quick") == []


async def test_mainnet_is_refused_unless_allowed(facilitator: FakeFacilitator) -> None:
    refused = make_base(base_settings(base_network="base-mainnet"), FakeBaseRpc(), facilitator)
    assert (await refused.health()).status == "mainnet_refused"
    allowed = make_base(
        base_settings(base_network="base-mainnet", x402_base_allow_mainnet=True), FakeBaseRpc(), facilitator
    )
    assert (await allowed.health()).status == "ok"
    assert allowed.network == "eip155:8453"
    assert allowed.usdc == USDC["base-mainnet"]
    assert allowed.requirements("quick")["extra"]["name"] == "USD Coin"  # mainnet USDC's EIP-712 name


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_pay_to": "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAeD"},  # one letter's case off: a typo
        {"base_pay_to": "0x" + "00" * 20},
        {"base_pay_to": "not-an-address"},
        {"base_cold_address": "0x1234"},
        # Coinbase's facilitator without a key, or with an ECDSA (PEM) key instead of Ed25519.
        {"base_facilitator_url": "https://api.cdp.coinbase.com/platform/v2/x402"},
        {
            "base_facilitator_url": "https://api.cdp.coinbase.com/platform/v2/x402",
            "cdp_api_key_id": "key",
            "cdp_api_key_secret": SecretStr("-----BEGIN " + "EC PRIVATE KEY-----\nMHc...\n-----END EC PRIV..."),
        },
    ],
)
async def test_misconfiguration_leaves_base_off(overrides: dict[str, Any]) -> None:
    service = BaseX402(base_settings(**overrides), rpc=FakeBaseRpc())
    assert (await service.health()).status == "misconfigured"
    assert await service.accepts("deep") == []


def test_the_cdp_facilitator_is_used_with_a_key() -> None:
    service = BaseX402(
        base_settings(
            base_facilitator_url="https://api.cdp.coinbase.com/platform/v2/x402",
            cdp_api_key_id="organizations/o/apiKeys/k",
            cdp_api_key_secret=SecretStr(cdp_secret()),
        ),
        rpc=FakeBaseRpc(),
    )
    assert service.configured
    assert service.facilitator is not None and service.facilitator.url.endswith("/platform/v2/x402")


# --- requirements --------------------------------------------------------------------------------------------------


async def test_requirements_are_exact_usd_in_usdc_on_sepolia(base: BaseX402) -> None:
    quick, deep = base.requirements("quick"), base.requirements("deep")
    assert (quick["amount"], deep["amount"]) == ("10000", "150000")  # $0.01 and $0.15, 6 decimals
    assert quick["network"] == NETWORK == "eip155:84532"
    assert quick["asset"] == SEPOLIA_USDC
    assert quick["payTo"] == PAY_TO.address
    assert quick["scheme"] == "exact"
    assert (quick["extra"]["name"], quick["extra"]["version"], quick["extra"]["decimals"]) == ("USDC", "2", 6)
    assert "paymentFlow" not in quick["extra"]  # x402's default flow for EVM: authorization
    assert quick["maxTimeoutSeconds"] >= 120


async def test_base_prices_match_solana_usdc(base: BaseX402) -> None:
    solana = make_x402(configured(Keypair(), Keypair().pubkey()), FakeRpc())
    for tier in ("quick", "deep"):
        assert int(base.requirements(tier)["amount"]) == await solana.price(tier, "USDC")


# --- local checks --------------------------------------------------------------------------------------------------


def test_a_good_payment_passes_the_local_checks(base: BaseX402) -> None:
    option = base.requirements("quick")
    payload = base_payload(option)
    pre = base.precheck(payload, [option])
    assert pre.payer == PAYER.address.lower()
    assert pre.amount == 10_000
    assert pre.key == f"{NETWORK}:{PAYER.address.lower()}:{payload['payload']['authorization']['nonce']}"


def test_addresses_match_in_any_case(base: BaseX402) -> None:
    option = base.requirements("quick")
    accepted = {**option, "payTo": option["payTo"].lower(), "asset": option["asset"].upper().replace("0X", "0x")}
    base.precheck(base_payload(accepted, to=PAY_TO.address.lower()), [option])


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda p: p["accepted"].update(amount="9999"), "requirements_mismatch"),
        (lambda p: p["accepted"].update(network="eip155:8453"), "requirements_mismatch"),
        (lambda p: p["accepted"].update(asset=USDC["base-mainnet"]), "requirements_mismatch"),
        (lambda p: p["accepted"].update(payTo=THIEF), "requirements_mismatch"),
        (lambda p: p["accepted"].update(scheme="upto"), "requirements_mismatch"),
        (lambda p: p.update(x402Version=1), "unsupported_x402_version"),
        (lambda p: p["accepted"]["extra"].update(assetTransferMethod="permit2"), "unsupported_asset_transfer_method"),
        (lambda p: p["payload"]["authorization"].update(to=THIEF), "invalid_exact_evm_payload_recipient_mismatch"),
        (
            lambda p: p["payload"]["authorization"].update(value="10001"),
            "invalid_exact_evm_payload_authorization_value_mismatch",
        ),
        (
            lambda p: p["payload"]["authorization"].update(value="1e4"),
            "invalid_exact_evm_payload_authorization_value_mismatch",
        ),
        (lambda p: p["payload"].update(signature="0x1234"), "invalid_exact_evm_payload_signature"),
        (
            lambda p: p["payload"]["authorization"].update(validAfter=str(int(time.time()) + 3600)),
            "invalid_exact_evm_payload_authorization_valid_after",
        ),
        (
            # Expiring before the analysis and the settlement can be done.
            lambda p: p["payload"]["authorization"].update(validBefore=str(int(time.time()) + 30)),
            "invalid_exact_evm_payload_authorization_valid_before",
        ),
        (lambda p: p["payload"]["authorization"].update({"from": PAY_TO.address}), "payer_is_pay_to"),
    ],
)
def test_the_local_checks_refuse(base: BaseX402, change: Any, reason: str) -> None:
    option = base.requirements("quick")
    payload = base_payload({**option, "extra": dict(option["extra"])})
    change(payload)
    with pytest.raises(PaymentError) as caught:
        base.precheck(payload, [option])
    assert caught.value.reason == reason
    assert not caught.value.malformed


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p.update(payload="nope"),
        lambda p: p["payload"].pop("authorization"),
        lambda p: p["payload"]["authorization"].update(nonce="0x12"),
        lambda p: p["payload"]["authorization"].update({"from": "alice"}),
        lambda p: p["payload"]["authorization"].pop("validBefore"),
    ],
)
def test_malformed_payloads_are_400s(base: BaseX402, change: Any) -> None:
    option = base.requirements("quick")
    payload = base_payload(option)
    change(payload)
    with pytest.raises(PaymentError) as caught:
        base.precheck(payload, [option])
    assert caught.value.malformed


# --- facilitator: verify and settle --------------------------------------------------------------------------------


async def test_verify_sends_unfrets_own_resource_and_extensions(base: BaseX402, facilitator: FakeFacilitator) -> None:
    option = base.requirements("quick")
    pre = base.precheck(base_payload(option), [option])
    await base.verify(pre, RESOURCE, {"bazaar": {"info": {"ours": True}}})
    (body,) = facilitator.calls("verify")
    assert body["x402Version"] == 2
    assert body["paymentRequirements"] == option  # ours, never the client's copy
    assert body["paymentPayload"]["resource"] == RESOURCE  # not what the client echoed
    assert body["paymentPayload"]["extensions"] == {"bazaar": {"info": {"ours": True}}}
    assert body["paymentPayload"]["payload"] == pre.payload["payload"]


@pytest.mark.parametrize(
    ("recorded", "reason"),
    [
        ("verify_insufficient_balance", "invalid_exact_evm_insufficient_balance"),
        ("verify_bad_signature", "invalid_exact_evm_signature"),
    ],
)
async def test_verify_refusals_carry_the_facilitators_reason(
    base: BaseX402, facilitator: FakeFacilitator, recorded: str, reason: str
) -> None:
    option = base.requirements("quick")
    pre = base.precheck(base_payload(option), [option])
    facilitator.verify = {**RECORDED[recorded], "payer": PAYER.address}
    with pytest.raises(PaymentError) as caught:
        await base.verify(pre, RESOURCE, {})
    assert caught.value.reason == reason


async def test_verify_refuses_another_payer(base: BaseX402, facilitator: FakeFacilitator) -> None:
    option = base.requirements("quick")
    pre = base.precheck(base_payload(option), [option])
    facilitator.verify = {"isValid": True, "payer": THIEF}
    with pytest.raises(PaymentError, match="payer_mismatch"):
        await base.verify(pre, RESOURCE, {})


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, text="<html>unauthorized</html>"),
        httpx.Response(500, json={"errorMessage": "boom"}),
        httpx.ConnectTimeout("slow"),
    ],
)
async def test_verify_without_an_answer_is_a_facilitator_error(
    base: BaseX402, facilitator: FakeFacilitator, answer: Any
) -> None:
    option = base.requirements("quick")
    pre = base.precheck(base_payload(option), [option])
    facilitator.verify = answer
    with pytest.raises(FacilitatorError):
        await base.verify(pre, RESOURCE, {})


async def test_a_400_with_a_verify_body_is_an_answer(base: BaseX402, facilitator: FakeFacilitator) -> None:
    option = base.requirements("quick")
    pre = base.precheck(base_payload(option), [option])
    facilitator.verify = httpx.Response(400, json={"isValid": False, "invalidReason": "invalid_payload"})
    with pytest.raises(PaymentError, match="invalid_payload"):
        await base.verify(pre, RESOURCE, {})


def receipt_for(base: BaseX402, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    auth = payload["payload"]["authorization"]
    return settled_receipt(auth["from"], base.pay_to, int(auth["value"]), auth["nonce"].lower(), **kwargs)


async def test_a_settlement_is_checked_on_base(base: BaseX402, rpc: FakeBaseRpc) -> None:
    option = base.requirements("deep")
    payload = base_payload(option)
    pre = base.precheck(payload, [option])
    rpc.receipts[TX] = receipt_for(base, payload)
    settlement = await base.settle(pre, RESOURCE, {})
    assert (settlement.transaction, settlement.payer, settlement.amount) == (TX, PAYER.address.lower(), 150_000)
    assert base.flagged() == []


@pytest.mark.parametrize(
    ("tamper", "detail"),
    [
        (lambda r: r.update(status="0x0"), "reverted"),
        (lambda r: r["logs"][1].update(data="0x" + (1).to_bytes(32, "big").hex()), "no USDC transfer"),
        (lambda r: r["logs"][1].update(address="0x" + "99" * 20), "no USDC transfer"),  # another token
        (lambda r: r["logs"].pop(0), "authorization wasn't used"),
    ],
)
async def test_a_settlement_that_doesnt_show_on_chain_is_a_mismatch(
    base: BaseX402, rpc: FakeBaseRpc, tamper: Any, detail: str
) -> None:
    option = base.requirements("quick")
    payload = base_payload(option)
    pre = base.precheck(payload, [option])
    receipt = receipt_for(base, payload)
    tamper(receipt)
    rpc.receipts[TX] = receipt
    with pytest.raises(BaseSettlementMismatchError, match=detail):
        await base.settle(pre, RESOURCE, {})
    assert (await base.health()).alerts == ("post_settlement_mismatch",)


async def test_without_a_receipt_the_authorization_state_decides(
    base: BaseX402, rpc: FakeBaseRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("unfret.x402.base._RECEIPT_RETRIES", 1)
    option = base.requirements("quick")
    payload = base_payload(option)
    pre = base.precheck(payload, [option])
    with pytest.raises(BaseSettlementMismatchError, match="unused on chain"):
        await base.settle(pre, RESOURCE, {})  # the facilitator claimed a settlement that didn't happen
    rpc.used.add((pre.payer, pre.nonce))
    assert (await base.settle(pre, RESOURCE, {})).transaction == TX


async def test_settlement_refusals_and_pending(base: BaseX402, facilitator: FakeFacilitator) -> None:
    option = base.requirements("quick")
    pre = base.precheck(base_payload(option), [option])
    facilitator.settle = RECORDED["settle_insufficient_balance"]
    with pytest.raises(SettlementRefusedError) as refused:
        await base.settle(pre, RESOURCE, {})
    assert refused.value.reason == "invalid_exact_evm_insufficient_balance"
    facilitator.settle = {"success": False, "errorReason": "settlement_pending", "transaction": TX, "network": NETWORK}
    with pytest.raises(SettlementUnknownError) as pending:
        await base.settle(pre, RESOURCE, {})
    assert pending.value.transaction == TX
    facilitator.settle = httpx.ReadTimeout("no answer")
    with pytest.raises(SettlementUnknownError) as unknown:
        await base.settle(pre, RESOURCE, {})
    assert unknown.value.transaction == ""


# --- health and the outflow audit ----------------------------------------------------------------------------------


async def test_health_reads_pay_tos_usdc_and_pauses_without_the_rpc(base: BaseX402, rpc: FakeBaseRpc) -> None:
    rpc.balance = 7_000_000
    health = await base.health()
    assert (health.status, health.usdc_units, health.alerts) == ("ok", 7_000_000, ())
    base._balance = None
    rpc.down = True
    assert (await base.health()).status == "rpc_unavailable"
    assert await base.accepts("quick") == []


async def test_a_receiving_address_asks_for_a_sweep_by_hand(facilitator: FakeFacilitator) -> None:
    rpc = FakeBaseRpc()
    rpc.balance = 60_000_000
    separate = make_base(base_settings(base_cold_address=COLD.address), rpc, facilitator)
    assert (await separate.health()).alerts == ("base_sweep_due",)
    # payTo as the cold wallet itself: holding is the point, nothing to sweep.
    same = make_base(base_settings(), rpc, facilitator)
    assert (await same.health()).alerts == ()


async def test_the_audit_flags_outflows_and_pauses_payments(base: BaseX402, rpc: FakeBaseRpc) -> None:
    assert await base.audit_outflows() == []  # first run: the newest chunk, then a cursor
    rpc.head += 50
    rpc.logs_by_block[rpc.head - 20] = [outflow_log(TRANSFER_TOPIC, PAY_TO.address, THIEF, 5_000_000)]
    issues = await base.audit_outflows()
    assert issues == [f"USDC transfer of 5000000 from payTo to {THIEF.lower()}"]
    health = await base.health()
    assert (health.status, health.alerts) == ("compromise_suspected", ("outflow_to_non_cold",))
    assert await base.accepts("quick") == []
    assert await base.clear_alerts() == issues
    assert (await base.health()).status == "ok"


async def test_sweeps_to_cold_spam_and_revocations_are_not_outflows(facilitator: FakeFacilitator) -> None:
    rpc = FakeBaseRpc()
    base = make_base(base_settings(base_cold_address=COLD.address), rpc, facilitator)
    await base.audit_outflows()
    rpc.head += 10
    rpc.logs_by_block[rpc.head - 6] = [
        outflow_log(TRANSFER_TOPIC, PAY_TO.address, COLD.address, 40_000_000),  # the sweep by hand
        outflow_log(TRANSFER_TOPIC, PAY_TO.address, THIEF, 0),  # address-poisoning spam: moves nothing
        outflow_log(APPROVAL_TOPIC, PAY_TO.address, THIEF, 0),  # a revocation
        outflow_log(TRANSFER_TOPIC, PAY_TO.address, THIEF, 1, removed=True),  # reorged away
    ]
    assert await base.audit_outflows() == []


async def test_with_pay_to_as_cold_every_outflow_alarms_and_so_does_an_approval(
    base: BaseX402, rpc: FakeBaseRpc
) -> None:
    await base.audit_outflows()
    rpc.head += 10
    rpc.logs_by_block[rpc.head - 6] = [
        outflow_log(TRANSFER_TOPIC, PAY_TO.address, COLD.address, 1_000, tx="0x" + "01" * 32),
        outflow_log(APPROVAL_TOPIC, PAY_TO.address, THIEF, 2**256 - 1, tx="0x" + "02" * 32),
    ]
    issues = await base.audit_outflows()
    assert issues == [
        f"USDC transfer of 1000 from payTo to {COLD.address.lower()}",
        f"USDC approval of {2**256 - 1} by payTo to spender {THIEF.lower()}",
    ]
    assert [flag.signature for flag in base.flagged()] == ["0x" + "01" * 32, "0x" + "02" * 32]


async def test_the_audit_reads_in_chunks_behind_the_head_and_flags_a_backlog(
    base: BaseX402, rpc: FakeBaseRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("unfret.x402.base._AUDIT_CHUNK", 100)
    monkeypatch.setattr("unfret.x402.base._AUDIT_MAX_CHUNKS", 3)
    await base.audit_outflows()
    confirmed = rpc.head - 5  # base_audit_confirmations
    assert rpc.log_calls == [(confirmed - 99, confirmed)]
    rpc.log_calls.clear()
    rpc.head += 250
    assert await base.audit_outflows() == []
    assert rpc.log_calls == [
        (confirmed + 1, confirmed + 100),
        (confirmed + 101, confirmed + 200),
        (confirmed + 201, confirmed + 250),
    ]
    rpc.log_calls.clear()
    rpc.head += 1_000  # more than three chunks behind: the oldest are skipped, and that is flagged
    issues = await base.audit_outflows()
    assert issues == ["audit backlog: blocks skipped unaudited"]
    assert len(rpc.log_calls) == 3
    assert rpc.log_calls[-1][1] == rpc.head - 5


async def test_a_failed_read_keeps_the_cursor_where_it_was(base: BaseX402, rpc: FakeBaseRpc) -> None:
    await base.audit_outflows()
    cursor = base._cursor
    rpc.head += 10
    rpc.down = True
    with pytest.raises(Exception):  # noqa: B017 - the RPC's error; run_forever logs it and retries
        await base.audit_outflows()
    assert base._cursor == cursor
