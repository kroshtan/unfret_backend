"""OAuth for the remote MCP server: discovery, registration, the sign-in page, tokens, /mcp with a token, revoking."""

import base64
import hashlib
import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import pytest

from tests.integration.conftest import FakeGraph
from tests.integration.test_keys import solve
from tests.integration.test_mcp_remote import PAGE, mcp_client, serving, structured
from unfret import oauth
from unfret.config import settings
from unfret.credits import retire
from unfret.routes import oauth as oauth_routes
from unfret.routes.oauth import RateLimit

__all__ = ["serving"]  # the fixture, imported to be used here

CLAUDE = "https://claude.ai/api/mcp/auth_callback"
RESOURCE = "https://unfret.net/mcp"


@pytest.fixture(autouse=True)
def oauth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True)
    monkeypatch.setattr(settings, "public_base_url", "https://unfret.net")
    monkeypatch.setattr(settings, "key_pow_bits", 4)
    monkeypatch.setattr(oauth_routes, "_bad_passes", RateLimit(1000))
    monkeypatch.setattr(oauth_routes, "_bad_codes", RateLimit(1000))
    monkeypatch.setattr(oauth_routes, "_registrations", RateLimit(1000))


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def register(client: httpx.AsyncClient, redirect_uris: list[str] | None = None) -> str:
    response = await client.post(
        "/oauth/register", json={"client_name": "Claude", "redirect_uris": redirect_uris or [CLAUDE]}
    )
    assert response.status_code == 201, response.text
    client_id: str = response.json()["client_id"]
    return client_id


def authorization(client_id: str, challenge: str, redirect_uri: str = CLAUDE, **extra: str) -> dict[str, str]:
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": "st-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": "checks",
        "resource": RESOURCE,
        **extra,
    }


async def approve_with_pass(client: httpx.AsyncClient, params: dict[str, str], key: str) -> httpx.Response:
    return await client.post("/oauth/authorize", data={**params, "action": "pass", "pass": key})


def code_of(response: httpx.Response, redirect_uri: str = CLAUDE) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    sent, expected = urlsplit(location), urlsplit(redirect_uri)
    assert (sent.scheme, sent.netloc, sent.path) == (expected.scheme, expected.netloc, expected.path)
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["st-123"] and query["iss"] == ["https://unfret.net"]
    return query["code"][0]


async def exchange(
    client: httpx.AsyncClient, client_id: str, code: str, verifier: str, redirect_uri: str = CLAUDE
) -> httpx.Response:
    return await client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )


async def refresh(client: httpx.AsyncClient, client_id: str, token: str) -> httpx.Response:
    return await client.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": token, "client_id": client_id}
    )


async def signed_in(
    client: httpx.AsyncClient, *, developer: bool = True
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415 - imports the app's test fixtures

    issued = await new_pass(client, developer=developer)
    client_id = await register(client)
    verifier, challenge = pkce()
    code = code_of(await approve_with_pass(client, authorization(client_id, challenge), issued["key"]))
    response = await exchange(client, client_id, code, verifier)
    assert response.status_code == 200, response.text
    return client_id, issued, response.json()


async def mcp_status(client: httpx.AsyncClient, token: str) -> int:
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    return (await client.post("/mcp", json=initialize, headers=headers)).status_code


# ── Discovery ────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_a_401_at_mcp_says_where_to_sign_in(serving: httpx.AsyncClient) -> None:
    refused = await serving.post("/mcp", json={}, headers={"Accept": "application/json"})

    assert refused.status_code == 401
    challenge = refused.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="https://unfret.net/.well-known/oauth-protected-resource/mcp"' in challenge
    assert "error=" not in challenge  # no credentials at all: no error code

    bad = await serving.post("/mcp", json={}, headers={"Authorization": "Bearer ufat_nope"})
    assert bad.status_code == 401 and 'error="invalid_token"' in bad.headers["WWW-Authenticate"]


async def test_discovery_documents(client: httpx.AsyncClient) -> None:
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        document = (await client.get(path)).json()
        assert document["resource"] == RESOURCE and document["authorization_servers"] == ["https://unfret.net"]
        assert "offline_access" not in document["scopes_supported"]

    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == "https://unfret.net"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
    assert metadata["client_id_metadata_document_supported"] is True
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["registration_endpoint"] == "https://unfret.net/oauth/register"


async def test_everything_is_404_while_off(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False)
    assert (await client.get("/.well-known/oauth-authorization-server")).status_code == 404
    assert (await client.post("/oauth/register", json={"redirect_uris": [CLAUDE]})).status_code == 404
    assert (await client.post("/oauth/token", data={"grant_type": "refresh_token"})).status_code == 404


async def test_while_off_mcp_takes_no_token_and_says_nothing_of_oauth(
    serving: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, tokens = await signed_in(serving)
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False)

    refused = await serving.post("/mcp", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"})

    assert refused.status_code == 401 and "resource_metadata" not in refused.headers["WWW-Authenticate"]


# ── The whole flow ───────────────────────────────────────────────────────────────────────────────────────────────


async def test_full_flow_register_authorize_token_mcp_refresh_revoke(
    serving: httpx.AsyncClient, graph: FakeGraph
) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415

    issued = await new_pass(serving, developer=True)
    client_id = await register(serving)
    verifier, challenge = pkce()
    params = authorization(client_id, challenge)

    page = await serving.get("/oauth/authorize", params=params)
    assert page.status_code == 200
    assert "Connect Unfret to Claude" in page.text and "claude.ai" in page.text
    assert "form-action 'self' https://claude.ai;" in page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]

    code = code_of(await approve_with_pass(serving, params, issued["key"]))
    response = await exchange(serving, client_id, code, verifier)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == settings.mcp_oauth_access_ttl_s
    assert tokens["access_token"].startswith("ufat_") and tokens["refresh_token"].startswith("ufrt_")

    async with mcp_client(tokens["access_token"]) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        checked = structured(await session.call_tool("check_store_deep", PAGE))
        left = structured(await session.call_tool("get_checks_left", {}))
    assert names == {"check_store_quick", "check_store_deep", "get_checks_left", "get_pricing"}
    # Paid from the pass the person picked.
    assert checked["status"] == "ok" and checked["charged"] is True and left["checks_remaining"] == 19

    # The token is for /mcp only: the rest of the API doesn't take it.
    credits = await serving.get("/v1/credits", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert credits.status_code == 401

    renewed = await refresh(serving, client_id, tokens["refresh_token"])
    assert renewed.status_code == 200
    fresh = renewed.json()
    assert fresh["refresh_token"] != tokens["refresh_token"]
    assert await mcp_status(serving, tokens["access_token"]) == 401  # the old access token was replaced
    assert await mcp_status(serving, fresh["access_token"]) == 200
    assert (await refresh(serving, client_id, tokens["refresh_token"])).json()["error"] == "invalid_grant"

    revoked = await serving.post("/oauth/revoke", data={"token": fresh["refresh_token"], "client_id": client_id})
    assert revoked.status_code == 200
    assert await mcp_status(serving, fresh["access_token"]) == 401
    assert (await refresh(serving, client_id, fresh["refresh_token"])).json()["error"] == "invalid_grant"


async def test_a_new_free_pass_from_the_sign_in_page(serving: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    client_id = await register(serving)
    verifier, challenge = pkce()
    params = authorization(client_id, challenge)
    pow_challenge = (await serving.get("/v1/keys/challenge")).json()
    solution = solve(pow_challenge["challenge"], pow_challenge["bits"])

    page = await serving.post(
        "/oauth/authorize",
        data={**params, "action": "create", "challenge": pow_challenge["challenge"], "solution": solution},
    )

    # The pass and its backup code are shown once, with a link on to the client.
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    key = re.search(r'<code id="new-pass">(ufk_[a-z2-7_]+)</code>', page.text)
    assert key and re.search(r'<code id="new-backup">ufr_', page.text)
    link = re.search(r'href="(https://claude\.ai/api/mcp/auth_callback\?[^"]+)"', page.text)
    assert link
    location = link.group(1).replace("&amp;", "&")
    code = parse_qs(urlsplit(location).query)["code"][0]
    tokens = (await exchange(serving, client_id, code, verifier)).json()
    async with mcp_client(tokens["access_token"]) as session:
        left = structured(await session.call_tool("get_checks_left", {}))
    assert left["free_checks_remaining"] == settings.free_checks_per_pass

    # The same solved challenge doesn't make a second pass.
    again = await serving.post(
        "/oauth/authorize",
        data={**params, "action": "create", "challenge": pow_challenge["challenge"], "solution": solution},
    )
    assert again.status_code == 400 and "Challenge invalid" in again.text
    assert await pool.fetchval("SELECT count(*) FROM api_keys") == 1


async def test_a_loopback_client_any_port(client: httpx.AsyncClient) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415

    issued = await new_pass(client)
    client_id = await register(client, ["http://localhost/callback", "http://127.0.0.1/callback"])
    verifier, challenge = pkce()
    redirect = "http://127.0.0.1:53111/callback"
    params = authorization(client_id, challenge, redirect)

    page = await client.get("/oauth/authorize", params=params)
    assert page.status_code == 200 and "program on your own computer" in page.text
    assert "form-action 'self' http://127.0.0.1:53111;" in page.headers["Content-Security-Policy"]
    code = code_of(await approve_with_pass(client, params, issued["key"]), redirect)
    assert (await exchange(client, client_id, code, verifier, redirect)).status_code == 200


async def test_a_client_id_metadata_document(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415

    url = "https://claude.ai/oauth/claude-code-client-metadata"
    document = {"client_id": url, "client_name": "Claude Code", "redirect_uris": ["http://localhost/callback"]}
    fetched: list[str] = []

    async def fetch(address: str) -> bytes:
        fetched.append(address)
        import json  # noqa: PLC0415

        return json.dumps(document).encode()

    monkeypatch.setattr(oauth, "fetch_document", fetch)
    monkeypatch.setattr(oauth, "_documents", {})
    issued = await new_pass(client)
    verifier, challenge = pkce()
    redirect = "http://localhost:40001/callback"
    params = authorization(url, challenge, redirect)

    page = await client.get("/oauth/authorize", params=params)
    assert (
        page.status_code == 200 and "Connect Unfret to claude.ai" in page.text
    )  # the host, not the self-claimed name
    code = code_of(await approve_with_pass(client, params, issued["key"]), redirect)
    assert (await exchange(client, url, code, verifier, redirect)).status_code == 200
    assert fetched == [url]  # cached between the page and the approval

    # A document that isn't its own client_id, or a redirect it doesn't list, gets nowhere.
    monkeypatch.setattr(oauth, "_documents", {})
    document["client_id"] = "https://evil.example/doc"
    assert (await client.get("/oauth/authorize", params=params)).status_code == 400


# ── Refusals ─────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "redirect_uris",
    [
        ["https://evil.example/callback"],
        ["https://claude.ai/api/mcp/auth_callback/../evil"],
        ["https://claude.ai.evil.example/api/mcp/auth_callback"],
        ["http://claude.ai/api/mcp/auth_callback"],
        ["http://192.168.1.10/callback"],
        [CLAUDE, "https://evil.example/cb"],
        [],
    ],
)
async def test_registration_refuses_other_redirects(client: httpx.AsyncClient, redirect_uris: list[str]) -> None:
    response = await client.post("/oauth/register", json={"redirect_uris": redirect_uris})
    assert response.status_code == 400 and response.json()["error"] == "invalid_redirect_uri"


async def test_registration_refuses_a_confidential_client(client: httpx.AsyncClient) -> None:
    body = {"redirect_uris": [CLAUDE], "token_endpoint_auth_method": "client_secret_basic"}
    response = await client.post("/oauth/register", json=body)
    assert response.status_code == 400 and response.json()["error"] == "invalid_client_metadata"


async def test_registrations_are_rate_limited(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_routes, "_registrations", RateLimit(2))
    statuses = [(await client.post("/oauth/register", json={"redirect_uris": [CLAUDE]})).status_code for _ in range(3)]
    assert statuses == [201, 201, 429]


async def test_a_wrong_redirect_is_never_followed(client: httpx.AsyncClient) -> None:
    client_id = await register(client)
    _, challenge = pkce()

    for redirect in ("https://evil.example/cb", "https://claude.com/api/mcp/auth_callback"):  # not registered
        page = await client.get("/oauth/authorize", params=authorization(client_id, challenge, redirect))
        assert page.status_code == 400 and "location" not in page.headers
    unknown = await client.get("/oauth/authorize", params=authorization("ufc_unknown", challenge))
    assert unknown.status_code == 400 and "location" not in unknown.headers


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge": ""}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"resource": "https://unfret.net/v1/check"}, "invalid_target"),
    ],
)
async def test_a_bad_request_goes_back_with_an_error(
    client: httpx.AsyncClient, change: dict[str, str], error: str
) -> None:
    client_id = await register(client)
    _, challenge = pkce()

    response = await client.get("/oauth/authorize", params={**authorization(client_id, challenge), **change})

    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == [error] and query["state"] == ["st-123"] and query["iss"] == ["https://unfret.net"]


async def test_a_wrong_pass_shows_the_page_again(client: httpx.AsyncClient) -> None:
    client_id = await register(client)
    _, challenge = pkce()
    response = await approve_with_pass(client, authorization(client_id, challenge), "ufk_aaaaaaaaaa_" + "a" * 52)
    assert response.status_code == 400 and "a valid Unfret pass" in response.text


async def test_bad_pkce_wrong_redirect_and_other_client_are_refused(client: httpx.AsyncClient) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415

    issued = await new_pass(client)
    client_id = await register(client, [CLAUDE, "https://claude.com/api/mcp/auth_callback"])
    other = await register(client)
    verifier, challenge = pkce()

    for attempt in (
        {"verifier": pkce()[0]},  # another verifier
        {"verifier": "short"},
        {"redirect": "https://claude.com/api/mcp/auth_callback"},
        {"client": other},
    ):
        code = code_of(await approve_with_pass(client, authorization(client_id, challenge), issued["key"]))
        response = await exchange(
            client,
            str(attempt.get("client", client_id)),
            code,
            str(attempt.get("verifier", verifier)),
            str(attempt.get("redirect", CLAUDE)),
        )
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant", attempt
        # A refused code is spent: the right request can't use it afterwards.
        assert (await exchange(client, client_id, code, verifier)).json()["error"] == "invalid_grant"


async def test_a_code_used_twice_revokes_what_it_gave(serving: httpx.AsyncClient) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415

    issued = await new_pass(serving)
    client_id = await register(serving)
    verifier, challenge = pkce()
    code = code_of(await approve_with_pass(serving, authorization(client_id, challenge), issued["key"]))
    tokens = (await exchange(serving, client_id, code, verifier)).json()
    assert await mcp_status(serving, tokens["access_token"]) == 200

    again = await exchange(serving, client_id, code, verifier)

    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert await mcp_status(serving, tokens["access_token"]) == 401


async def test_an_expired_code_and_an_expired_token(serving: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415

    issued = await new_pass(serving)
    client_id = await register(serving)
    verifier, challenge = pkce()
    code = code_of(await approve_with_pass(serving, authorization(client_id, challenge), issued["key"]))
    await pool.execute("UPDATE oauth_codes SET expires_at = NOW() - INTERVAL '1 second'")
    assert (await exchange(serving, client_id, code, verifier)).json()["error"] == "invalid_grant"

    _, _, tokens = await signed_in(serving, developer=False)
    await pool.execute("UPDATE oauth_grants SET access_expires_at = NOW() - INTERVAL '1 second'")
    refused = await serving.post("/mcp", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert refused.status_code == 401 and 'error="invalid_token"' in refused.headers["WWW-Authenticate"]
    # The refresh token still works, as Claude expects after a 401.
    assert (await refresh(serving, client_id, tokens["refresh_token"])).status_code == 400  # another client's
    renewed = await refresh(
        serving, (await pool.fetchval("SELECT client_id FROM oauth_grants")), tokens["refresh_token"]
    )
    assert renewed.status_code == 200

    await pool.execute("UPDATE oauth_grants SET refresh_expires_at = NOW() - INTERVAL '1 second'")
    expired = await refresh(
        serving, await pool.fetchval("SELECT client_id FROM oauth_grants"), renewed.json()["refresh_token"]
    )
    assert expired.json()["error"] == "invalid_grant"


async def test_a_stolen_refresh_token_used_after_rotation_revokes_the_grant(
    serving: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    client_id, _, tokens = await signed_in(serving)
    fresh = (await refresh(serving, client_id, tokens["refresh_token"])).json()
    await pool.execute("UPDATE oauth_grants SET rotated_at = NOW() - INTERVAL '2 minutes'")  # past the grace

    assert (await refresh(serving, client_id, tokens["refresh_token"])).json()["error"] == "invalid_grant"

    assert await mcp_status(serving, fresh["access_token"]) == 401
    assert (await refresh(serving, client_id, fresh["refresh_token"])).json()["error"] == "invalid_grant"


async def test_replacing_the_pass_from_its_backup_code_ends_the_grant(serving: httpx.AsyncClient) -> None:
    client_id, issued, tokens = await signed_in(serving)

    recovered = await serving.post("/v1/keys/recover", json={"recovery_code": issued["recovery_code"]})
    assert recovered.status_code == 200

    assert await mcp_status(serving, tokens["access_token"]) == 401
    assert (await refresh(serving, client_id, tokens["refresh_token"])).json()["error"] == "invalid_grant"


async def test_retiring_the_pass_deletes_its_grants(serving: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    _, _, tokens = await signed_in(serving)
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    await retire.retire_passes(
        pool,
        before=datetime.now(UTC) + timedelta(days=1),
        now=datetime.now(UTC) + timedelta(days=2),
        keep=[],
        confirm=True,
        settings=settings,
    )

    assert await pool.fetchval("SELECT count(*) FROM oauth_grants") == 0
    assert await mcp_status(serving, tokens["access_token"]) == 401


async def test_the_token_endpoint_takes_forms_only(client: httpx.AsyncClient) -> None:
    response = await client.post("/oauth/token", json={"grant_type": "refresh_token"})
    assert response.status_code == 400 and response.json()["error"] == "invalid_request"
    unknown = await client.post("/oauth/token", data={"grant_type": "password", "client_id": "ufc_x"})
    assert unknown.json()["error"] == "unsupported_grant_type"


async def test_purge_keeps_live_grants(serving: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    await signed_in(serving)
    await register(serving)  # a client that never signed anyone in
    await pool.execute("UPDATE oauth_clients SET created_on = CURRENT_DATE - 60")

    await oauth.purge(pool)

    assert await pool.fetchval("SELECT count(*) FROM oauth_clients") == 1  # the one with a grant
    assert await pool.fetchval("SELECT count(*) FROM oauth_grants") == 1


def test_redirect_rules() -> None:
    assert oauth.redirect_allowed(CLAUDE) and oauth.redirect_allowed("https://claude.com/api/mcp/auth_callback")
    assert oauth.redirect_allowed("http://[::1]:8080/cb") and oauth.redirect_allowed("http://localhost:1/cb")
    for uri in (CLAUDE + "?x=1", CLAUDE + "#f", "https://user@claude.ai/api/mcp/auth_callback", "javascript:alert(1)"):
        assert not oauth.redirect_allowed(uri), uri
    assert oauth.redirect_matches("http://localhost:4000/callback", ["http://localhost/callback"])
    assert not oauth.redirect_matches("http://localhost:4000/other", ["http://localhost/callback"])
    assert not oauth.redirect_matches("https://claude.ai:444/api/mcp/auth_callback", [CLAUDE])


async def test_the_in_process_stand_in_works_only_inside_its_call(client: httpx.AsyncClient) -> None:
    from tests.integration.test_check_pass import new_pass  # noqa: PLC0415
    from unfret.credits import keys  # noqa: PLC0415

    issued = await new_pass(client)
    with keys.internal_pass(issued["account"]) as stand_in:
        inside = await client.get("/v1/credits", headers={"Authorization": f"Bearer {stand_in}"})
    after = await client.get("/v1/credits", headers={"Authorization": f"Bearer {stand_in}"})
    forged = await client.get("/v1/credits", headers={"Authorization": "Bearer ufm_" + "a" * 43})

    assert inside.status_code == 200 and after.status_code == 401 and forged.status_code == 401
    # /mcp never takes one from outside.
    assert (await client.post("/mcp", json={}, headers={"Authorization": f"Bearer {stand_in}"})).status_code in {
        401,
        503,
    }


async def test_the_developer_page_mentions_signing_in_only_while_it_is_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    on = (await client.get("/developers")).text
    monkeypatch.setattr(settings, "mcp_oauth_enabled", False)
    off = (await client.get("/developers")).text

    assert 'id="mcp-oauth"' in on and "oauth-on" not in on
    assert 'id="mcp-oauth"' not in off and "oauth-on" not in off


async def test_failed_code_exchanges_are_limited_but_refreshes_are_not(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth_routes, "_bad_codes", RateLimit(2))
    for _ in range(4):  # a grant that ended: normal, never limited
        assert (await refresh(client, "ufc_x", "ufrt_gone")).status_code == 400
    statuses = [(await exchange(client, "ufc_x", "nope", "v" * 43)).status_code for _ in range(3)]
    assert statuses == [400, 400, 429]
