"""Phone sign-in through a pairing, end to end over HTTP, including the attacks it must resist."""

import base64

import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair

from veridex.config import settings
from veridex.credits import siws
from veridex.credits.pairing import pairing_statement
from veridex.main import app


def message(fields: dict[str, str], keypair: Keypair, statement: str | None = None) -> bytes:
    return siws.message_text(
        siws.SignInFields(
            domain=fields["domain"],
            address=str(keypair.pubkey()),
            statement=fields["statement"] if statement is None else statement,
            uri=fields["uri"],
            version=fields["version"],
            nonce=fields["nonce"],
            issued_at=fields["issued_at"],
            expiration_time=fields["expiration_time"],
        )
    ).encode()


async def phone_sign_in(
    client: httpx.AsyncClient,
    keypair: Keypair,
    pair_id: str,
    *,
    nonce_pair: str | None = None,
    statement: str | None = None,
) -> httpx.Response:
    fields = (await client.get("/v1/auth/nonce", params={"pair": nonce_pair or pair_id})).json()
    text = message(fields, keypair, statement)
    return await client.post(
        "/v1/auth/verify",
        json={
            "address": str(keypair.pubkey()),
            "signed_message": base64.b64encode(text).decode(),
            "signature": base64.b64encode(bytes(keypair.sign_message(text))).decode(),
            "pair_id": pair_id,
        },
    )


async def start(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/v1/auth/pair")
    assert response.status_code == 201
    body: dict[str, str] = response.json()
    return body


async def test_phone_sign_in_reaches_the_extension_once(client: httpx.AsyncClient) -> None:
    pairing = await start(client)
    assert pairing["connect_url"].endswith(f"/connect?pair={pairing['pair_id']}")
    info = (await client.get(f"/v1/auth/pair/{pairing['pair_id']}/info")).json()
    assert info["confirm_code"] == pairing["confirm_code"]
    fields = (await client.get("/v1/auth/nonce", params={"pair": pairing["pair_id"]})).json()
    assert fields["statement"].endswith(f"For the device showing {pairing['confirm_code']}.")

    pending = await client.get(f"/v1/auth/pair/{pairing['pair_id']}")
    assert pending.status_code == 202

    phone = Keypair()
    assert (await phone_sign_in(client, phone, pairing["pair_id"])).status_code == 200

    collected = await client.get(f"/v1/auth/pair/{pairing['pair_id']}")
    assert collected.status_code == 200
    session = collected.json()
    assert session["address"] == str(phone.pubkey())
    credits = await client.get("/v1/credits", headers={"Authorization": f"Bearer {session['token']}"})
    assert credits.status_code == 200

    replay = await client.get(f"/v1/auth/pair/{pairing['pair_id']}")
    assert replay.status_code == 404  # delivered once, then gone


async def test_an_attacker_cannot_sign_into_someone_elses_pairing_with_another_code(client: httpx.AsyncClient) -> None:
    victim, attacker = await start(client), await start(client)
    # The attacker signs a statement naming their own code and submits it for the victim's pairing.
    fields = (await client.get("/v1/auth/nonce", params={"pair": attacker["pair_id"]})).json()
    response = await phone_sign_in(client, Keypair(), victim["pair_id"], nonce_pair=attacker["pair_id"])

    assert response.status_code == 401
    assert "another device" in response.json()["detail"]
    assert (await client.get(f"/v1/auth/pair/{victim['pair_id']}")).status_code == 202
    assert (
        fields["statement"]
        != (await client.get("/v1/auth/nonce", params={"pair": victim["pair_id"]})).json()["statement"]
    )


async def test_a_plain_sign_in_message_cannot_bind_a_pairing(client: httpx.AsyncClient) -> None:
    pairing = await start(client)
    response = await phone_sign_in(client, Keypair(), pairing["pair_id"], statement=siws.STATEMENT)
    assert response.status_code == 401
    assert (await client.get(f"/v1/auth/pair/{pairing['pair_id']}")).status_code == 202


async def test_a_signed_in_pairing_cannot_be_rebound(client: httpx.AsyncClient) -> None:
    pairing = await start(client)
    victim = Keypair()
    assert (await phone_sign_in(client, victim, pairing["pair_id"])).status_code == 200

    # No nonce for a signed-in pairing…
    assert (await client.get("/v1/auth/nonce", params={"pair": pairing["pair_id"]})).status_code == 409
    # …and a hand-built message naming the right code, on an ordinary nonce, is refused too.
    attacker = Keypair()
    fields = (await client.get("/v1/auth/nonce")).json()
    statement = pairing_statement(siws.STATEMENT, pairing["confirm_code"])
    text = message(fields, attacker, statement)
    rebind = await client.post(
        "/v1/auth/verify",
        json={
            "address": str(attacker.pubkey()),
            "signed_message": base64.b64encode(text).decode(),
            "signature": base64.b64encode(bytes(attacker.sign_message(text))).decode(),
            "pair_id": pairing["pair_id"],
        },
    )
    assert rebind.status_code == 409
    assert rebind.json()["error_code"] == "PAIRING_USED"
    session = (await client.get(f"/v1/auth/pair/{pairing['pair_id']}")).json()
    assert session["address"] == str(victim.pubkey())


async def test_an_expired_pairing_is_gone_not_unknown(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pairing = await start(client)
    store = app.credits.pairings
    real = store.clock
    monkeypatch.setattr(store, "clock", lambda: real() + 301)

    assert (await client.get(f"/v1/auth/pair/{pairing['pair_id']}")).status_code == 410
    assert (await client.get(f"/v1/auth/pair/{pairing['pair_id']}/info")).json()["error_code"] == "PAIRING_EXPIRED"
    assert (await client.get("/v1/auth/nonce", params={"pair": pairing["pair_id"]})).status_code == 410
    unknown = await client.get("/v1/auth/pair/AAAAAAAAAAAAAAAAAAAAAA")
    assert unknown.status_code == 404
    assert unknown.json()["error_code"] == "PAIRING_UNKNOWN"


async def test_pairing_creation_is_rate_limited(client: httpx.AsyncClient) -> None:
    codes = [(await client.post("/v1/auth/pair")).status_code for _ in range(settings.pairing_per_ip + 1)]
    assert codes[:-1] == [201] * settings.pairing_per_ip
    assert codes[-1] == 429


async def test_pairing_is_off_without_sign_in(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "session_secret", SecretStr(""))
    assert (await client.post("/v1/auth/pair")).status_code == 503
    assert (await client.get("/v1/auth/pair/x")).status_code == 503
    assert (await client.get("/v1/auth/pair/x/info")).status_code == 503


async def test_pairing_endpoints_allow_extension_origins(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/pair", headers={"Origin": "chrome-extension://kpllgmbgfmnlpnmkibmhflkafhilckfe"}
    )
    assert response.headers.get("access-control-allow-origin") == "*"
