import base64
import json
import time
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from solders.keypair import Keypair

from unfret_mcp import server as server_module
from unfret_mcp.base import (
    CAIP2,
    USDC,
    BaseKeyError,
    BaseWallet,
    choose_base_requirement,
)
from unfret_mcp.payer import Payer, PaymentFailedError, SpendGuard, SpendRefusedError
from unfret_mcp.solana import CAIP2 as SOLANA_CAIP2
from unfret_mcp.solana import Rpc

from .test_payer import TokenRpc, b64, requirement, usdc_requirement

KEY = Account.create()  # throwaway: holds nothing
KEY_HEX = KEY.key.hex().removeprefix("0x")
PAY_TO = Account.create().address
SEPOLIA = CAIP2["base-sepolia"]
MAINNET = CAIP2["base-mainnet"]


def base_requirement(amount: int = 10_000, network: str = SEPOLIA, **extra: Any) -> dict[str, Any]:
    name = "USD Coin" if network == MAINNET else "USDC"
    return {
        "scheme": "exact",
        "network": network,
        "amount": str(amount),
        "asset": USDC["base-mainnet" if network == MAINNET else "base-sepolia"],
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {"name": name, "version": "2", "decimals": 6, "tier": "quick", **extra},
    }


def wallet(network: str = "base-sepolia") -> BaseWallet:
    return BaseWallet(KEY_HEX, "base-mainnet" if network == "base-mainnet" else "base-sepolia")


# Requirement selection.


def test_picks_circle_usdc_exact_on_the_wallets_network() -> None:
    other_token = {**base_requirement(), "asset": Account.create().address}
    permit2 = base_requirement(assetTransferMethod="permit2")
    wrong_domain = base_requirement(name="Fake USDC")
    upto = {**base_requirement(), "scheme": "upto"}
    good = base_requirement()
    offer = {"accepts": [requirement(), other_token, permit2, wrong_domain, upto, good]}
    assert choose_base_requirement(offer, wallet()) == good


def test_base_mainnet_is_refused_unless_configured() -> None:
    offer = {"accepts": [requirement(), base_requirement(network=MAINNET)]}
    with pytest.raises(ValueError, match="UNFRET_MCP_BASE_NETWORK=base-mainnet"):
        choose_base_requirement(offer, wallet())
    assert choose_base_requirement(offer, wallet("base-mainnet"))["network"] == MAINNET


def test_a_mainnet_wallet_never_pays_on_sepolia() -> None:
    with pytest.raises(ValueError, match="base-mainnet"):
        choose_base_requirement({"accepts": [base_requirement()]}, wallet("base-mainnet"))


# The signed authorization.


def test_the_authorization_commits_to_exactly_the_offer() -> None:
    now = 1_800_000_000
    signed = wallet().sign(base_requirement(150_000), now=now)
    auth = signed["authorization"]
    assert auth["from"] == KEY.address
    assert auth["to"] == PAY_TO
    assert auth["value"] == "150000"
    assert auth["validAfter"] == str(now - 600)
    assert auth["validBefore"] == str(now + 300)  # maxTimeoutSeconds: beyond Unfret's analysis timeout plus a minute
    assert len(bytes.fromhex(auth["nonce"].removeprefix("0x"))) == 32
    assert auth["nonce"] != wallet().sign(base_requirement(150_000), now=now)["authorization"]["nonce"]

    typed = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": USDC["base-sepolia"]},
        "message": {k: int(v) if k in {"value", "validAfter", "validBefore"} else v for k, v in auth.items()},
    }
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=signed["signature"]) == KEY.address


def test_the_mainnet_domain_is_usd_coin_on_chain_8453() -> None:
    signed = wallet("base-mainnet").sign(base_requirement(network=MAINNET), now=1_800_000_000)
    auth = signed["authorization"]
    domain = {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": USDC["base-mainnet"]}
    message = {k: int(v) if k in {"value", "validAfter", "validBefore"} else v for k, v in auth.items()}
    encoded = encode_typed_data(domain, {"TransferWithAuthorization": _TRANSFER_TYPES}, message)
    assert Account.recover_message(encoded, signature=signed["signature"]) == KEY.address


_TRANSFER_TYPES = [
    {"name": "from", "type": "address"},
    {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"},
    {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"},
    {"name": "nonce", "type": "bytes32"},
]


def test_an_offers_long_timeout_is_capped() -> None:
    signed = wallet().sign({**base_requirement(), "maxTimeoutSeconds": 86_400}, now=10_000)
    assert signed["authorization"]["validBefore"] == "10600"


# The key stays secret.


def test_the_key_never_shows() -> None:
    w = wallet()
    assert KEY_HEX not in repr(w) and KEY_HEX not in str(w) and KEY_HEX not in str(vars(w))
    assert KEY.address in repr(w)
    for bad in (KEY_HEX[:-2], KEY_HEX + "zz", "0x" + "0" * 64):
        with pytest.raises(BaseKeyError) as refused:
            BaseWallet(bad, "base-sepolia")
        assert bad not in str(refused.value) and bad[2:] not in str(refused.value)
        assert refused.value.__cause__ is None and refused.value.__suppress_context__


# Paying through the payer.


def offers(*accepts: dict[str, Any], settle: bool = True) -> tuple[httpx.MockTransport, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if "PAYMENT-SIGNATURE" not in request.headers:
            required = {"x402Version": 2, "accepts": list(accepts)}
            return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(required)}, json=required)
        payload = json.loads(base64.b64decode(request.headers["PAYMENT-SIGNATURE"]))
        sent.append(payload)
        network = payload["accepted"]["network"]
        if not settle:
            return httpx.Response(402, json={"error_code": "PAYMENT_INVALID", "detail": "insufficient_funds"})
        settlement = {"success": True, "transaction": "0x" + "12" * 32, "network": network}
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": b64(settlement)}, json={"network": network})

    return httpx.MockTransport(handle), sent


def payer(
    transport: httpx.MockTransport,
    spend: SpendGuard,
    *,
    solana: bool = False,
    base: BaseWallet | None = None,
    prefer: str = "solana",
) -> Payer:
    return Payer(
        api_url="http://api",
        agent=Keypair() if solana else None,
        rpc=TokenRpc(usdc=10**9) if solana else Rpc("http://rpc.invalid"),
        network="solana-devnet",
        guard=spend,
        base=base,
        prefer_network="base" if prefer == "base" else "solana",
        transport=transport,
    )


def spend_guard() -> SpendGuard:
    return SpendGuard(max_per_call=250_000, session_cap=2_500_000)


async def test_pays_usdc_on_base_and_counts_it_in_dollars() -> None:
    transport, sent = offers(requirement(), base_requirement(150_000))
    spend = spend_guard()
    result = await payer(transport, spend, base=wallet()).check({"url": "https://shop.test"})
    assert result["status"] == "ok" and result["verdict"] == {"network": SEPOLIA}
    assert result["network"] == SEPOLIA
    assert result["paid"] == {"currency": "USDC", "amount": 150_000, "usd_micros": 150_000}
    assert result["paid_lamports"] == 0
    assert spend.spent == 150_000
    (payload,) = sent
    assert payload["x402Version"] == 2 and payload["accepted"] == base_requirement(150_000)
    assert payload["payload"]["authorization"]["to"] == PAY_TO
    assert result["payment"] == payload["payload"]["authorization"]["nonce"]
    assert KEY_HEX not in json.dumps(result) and KEY_HEX not in json.dumps(payload)


async def test_the_spend_cap_applies_to_base() -> None:
    transport, sent = offers(base_requirement(300_000))
    spend = spend_guard()
    with pytest.raises(SpendRefusedError, match="per-call maximum"):
        await payer(transport, spend, base=wallet()).check({"url": "u"})
    assert sent == [] and spend.spent == 0

    transport, sent = offers(base_requirement(200_000))
    nearly_spent = SpendGuard(max_per_call=250_000, session_cap=250_000, spent=100_000)
    with pytest.raises(SpendRefusedError, match="session cap"):
        await payer(transport, nearly_spent, base=wallet()).check({"url": "u"})
    assert sent == []


async def test_a_cap_refusal_on_base_does_not_fall_back_to_solana() -> None:
    transport, sent = offers(usdc_requirement(300_000), base_requirement(300_000))
    with pytest.raises(SpendRefusedError):
        await payer(transport, spend_guard(), solana=True, base=wallet(), prefer="base").check({"url": "u"})
    assert sent == []


async def test_a_refused_base_payment_gives_the_spend_back() -> None:
    transport, _ = offers(base_requirement(150_000), settle=False)
    spend = spend_guard()
    result = await payer(transport, spend, base=wallet()).check({"url": "u"})
    assert result["status"] == "not_paid" and spend.spent == 0


async def test_base_mainnet_is_never_paid_without_the_setting() -> None:
    transport, sent = offers(base_requirement(network=MAINNET))
    with pytest.raises(PaymentFailedError, match="UNFRET_MCP_BASE_NETWORK=base-mainnet"):
        await payer(transport, spend_guard(), base=wallet()).check({"url": "u"})
    assert sent == []


# Choosing the network.


async def test_with_both_wallets_solana_is_tried_first_by_default() -> None:
    transport, sent = offers(usdc_requirement(150_000), base_requirement(150_000))
    result = await payer(transport, spend_guard(), solana=True, base=wallet()).check({"url": "u"})
    assert result["network"] == SOLANA_CAIP2["solana-devnet"] and sent[0]["accepted"]["network"].startswith("solana:")


async def test_prefer_base_pays_on_base() -> None:
    transport, _ = offers(usdc_requirement(150_000), base_requirement(150_000))
    result = await payer(transport, spend_guard(), solana=True, base=wallet(), prefer="base").check({"url": "u"})
    assert result["network"] == SEPOLIA


async def test_the_other_network_is_the_fallback_when_the_preferred_isnt_offered() -> None:
    transport, _ = offers(usdc_requirement(150_000))  # no Base option (Base off on that server)
    result = await payer(transport, spend_guard(), solana=True, base=wallet(), prefer="base").check({"url": "u"})
    assert result["network"] == SOLANA_CAIP2["solana-devnet"]
    transport, _ = offers(base_requirement(150_000))  # Base alone
    result = await payer(transport, spend_guard(), solana=True, base=wallet()).check({"url": "u"})
    assert result["network"] == SEPOLIA


async def test_when_nothing_fits_each_networks_reason_is_given() -> None:
    transport, _ = offers(base_requirement(network=MAINNET))
    with pytest.raises(PaymentFailedError, match=r"solana: .*; base: .*base-mainnet"):
        await payer(transport, spend_guard(), solana=True, base=wallet()).check({"url": "u"})


def test_rails_follow_the_preference_and_the_configured_wallets() -> None:
    transport, _ = offers()
    assert payer(transport, spend_guard(), solana=True, base=wallet()).rails() == ["solana", "base"]
    assert payer(transport, spend_guard(), solana=True, base=wallet(), prefer="base").rails() == ["base", "solana"]
    assert payer(transport, spend_guard(), base=wallet()).rails() == ["base"]
    assert payer(transport, spend_guard(), solana=True, prefer="base").rails() == ["solana"]


# Configuration from the environment.


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "UNFRET_MCP_KEYPAIR",
        "UNFRET_MCP_SECRET_KEY",
        "UNFRET_MCP_BASE_KEY",
        "UNFRET_MCP_BASE_NETWORK",
        "UNFRET_MCP_PREFER",
        "UNFRET_MCP_NETWORK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server_module, "_payer", None)
    return monkeypatch


def test_a_base_key_alone_configures_a_sepolia_payer(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("UNFRET_MCP_BASE_KEY", "0x" + KEY_HEX)
    configured = server_module.payer()
    assert configured.agent is None and configured.base is not None
    assert configured.base.caip2 == SEPOLIA and configured.rails() == ["base"]


def test_base_mainnet_and_the_preference_come_from_the_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("UNFRET_MCP_BASE_KEY", KEY_HEX)
    clean_env.setenv("UNFRET_MCP_BASE_NETWORK", "base-mainnet")
    clean_env.setenv("UNFRET_MCP_SECRET_KEY", str(Keypair()))
    clean_env.setenv("UNFRET_MCP_PREFER", "Base")
    configured = server_module.payer()
    assert configured.base is not None and configured.base.caip2 == MAINNET
    assert configured.rails() == ["base", "solana"]


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("UNFRET_MCP_BASE_NETWORK", "base-goerli", "UNFRET_MCP_BASE_NETWORK"),
        ("UNFRET_MCP_PREFER", "ethereum", "UNFRET_MCP_PREFER"),
    ],
)
def test_bad_settings_are_refused(clean_env: pytest.MonkeyPatch, name: str, value: str, match: str) -> None:
    clean_env.setenv("UNFRET_MCP_BASE_KEY", KEY_HEX)
    clean_env.setenv(name, value)
    with pytest.raises(RuntimeError, match=match):
        server_module.payer()


def test_no_wallet_names_every_option(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="UNFRET_MCP_BASE_KEY"):
        server_module.payer()


def test_a_bad_base_key_is_refused_without_echoing_it(clean_env: pytest.MonkeyPatch) -> None:
    bad = KEY_HEX[:-1] + "g"
    clean_env.setenv("UNFRET_MCP_BASE_KEY", bad)
    with pytest.raises(BaseKeyError) as refused:
        server_module.payer()
    assert bad not in str(refused.value)


def test_signing_uses_the_clock_by_default() -> None:
    before = int(time.time())
    auth = wallet().sign(base_requirement())["authorization"]
    assert before + 300 <= int(auth["validBefore"]) <= int(time.time()) + 300
