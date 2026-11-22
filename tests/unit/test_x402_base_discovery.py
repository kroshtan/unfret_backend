"""Discovery with Base on: the Bazaar extension, /.well-known/x402's networks, llms.txt and /developers."""

import html
import json
import re
import string
from collections.abc import AsyncIterator

import httpx
import jsonschema
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from tests.unit.test_x402_route import BODY, COLD, HOT, FakeGraph
from tests.unit.x402_base_helpers import (
    NETWORK,
    PAY_TO,
    SEPOLIA_USDC,
    FakeBaseRpc,
    FakeFacilitator,
    base_header,
    base_settings,
    make_base,
    settled_receipt,
)
from tests.unit.x402_helpers import FakeRpc, configured, make_x402
from unfret.config import Settings, settings
from unfret.main import app
from unfret.routes.check import VERDICT_EXAMPLE, _base_extensions
from unfret.x402.base import BaseX402
from unfret.x402.ownership import origin_of
from unfret.x402.payment import decode_header
from unfret.x402.verdict import Verdict

SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
TX = "0x" + "12" * 32


@pytest.fixture
def facilitator() -> FakeFacilitator:
    return FakeFacilitator()


@pytest.fixture
def rpc() -> FakeBaseRpc:
    return FakeBaseRpc()


async def _client(monkeypatch: pytest.MonkeyPatch, base: BaseX402) -> httpx.AsyncClient:
    monkeypatch.setattr(app, "x402", make_x402(configured(HOT, COLD), FakeRpc()))
    monkeypatch.setattr(app, "x402_base", base)
    monkeypatch.setattr(app, "quick_graph", FakeGraph())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def on(
    monkeypatch: pytest.MonkeyPatch, rpc: FakeBaseRpc, facilitator: FakeFacilitator
) -> AsyncIterator[httpx.AsyncClient]:
    async with await _client(monkeypatch, make_base(base_settings(), rpc, facilitator)) as c:
        yield c


@pytest.fixture
async def off(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    async with await _client(monkeypatch, BaseX402(Settings(), rpc=FakeBaseRpc())) as c:
        yield c


def _printable(text: str) -> bool:
    return all(c in string.printable and c not in "\t\n\r\x0b\x0c" for c in text)


def test_the_bazaar_extension_validates_against_its_own_schema() -> None:
    bazaar = _base_extensions()["bazaar"]
    jsonschema.Draft202012Validator.check_schema(bazaar["schema"])
    jsonschema.Draft202012Validator(bazaar["schema"]).validate(bazaar["info"])
    assert "$ref" not in str(bazaar["schema"]) and "$id" not in str(bazaar["schema"])  # no external references
    body_schema = bazaar["schema"]["properties"]["input"]["properties"]["body"]
    jsonschema.validate(BODY, body_schema)  # what a real request sends fits too


def test_the_example_verdict_is_a_real_verdict() -> None:
    assert Verdict.model_validate(VERDICT_EXAMPLE).model_dump(mode="json")["action"] == "warn"


async def test_a_402_with_base_carries_the_bazaars_metadata(on: httpx.AsyncClient) -> None:
    required = decode_header((await on.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])
    assert required["extensions"] == _base_extensions()
    resource = required["resource"]
    assert resource["serviceName"] == "Unfret" and len(resource["serviceName"]) <= 32
    # The Bazaar's rules for service metadata: at most 5 tags of printable ASCII, 32 characters; an http(s) icon URL
    # (PNG: SVG isn't accepted); a description of at most 500 characters.
    assert 0 < len(resource["tags"]) <= 5
    assert all(0 < len(tag) <= 32 and _printable(tag) for tag in resource["tags"])
    assert resource["iconUrl"].startswith("https://") and resource["iconUrl"].endswith(".png")
    assert len(resource["description"]) <= 500


async def test_the_icon_exists(on: httpx.AsyncClient) -> None:
    required = decode_header((await on.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])
    path = required["resource"]["iconUrl"].split("/static/", 1)[1]
    response = await on.get(f"/static/{path}")
    assert (response.status_code, response.headers["content-type"]) == (200, "image/png")


async def test_without_base_the_402_is_as_before(off: httpx.AsyncClient) -> None:
    required = decode_header((await off.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])
    assert "extensions" not in required
    assert set(required["resource"]) == {"url", "description", "mimeType", "serviceName"}


async def test_each_settlement_carries_unfrets_discovery_metadata(
    on: httpx.AsyncClient, rpc: FakeBaseRpc, facilitator: FakeFacilitator
) -> None:
    accepts = decode_header((await on.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])["accepts"]
    header = base_header(accepts[-1])
    auth = decode_header(header)["payload"]["authorization"]
    rpc.receipts[TX] = settled_receipt(auth["from"], PAY_TO.address, int(auth["value"]), auth["nonce"].lower())
    assert (await on.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})).status_code == 200
    (settle,) = facilitator.calls("settle")
    payload = settle["paymentPayload"]
    assert payload["extensions"] == _base_extensions()  # ours, not the client's echo ({"client": "poisoned"})
    assert payload["resource"]["tags"] and payload["resource"]["url"].endswith("/v1/check")


async def test_well_known_lists_both_networks(on: httpx.AsyncClient) -> None:
    document = (await on.get("/.well-known/x402")).json()
    assert (document["network"], document["network_name"]) == (SOLANA_DEVNET, "solana-devnet")  # unchanged
    solana, base = document["networks"]
    assert solana["network"] == SOLANA_DEVNET and solana["paymentFlow"] == "upfront"
    assert base["network"] == NETWORK == "eip155:84532"
    assert base["network_name"] == "base-sepolia"
    usdc = base["assets"]["USDC"]
    assert (usdc["address"], usdc["decimals"], usdc["eip712"]) == (SEPOLIA_USDC, 6, {"name": "USDC", "version": "2"})
    assert base["payTo"] == PAY_TO.address
    assert base["paymentFlow"].startswith("authorization")


async def test_well_known_without_base(off: httpx.AsyncClient) -> None:
    document = (await off.get("/.well-known/x402")).json()
    assert [n["network"] for n in document["networks"]] == [SOLANA_DEVNET]


async def test_llms_txt_describes_base_while_it_is_on(on: httpx.AsyncClient) -> None:
    text = (await on.get("/llms.txt")).text
    assert "### On Base" in text and "eip155:84532" in text and SEPOLIA_USDC in text
    assert "transferWithAuthorization" in text


async def test_llms_txt_without_base(off: httpx.AsyncClient) -> None:
    text = (await off.get("/llms.txt")).text
    assert "### On Base" not in text
    assert "Base" not in text  # the errors table too: nothing an agent could take for a way to pay on Base
    assert "Base" not in json.dumps((await off.get("/.well-known/x402")).json())


async def test_llms_txt_errors_name_base_while_it_is_on(on: httpx.AsyncClient) -> None:
    assert "Base never settled it" in (await on.get("/llms.txt")).text


async def test_developers_names_both_networks_when_base_is_on(
    on: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = (await on.get("/developers")).text
    assert "USDC and wSOL on Solana devnet, and USDC on Base Sepolia" in page
    assert "coming next" not in page.lower()
    assert "base-off" not in page and "base-on" not in page
    assert "transferWithAuthorization" in page and "Solana only" in page  # how to pay on Base; unfret-mcp doesn't
    ways = page.split('id="ways"', 1)[1].split("</section>", 1)[0]
    assert "USDC on Base Sepolia" in ways  # the overview lists every network
    http = (await on.get("/developers/http")).text
    assert "USDC and wSOL on Solana devnet, and USDC on Base Sepolia" in http
    assert "comes next" not in http and "base-off" not in http


async def test_developers_says_nothing_about_base_while_it_is_off(off: httpx.AsyncClient) -> None:
    page = (await off.get("/developers")).text
    assert "USDC and wSOL on Solana devnet, scheme" in page
    # Base isn't offered, so it isn't mentioned: no step for paying on it, and no promise.
    assert "Base" not in html.unescape(re.sub(r"<[^>]+>", "", page.split("<main", 1)[1].split("</main>", 1)[0]))
    assert "transferWithAuthorization" not in page and "base-on" not in page and "coming next" not in page.lower()
    http = " ".join((await off.get("/developers/http")).text.split())
    assert "USDC and wSOL on Solana devnet, scheme" in http and "comes next" not in http


def _signer(proof: str, origin: str) -> str:
    """x402scan's EVM check: recover the personal_sign signer of the origin (viem's recoverMessageAddress)."""
    return str(Account.recover_message(encode_defunct(text=origin), signature=proof))


async def test_with_base_on_a_second_ownership_proof_signed_by_base_pay_to(on: httpx.AsyncClient) -> None:
    origin = origin_of(settings.public_base_url)
    proofs = (await on.get("/.well-known/x402")).json()["ownershipProofs"]
    assert len(proofs) == 2  # Solana's first, then Base's
    accepts = decode_header((await on.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])["accepts"]
    base_pay_to = next(a["payTo"] for a in accepts if a["network"] == NETWORK)
    assert _signer(proofs[1], origin) == base_pay_to == PAY_TO.address
    document = (await on.get("/openapi.json")).json()
    assert document["x-discovery"]["ownershipProofs"] == proofs


async def test_without_base_only_solanas_proof(off: httpx.AsyncClient) -> None:
    assert len((await off.get("/.well-known/x402")).json()["ownershipProofs"]) == 1


def test_the_base_hot_key_signs_only_a_bare_origin(rpc: FakeBaseRpc, facilitator: FakeFacilitator) -> None:
    base = make_base(base_settings(), rpc, facilitator)
    proof = base.ownership_proof("https://unfret.net")
    assert proof is not None and proof == base.ownership_proof("https://unfret.net")  # signed once
    assert _signer(proof, "https://unfret.net") == PAY_TO.address
    assert base.hot is not None
    for not_an_origin in ("https://unfret.net/", "0x02f86583014a34800102", "https://unfret.net/v1/check"):
        with pytest.raises(PermissionError):
            base.hot.ownership_proof(not_an_origin)
    assert BaseX402(Settings(), rpc=rpc).ownership_proof("https://unfret.net") is None
