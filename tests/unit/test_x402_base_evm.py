"""The EVM pieces of x402 on Base: Keccak, EIP-55, the RPC reads, and the facilitator client with CDP's JWT."""

import base64
import json
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_utils import keccak as reference_keccak
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from tests.unit.x402_base_helpers import RECORDED
from unfret.x402.evm import (
    APPROVAL_TOPIC,
    AUTHORIZATION_STATE_SELECTOR,
    AUTHORIZATION_USED_TOPIC,
    BALANCE_OF_SELECTOR,
    TRANSFER_TOPIC,
    USDC,
    BaseRpc,
    address_from_topic,
    checksum_address,
    keccak256,
    parse_address,
    topic_address,
)
from unfret.x402.facilitator import CDP_HOST, CdpAuth, Facilitator, FacilitatorError, facilitator_auth
from unfret.x402.solana import RpcError


@pytest.mark.parametrize("data", [b"", b"abc", b"a" * 135, b"a" * 136, b"a" * 137, bytes(range(256)) * 3])
def test_keccak_matches_the_reference(data: bytes) -> None:
    assert keccak256(data) == reference_keccak(data)


@pytest.mark.parametrize(
    ("constant", "signature"),
    [
        (TRANSFER_TOPIC, "Transfer(address,address,uint256)"),
        (APPROVAL_TOPIC, "Approval(address,address,uint256)"),
        (AUTHORIZATION_USED_TOPIC, "AuthorizationUsed(address,bytes32)"),
    ],
)
def test_event_topics(constant: str, signature: str) -> None:
    assert constant == "0x" + keccak256(signature.encode()).hex()


def test_function_selectors() -> None:
    assert "0x" + keccak256(b"authorizationState(address,bytes32)")[:4].hex() == AUTHORIZATION_STATE_SELECTOR
    assert "0x" + keccak256(b"balanceOf(address)")[:4].hex() == BALANCE_OF_SELECTOR


def test_checksums_match_eth_account() -> None:
    for _ in range(20):
        address = Account.create().address
        assert checksum_address(address.lower()) == address
        assert parse_address(address) == address
        assert parse_address(address.lower()) == address  # no checksum to check
    for usdc in USDC.values():
        assert parse_address(usdc) == usdc


def test_a_checksum_typo_is_refused() -> None:
    # EIP-55's own example, fixed: a random address with few letters could turn all one case when a letter is
    # flipped, and an all-lower or all-upper address carries no checksum to check.
    address = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    assert parse_address(address) == address
    for flipped in (3, 4, 11):  # "a" -> "A", "A" -> "a", "F" -> "f": still mixed case, now a wrong checksum
        typo = address[:flipped] + address[flipped].swapcase() + address[flipped + 1 :]
        assert typo[2:] not in {typo[2:].lower(), typo[2:].upper()}
        with pytest.raises(ValueError, match="checksum"):
            parse_address(typo)


def test_topics_round_trip() -> None:
    address = Account.create().address
    assert address_from_topic(topic_address(address)) == address.lower()
    assert len(topic_address(address)) == 66


def rpc_with(handler: Any) -> BaseRpc:
    return BaseRpc("https://base.test", transport=httpx.MockTransport(handler))


async def test_rpc_reads() -> None:
    calls: list[dict[str, Any]] = []
    payer, nonce = Account.create().address, "0x" + "ab" * 32

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        result: Any = {
            "eth_blockNumber": "0x1f4",
            "eth_call": "0x" + (1).to_bytes(32, "big").hex(),
            "eth_getTransactionReceipt": None,
            "eth_getLogs": [{"topics": []}],
        }[body["method"]]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    rpc = rpc_with(handler)
    assert await rpc.block_number() == 500
    assert await rpc.authorization_used(USDC["base-sepolia"], payer, nonce) is True
    assert calls[-1]["params"][0]["data"] == AUTHORIZATION_STATE_SELECTOR + topic_address(payer)[2:] + "ab" * 32
    assert await rpc.token_balance(USDC["base-sepolia"], payer) == 1
    assert await rpc.receipt("0x" + "00" * 32) is None
    assert await rpc.logs(USDC["base-sepolia"], [TRANSFER_TOPIC], 10, 20) == [{"topics": []}]
    assert calls[-1]["params"][0]["fromBlock"] == "0xa" and calls[-1]["params"][0]["toBlock"] == "0x14"
    await rpc.aclose()


async def test_rpc_errors() -> None:
    rpc = rpc_with(lambda _: httpx.Response(200, json={"error": {"message": "range too large"}}))
    with pytest.raises(RpcError, match="range too large"):
        await rpc.block_number()
    rpc = rpc_with(lambda _: httpx.Response(503))
    with pytest.raises(RpcError):
        await rpc.block_number()


# --- CDP auth ------------------------------------------------------------------------------------------------------


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_cdp_jwt_is_an_eddsa_token_for_one_request() -> None:
    keypair = Keypair()
    auth = CdpAuth("organizations/o/apiKeys/k", base64.b64encode(bytes(keypair)).decode())
    token = auth.token("POST", CDP_HOST, "/platform/v2/x402/settle", now=1_800_000_000)
    header_b64, claims_b64, signature_b64 = token.split(".")
    header, claims = json.loads(_unb64(header_b64)), json.loads(_unb64(claims_b64))
    assert header["alg"] == "EdDSA" and header["kid"] == "organizations/o/apiKeys/k" and header["typ"] == "JWT"
    assert len(header["nonce"]) == 16
    assert claims == {
        "sub": "organizations/o/apiKeys/k",
        "iss": "cdp",
        "nbf": 1_800_000_000,
        "exp": 1_800_000_120,
        "uris": ["POST api.cdp.coinbase.com/platform/v2/x402/settle"],
    }
    signature = Signature.from_bytes(_unb64(signature_b64))
    assert signature.verify(Pubkey.from_bytes(bytes(keypair.pubkey())), f"{header_b64}.{claims_b64}".encode())


@pytest.mark.parametrize(
    "secret",
    [
        "not base64!",
        base64.b64encode(b"x" * 32).decode(),
        base64.b64encode(bytes(Keypair())[:32] + bytes(32)).decode(),
    ],
)
def test_cdp_secrets_that_arent_ed25519_keys_are_refused(secret: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        CdpAuth("key", secret)


def test_only_coinbases_facilitator_gets_cdp_auth() -> None:
    assert facilitator_auth("https://x402.org/facilitator", "", "") is None
    with pytest.raises(ValueError, match="cdp_api_key"):
        facilitator_auth("https://api.cdp.coinbase.com/platform/v2/x402", "", "")
    secret = base64.b64encode(bytes(Keypair())).decode()
    assert isinstance(facilitator_auth("https://api.cdp.coinbase.com/platform/v2/x402", "k", secret), CdpAuth)


async def test_the_facilitator_client_signs_each_request_for_its_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=RECORDED["verify_insufficient_balance"],
            headers={"EXTENSION-RESPONSES": base64.b64encode(b'{"bazaar":{"status":"success"}}').decode()},
        )

    keypair = Keypair()
    auth = CdpAuth("k", base64.b64encode(bytes(keypair)).decode())
    client = Facilitator(f"https://{CDP_HOST}/platform/v2/x402", auth, transport=httpx.MockTransport(handler))
    answer = await client.verify({"x402Version": 2}, {"scheme": "exact"})
    assert not answer.ok and answer.reason == "invalid_exact_evm_insufficient_balance"
    assert answer.extension_responses == {"bazaar": {"status": "success"}}
    (request,) = seen
    assert str(request.url) == f"https://{CDP_HOST}/platform/v2/x402/verify"
    claims = json.loads(_unb64(request.headers["Authorization"].removeprefix("Bearer ").split(".")[1]))
    assert claims["uris"] == [f"POST {CDP_HOST}/platform/v2/x402/verify"]
    assert json.loads(request.content) == {
        "x402Version": 2,
        "paymentPayload": {"x402Version": 2},
        "paymentRequirements": {"scheme": "exact"},
    }
    await client.aclose()


async def test_recorded_settle_refusal_is_parsed() -> None:
    client = Facilitator(
        "https://x402.org/facilitator",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=RECORDED["settle_insufficient_balance"])),
    )
    answer = await client.settle({}, {})
    assert (answer.ok, answer.reason, answer.transaction, answer.network) == (
        False,
        "invalid_exact_evm_insufficient_balance",
        "",
        "eip155:84532",
    )


async def test_an_answer_without_the_verdict_field_is_an_error() -> None:
    client = Facilitator(
        "https://x402.org/facilitator",
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"errorMessage": "Unauthorized"})),
    )
    with pytest.raises(FacilitatorError, match="401"):
        await client.verify({}, {})
    with pytest.raises(FacilitatorError, match="401"):
        await client.settle({}, {})
