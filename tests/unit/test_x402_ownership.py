"""
Ownership proofs for x402scan: the public origin signed by the hot wallet, in OpenAPI and /.well-known/x402.

The fixture records what x402scan reads and how it verifies (from its source), with vectors x402scan's own verifier
accepted.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from tests.unit.x402_helpers import FakeRpc, configured, make_x402
from unfret.config import Settings, settings
from unfret.main import app
from unfret.x402.ownership import check_origin, origin_of, ownership_proofs
from unfret.x402.payment import decode_header
from unfret.x402.wallet import HotWallet

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "x402" / "x402scan_ownership.json").read_text())
HOT = Keypair()
COLD = Keypair().pubkey()
BODY = {"url": "https://shop.test/p/1", "page_content": "<h1>Earbuds</h1>", "tier": "quick", "policy": "balanced"}


def verifies(proof: str, origin: str, pay_to: str) -> bool:
    """x402scan's Solana check: ed25519 over the origin's UTF-8 bytes, the signature base58, against payTo."""
    return Signature.from_string(proof).verify(Pubkey.from_string(pay_to), origin.encode())


def x402scan_reads(document: dict[str, Any]) -> list[str]:
    """``extractFromOpenapi`` of @agentcash/discovery: the provenance extension first, then x-discovery."""
    for key in FIXTURE["openapi_keys"]:
        value = document.get(key)
        listed = value.get("ownershipProofs") if isinstance(value, dict) else None
        if isinstance(listed, list) and (proofs := [p for p in listed if isinstance(p, str)]):
            return proofs
    return []


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://unfret.net", "https://unfret.net"),
        ("https://unfret.net/", "https://unfret.net"),  # x402scan signs URL.origin: no trailing slash
        ("https://Dev.Unfret.NET/some/path?q=1", "https://dev.unfret.net"),
        ("https://unfret.net:443", "https://unfret.net"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("HTTP://127.0.0.1:80", "http://127.0.0.1"),
    ],
)
def test_the_origin_is_what_a_browser_calls_it(url: str, origin: str) -> None:
    assert origin_of(url) == origin
    assert check_origin(origin) == origin


@pytest.mark.parametrize("url", ["ftp://unfret.net", "unfret.net", "https://user:pw@unfret.net", "https://"])
def test_only_http_origins(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        origin_of(url)


@pytest.mark.parametrize(
    "text",
    [
        "https://unfret.net/",
        "https://unfret.net/v1/check",
        "https://Unfret.net",
        "unfret.net",
        "https://unfret.net\n",
        "https://" + "a" * 400 + ".net",
        "\x01\x00\x01",  # the start of a transaction message
    ],
)
def test_the_hot_key_signs_nothing_but_a_bare_origin(text: str) -> None:
    hot = HotWallet(HOT, COLD)
    with pytest.raises(PermissionError):
        hot.ownership_proof(text)


def test_the_proof_verifies_against_the_hot_wallet() -> None:
    key = Keypair()  # a throwaway hot key
    proof = HotWallet(key, COLD).ownership_proof("https://unfret.net")
    assert verifies(proof, "https://unfret.net", str(key.pubkey()))
    assert len(Signature.from_string(proof).to_bytes()) == 64
    assert not verifies(proof, "https://unfret.net/", str(key.pubkey()))  # the exact origin only
    assert not verifies(proof, "https://unfret.net", str(Keypair().pubkey()))


@pytest.mark.parametrize("vector", FIXTURE["solana"]["vectors"])
def test_the_proof_is_the_one_x402scan_verified(vector: dict[str, Any]) -> None:
    # ed25519 is deterministic: the same throwaway key signs the same origin into the same proof.
    key = Keypair.from_seed(bytes.fromhex(FIXTURE["solana"]["seed_hex"]))
    assert str(key.pubkey()) == FIXTURE["solana"]["pay_to"]
    assert vector["x402scan_verified"]
    assert HotWallet(key, COLD).ownership_proof(vector["origin"]) == vector["proof"]
    assert verifies(vector["proof"], vector["origin"], FIXTURE["solana"]["pay_to"])


def test_the_service_proves_while_configured_only() -> None:
    rpc = FakeRpc()
    on = make_x402(configured(HOT, COLD), rpc)
    proof = on.ownership_proof("https://unfret.net")
    assert proof is not None and verifies(proof, "https://unfret.net", str(HOT.pubkey()))
    assert on.ownership_proof("https://unfret.net") == proof  # signed once
    assert (
        make_x402(Settings(hot_wallet_key=SecretStr(""), cold_address=""), rpc).ownership_proof("https://unfret.net")
        is None
    )
    assert (
        make_x402(Settings(hot_wallet_key=SecretStr(str(HOT)), cold_address=""), rpc).ownership_proof("https://x.y")
        is None
    )
    mainnet = Settings(hot_wallet_key=SecretStr(str(HOT)), cold_address=str(COLD), solana_network="solana-mainnet")
    assert make_x402(mainnet, rpc).ownership_proof("https://unfret.net") is None  # refused: no proof either


def test_a_new_hot_key_gets_its_own_proof() -> None:
    rpc = FakeRpc()
    first, other = Keypair(), Keypair()
    a = make_x402(configured(first, COLD), rpc).ownership_proof("https://unfret.net")
    b = make_x402(configured(other, COLD), rpc).ownership_proof("https://unfret.net")
    assert a and b and a != b
    assert verifies(b, "https://unfret.net", str(other.pubkey()))


def test_one_proof_per_service_holding_a_key() -> None:
    rpc = FakeRpc()
    on = make_x402(configured(HOT, COLD), rpc)
    off = make_x402(Settings(hot_wallet_key=SecretStr(""), cold_address=""), rpc)
    assert ownership_proofs("https://unfret.net/", [on, off, object(), None]) == [
        on.ownership_proof("https://unfret.net")
    ]
    assert ownership_proofs("https://unfret.net", [off, None]) == []
    assert ownership_proofs("ftp://unfret.net", [on]) == ownership_proofs("https://un_fret.net", [on]) == []


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(app, "x402", make_x402(configured(HOT, COLD), FakeRpc()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def off(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(app, "x402", make_x402(Settings(hot_wallet_key=SecretStr(""), cold_address=""), FakeRpc()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_the_openapi_document_carries_the_proof_x402scan_reads(client: httpx.AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()
    for key in FIXTURE["openapi_keys"]:
        jsonschema.validate(document[key], FIXTURE["container_schema"])
    (proof,) = x402scan_reads(document)
    origin = origin_of(settings.public_base_url)
    # The payTo x402scan checks it against is the one the 402 names.
    accepts = decode_header((await client.post("/v1/check", json=BODY)).headers["PAYMENT-REQUIRED"])["accepts"]
    pay_to = {a["payTo"] for a in accepts if a["network"].startswith("solana:")}
    assert pay_to == {str(HOT.pubkey())}
    assert verifies(proof, origin, pay_to.pop())


async def test_well_known_carries_the_proof_and_keeps_its_fields(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_document = (await client.get("/.well-known/x402")).json()
    monkeypatch.setattr(app, "x402", make_x402(Settings(hot_wallet_key=SecretStr(""), cold_address=""), FakeRpc()))
    off_document = (await client.get("/.well-known/x402")).json()
    proofs = on_document.pop(FIXTURE["well_known_key"])
    jsonschema.validate({"ownershipProofs": proofs}, FIXTURE["container_schema"])
    assert verifies(proofs[0], origin_of(settings.public_base_url), str(HOT.pubkey()))
    assert on_document == off_document  # nothing else changed for current clients


async def test_no_proof_while_x402_is_off(off: httpx.AsyncClient) -> None:
    assert FIXTURE["well_known_key"] not in (await off.get("/.well-known/x402")).json()
    document = (await off.get("/openapi.json")).json()
    assert not any(key in document for key in FIXTURE["openapi_keys"])
    assert x402scan_reads(document) == []
