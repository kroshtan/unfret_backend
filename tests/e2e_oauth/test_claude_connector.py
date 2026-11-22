"""
The remote MCP server's OAuth sign-in on a deployed dev, driven the way Claude's connector client drives it.

``make e2e-oauth`` (marker ``e2e_oauth``; never part of ``make test``, the dev sweep or the production gate) with
``UNFRET_E2E_OAUTH_URL`` (dev's base URL, e.g. ``https://dev.unfret.net``; production's host is refused). Everything
skips without it. It follows Claude's documented client behaviour (claude.com/docs/connectors/building/authentication
and .../lazy-authentication) and the MCP authorization spec (2026-07-28):

1. Lazy sign-in: ``initialize``, ``tools/list`` and ``get_pricing`` without credentials; a check gets the 401 whose
   ``WWW-Authenticate`` (``error="invalid_token"``, ``resource_metadata``) makes Claude show its Connect card.
2. Discovery from that header: RFC 9728 (``resource`` is the MCP URL exactly) and RFC 8414 (issuer, PKCE S256, grant
   types, public clients, CIMD and DCR).
3. Clients: Claude's real Client ID Metadata Documents, fetched by the server (claude.ai's,
   ``https://claude.ai/oauth/mcp-oauth-client-metadata``, with ``https://claude.ai/api/mcp/auth_callback``, which
   Claude uses since the server advertises CIMD and ``none``; and Claude Code's,
   ``https://claude.ai/oauth/claude-code-client-metadata``, with an ephemeral loopback port), and Dynamic Client
   Registration (Claude's fallback) with Claude's callback and with a loopback one.
4. The sign-in page driven over HTTP as a browser would: **Continue** (the page's proof of work solved here, a new pass
   made, the code taken from the "Continue to claude.ai" link) and **I already have a pass** (that same pass pasted;
   the code taken from the 303 without following it).
5. Tokens: the form-encoded code exchange with PKCE and ``resource``, ``iss`` on the redirect (RFC 9207), ``/mcp``
   with the token, refresh rotation (a rotated-away token refused inside the grace and the grant revoked when it is
   presented after it), revocation, and the exact error shapes.

Only free things are used: quick checks on inline page content and ``get_checks_left``. Each run makes one new pass on
dev (the free checks it comes with are never spent) and revokes every grant it made, except the one the
grace-period test revokes by itself.
"""

import base64
import hashlib
import html
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytestmark = pytest.mark.e2e_oauth

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_AI_CIMD = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CODE_CIMD = "https://claude.ai/oauth/claude-code-client-metadata"
PROTOCOL = "2025-06-18"  # an initialize handshake version Claude's client sends
PAGE = "<html><head><title>Wireless earbuds</title></head><body><h1>Wireless earbuds</h1><p>$19.99</p></body></html>"
REFRESH_GRACE_S = 60  # unfret.oauth.REFRESH_GRACE_S
PRODUCTION_HOSTS = frozenset({"unfret.net", "www.unfret.net"})


def _base() -> str:
    base = os.environ.get("UNFRET_E2E_OAUTH_URL", "").strip().rstrip("/")
    if not base:
        pytest.skip("UNFRET_E2E_OAUTH_URL is not set")
    if (urlsplit(base).hostname or "") in PRODUCTION_HOSTS:
        pytest.fail("UNFRET_E2E_OAUTH_URL points at production; this test runs against dev only")
    return base


@pytest.fixture(scope="module")
def base() -> str:
    return _base()


@pytest.fixture(scope="module")
def http(base: str) -> Iterator[httpx.Client]:
    # Claude's servers time discovery, registration and tokens out after 10 s; the same budget here.
    with httpx.Client(base_url=base, timeout=httpx.Timeout(10, read=60), follow_redirects=False) as client:
        yield client


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def solve(challenge: str, bits: int) -> str:
    """The page's proof of work (``new-pass.js``): the first n whose SHA-256(challenge:n) has ``bits`` zero bits."""
    prefix = hashlib.sha256(f"{challenge}:".encode())
    n = 0
    while True:
        h = prefix.copy()
        h.update(str(n).encode())
        if int.from_bytes(h.digest()[:8]) >> (64 - bits) == 0:
            return str(n)
        n += 1


def mcp(http: httpx.Client, method: str, params: dict[str, Any] | None = None, token: str = "") -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": secrets.randbelow(10**6), "method": method}
    if params is not None:
        body["params"] = params
    return http.post("/mcp", json=body, headers=headers)


def call(http: httpx.Client, tool: str, arguments: dict[str, Any] | None = None, token: str = "") -> httpx.Response:
    return mcp(http, "tools/call", {"name": tool, "arguments": arguments or {}}, token)


def result(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    structured: dict[str, Any] = body["result"].get("structuredContent") or body["result"]
    return structured


def challenge_params(header: str) -> dict[str, str]:
    assert header.startswith("Bearer "), header
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def register(http: httpx.Client, redirect_uris: list[str], application_type: str = "web") -> str:
    # What an MCP client sends at registration (RFC 7591; the 2026-07-28 spec adds application_type).
    response = http.post(
        "/oauth/register",
        json={
            "client_name": "Claude",
            "redirect_uris": redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": application_type,
            "scope": "checks",
        },
    )
    assert response.status_code == 201, response.text
    registered = response.json()
    assert registered["token_endpoint_auth_method"] == "none"
    assert registered["redirect_uris"] == redirect_uris
    assert "client_secret" not in registered
    client_id: str = registered["client_id"]
    return client_id


@dataclass
class Flow:
    """One authorization request, as Claude builds it."""

    base: str
    client_id: str
    redirect_uri: str
    verifier: str
    challenge: str
    state: str

    @classmethod
    def start(cls, base: str, client_id: str, redirect_uri: str) -> Flow:
        verifier, challenge = pkce()
        return cls(base, client_id, redirect_uri, verifier, challenge, secrets.token_urlsafe(16))

    @property
    def params(self) -> dict[str, str]:
        return {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "scope": "checks",
            "resource": f"{self.base}/mcp",
        }

    def code_from(self, location: str) -> str:
        """Check the redirect back to the client as Claude does (state, then ``iss`` exactly) and take the code."""
        sent, expected = urlsplit(location), urlsplit(self.redirect_uri)
        assert (sent.scheme, sent.netloc, sent.path) == (expected.scheme, expected.netloc, expected.path), location
        query = parse_qs(sent.query)
        assert query.get("state") == [self.state], location
        assert query.get("iss") == [self.base], location  # RFC 9207, compared without normalisation
        assert "error" not in query, location
        return query["code"][0]

    def exchange(self, http: httpx.Client, code: str, **override: str) -> httpx.Response:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": self.verifier,
            "resource": f"{self.base}/mcp",
            **override,
        }
        return http.post("/oauth/token", data=form)


def page(http: httpx.Client, flow: Flow) -> str:
    response = http.get("/oauth/authorize", params=flow.params)
    assert response.status_code == 200, response.text
    csp = response.headers["content-security-policy"]
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(flow.redirect_uri))
    assert f"form-action 'self' {origin}" in csp, csp  # the paste form's 303 to the client must not be blocked
    assert "frame-ancestors 'none'" in csp, csp
    return response.text


def hidden_fields(text: str, form_id: str | None = None) -> dict[str, str]:
    if form_id is not None:
        match = re.search(rf'<form id="{form_id}".*?</form>', text, re.DOTALL)
        assert match, f"no form {form_id}"
        text = match.group(0)
    return {
        html.unescape(name): html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', text)
    }


def tokens_ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"].startswith("ufat_") and body["refresh_token"].startswith("ufrt_")
    assert isinstance(body["expires_in"], int) and body["expires_in"] > 300
    assert body["scope"] == "checks"
    return body


def oauth_error(response: httpx.Response, error: str, status: int = 400) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"] == error, body
    assert isinstance(body.get("error_description"), str)


def revoke(http: httpx.Client, token: str, client_id: str) -> None:
    response = http.post(
        "/oauth/revoke", data={"token": token, "token_type_hint": "refresh_token", "client_id": client_id}
    )
    assert response.status_code == 200, response.text


def signed_in_works(http: httpx.Client, access_token: str) -> None:
    listed = mcp(http, "tools/list", token=access_token)
    assert {t["name"] for t in result(listed)["tools"]} >= {"check_store_quick", "get_checks_left"}
    quick = result(
        call(http, "check_store_quick", {"url": "https://example.com/earbuds", "page_content": PAGE}, access_token)
    )
    assert quick["status"] == "ok" and quick["verdict"]["action"] in {"allow", "warn", "block"}, quick
    left = result(call(http, "get_checks_left", token=access_token))
    assert left["status"] == "ok", left


def signed_out(http: httpx.Client, access_token: str) -> None:
    response = call(http, "check_store_quick", {"url": "https://example.com/", "page_content": PAGE}, access_token)
    assert response.status_code == 401, response.text
    assert challenge_params(response.headers["www-authenticate"])["error"] == "invalid_token"


# ── Lazy sign-in and discovery ───────────────────────────────────────────────────────────────────────────────────


def test_connects_and_lists_tools_without_signing_in(http: httpx.Client) -> None:
    for version in ("2025-06-18", "2025-11-25"):
        init = mcp(
            http,
            "initialize",
            {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "claude-ai", "version": "0.1.0"}},
        )
        assert result(init)["serverInfo"]["name"] == "unfret"
    note = http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL},
    )
    assert note.status_code == 202, note.text
    tools = {t["name"]: t for t in result(mcp(http, "tools/list"))["tools"]}
    assert set(tools) == {"check_store_quick", "check_store_deep", "get_checks_left", "get_pricing"}
    for tool in tools.values():
        hints = tool["annotations"]
        assert {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"} <= set(hints), tool["name"]
        assert tool.get("title") and tool.get("description")
    pricing = result(call(http, "get_pricing"))
    assert pricing["status"] == "ok" and pricing["pricing"], pricing


@pytest.mark.parametrize("tool", ["check_store_quick", "check_store_deep", "get_checks_left"])
def test_a_tool_that_needs_a_pass_asks_for_sign_in(http: httpx.Client, base: str, tool: str) -> None:
    response = call(http, tool, {"url": "https://example.com/", "page_content": PAGE} if "check" in tool else {})
    assert response.status_code == 401, response.text  # a transport-level 401: what shows Claude's Connect card
    challenge = challenge_params(response.headers["www-authenticate"])
    assert challenge["error"] == "invalid_token"
    assert challenge["resource_metadata"] == f"{base}/.well-known/oauth-protected-resource/mcp"
    assert challenge["scope"] == "checks"
    # Browser-based MCP clients can read the challenge (Claude itself connects from Anthropic's servers).
    preflight = http.options(
        "/mcp",
        headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version",
        },
    )
    assert (
        preflight.status_code == 200 and "authorization" in preflight.headers["access-control-allow-headers"].lower()
    )
    cors = call(http, tool, {"url": "https://example.com/", "page_content": PAGE} if "check" in tool else {})
    assert cors.status_code == 401


def test_an_unknown_token_is_401_not_a_tool_error(http: httpx.Client) -> None:
    response = call(http, "get_checks_left", token="ufat_" + secrets.token_urlsafe(32))
    assert response.status_code == 401, response.text
    assert challenge_params(response.headers["www-authenticate"])["error"] == "invalid_token"


def test_discovery_from_the_challenge(http: httpx.Client, base: str) -> None:
    header = call(http, "get_checks_left").headers["www-authenticate"]
    url = challenge_params(header)["resource_metadata"]
    resource = http.get(url, headers={"Origin": "https://claude.ai"})
    assert resource.status_code == 200, resource.text
    assert resource.headers.get("access-control-allow-origin") == "*"
    metadata = resource.json()
    assert metadata["resource"] == f"{base}/mcp"  # exactly the URL the person adds
    assert metadata["authorization_servers"][0] == base  # Claude uses only the first
    assert metadata["scopes_supported"] == ["checks"] and "offline_access" not in metadata["scopes_supported"]
    assert http.get("/.well-known/oauth-protected-resource").json() == metadata  # the fallback Claude probes

    server = http.get("/.well-known/oauth-authorization-server")  # issuer without a path: the first URL tried
    assert server.status_code == 200, server.text
    auth = server.json()
    assert auth["issuer"] == base  # must equal what the URL was built from
    for endpoint in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert auth[endpoint].startswith(f"{base}/oauth/"), endpoint
    assert auth["code_challenge_methods_supported"] == ["S256"]
    assert set(auth["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    assert auth["response_types_supported"] == ["code"]
    # Claude uses CIMD only with both of these; otherwise DCR (registration_endpoint, above).
    assert auth["client_id_metadata_document_supported"] is True
    assert "none" in auth["token_endpoint_auth_methods_supported"]
    assert auth["authorization_response_iss_parameter_supported"] is True


# ── Clients ──────────────────────────────────────────────────────────────────────────────────────────────────────


def test_registration_refusals(http: httpx.Client) -> None:
    oauth_error(
        http.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]}), "invalid_redirect_uri"
    )
    oauth_error(
        http.post(
            "/oauth/register",
            json={"redirect_uris": [CLAUDE_CALLBACK], "token_endpoint_auth_method": "client_secret_basic"},
        ),
        "invalid_client_metadata",
    )
    oauth_error(
        http.post(
            "/oauth/register",
            content=b"redirect_uris=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        "invalid_client_metadata",
    )


def test_claude_codes_metadata_document_is_accepted(http: httpx.Client, base: str) -> None:
    # Claude Code identifies itself by this URL; the server fetches it. Its redirect URIs carry no port.
    for redirect_uri in ("http://localhost:54321/callback", "http://127.0.0.1:3118/callback"):
        text = page(http, Flow.start(base, CLAUDE_CODE_CIMD, redirect_uri))
        assert "Connect Unfret to claude.ai" in text  # a CIMD client is named by its host
        assert "program on your own computer" in text  # the loopback warning
    # A redirect URI the document doesn't list is refused on the page, never redirected to.
    refused = http.get("/oauth/authorize", params=Flow.start(base, CLAUDE_CODE_CIMD, CLAUDE_CALLBACK).params)
    assert refused.status_code == 400 and "location" not in refused.headers


def test_authorization_request_refusals(http: httpx.Client, base: str) -> None:
    client_id = register(http, [CLAUDE_CALLBACK])
    # A redirect the client didn't register: a page, never a redirect.
    stray = http.get(
        "/oauth/authorize",
        params={**Flow.start(base, client_id, CLAUDE_CALLBACK).params, "redirect_uri": "https://claude.ai/other"},
    )
    assert stray.status_code == 400 and "location" not in stray.headers
    unknown = http.get("/oauth/authorize", params=Flow.start(base, "ufc_nope", CLAUDE_CALLBACK).params)
    assert unknown.status_code == 400 and "location" not in unknown.headers
    # No PKCE, or another resource: sent back to the client with the error, the state and iss.
    for change, error in (
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"resource": "https://example.com/mcp"}, "invalid_target"),
    ):
        flow = Flow.start(base, client_id, CLAUDE_CALLBACK)
        response = http.get("/oauth/authorize", params={**flow.params, **change})
        assert response.status_code == 303, response.text
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["error"] == [error] and query["state"] == [flow.state] and query["iss"] == [base]


# ── Signing in ───────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def new_pass(http: httpx.Client, base: str) -> Iterator[tuple[str, Flow, str]]:
    """
    Claude's hosted flow with **Continue**: claude.ai's metadata document, its callback, the proof of work.

    :yield: The new pass, the flow and its code (taken from the "Continue to claude.ai" link, not followed).
    """
    flow = Flow.start(base, CLAUDE_AI_CIMD, CLAUDE_CALLBACK)
    text = page(http, flow)
    assert "Connect Unfret to claude.ai" in text and "program on your own computer" not in text
    fields = hidden_fields(text, "create-form")
    assert fields["action"] == "create" and fields["state"] == flow.state
    work = http.get("/v1/keys/challenge", headers={"Accept": "application/json"})  # what the page's script fetches
    assert work.status_code == 200, work.text
    pow_challenge = work.json()
    fields |= {
        "challenge": pow_challenge["challenge"],
        "solution": solve(pow_challenge["challenge"], pow_challenge["bits"]),
    }
    made = http.post("/oauth/authorize", data=fields, headers={"Origin": base, "Referer": f"{base}/oauth/authorize"})
    assert made.status_code == 200, made.text
    assert made.headers["cache-control"].startswith("no-store")
    key = re.search(r'<code id="new-pass">(ufk_[^<]+)</code>', made.text)
    link = re.search(r'<a class="button primary large" href="([^"]+)">Continue to claude\.ai</a>', made.text)
    assert key and link, made.text
    yield key.group(1), flow, flow.code_from(html.unescape(link.group(1)))


def test_continue_signs_in_refreshes_and_revokes(http: httpx.Client, new_pass: tuple[str, Flow, str]) -> None:
    _, flow, code = new_pass
    # The token endpoint takes forms only (RFC 6749 §4.1.3); JSON is refused with an OAuth error, not a 415/422.
    oauth_error(
        http.post(
            "/oauth/token", json={"grant_type": "authorization_code", "code": code, "client_id": flow.client_id}
        ),
        "invalid_request",
    )
    first = tokens_ok(flow.exchange(http, code))
    signed_in_works(http, first["access_token"])
    # The code works once; using it again revokes what it gave.
    oauth_error(flow.exchange(http, code), "invalid_grant")
    signed_out(http, first["access_token"])
    oauth_error(
        http.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": flow.client_id},
        ),
        "invalid_grant",
    )


def test_refresh_rotation_and_revocation(http: httpx.Client, base: str, new_pass: tuple[str, Flow, str]) -> None:
    key, _, _ = new_pass
    client_id = register(http, [CLAUDE_CALLBACK])
    flow = Flow.start(base, client_id, CLAUDE_CALLBACK)
    fields = hidden_fields(page(http, flow))
    pasted = http.post("/oauth/authorize", data={**fields, "action": "pass", "pass": key})
    assert pasted.status_code == 303, pasted.text
    tokens = tokens_ok(flow.exchange(http, flow.code_from(pasted.headers["location"])))

    def refresh(token: str, **extra: str) -> httpx.Response:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": client_id,
            "resource": f"{base}/mcp",
            **extra,
        }
        return http.post("/oauth/token", data=form)

    rotated = tokens_ok(refresh(tokens["refresh_token"]))
    assert rotated["refresh_token"] != tokens["refresh_token"] and rotated["access_token"] != tokens["access_token"]
    signed_out(http, tokens["access_token"])  # the old access token is replaced
    signed_in_works(http, rotated["access_token"])
    # Within the grace, the rotated-away token is refused but the grant lives (a client's own concurrent refresh).
    oauth_error(refresh(tokens["refresh_token"]), "invalid_grant")
    signed_in_works(http, rotated["access_token"])
    oauth_error(refresh(rotated["refresh_token"], client_id="ufc_someone_else"), "invalid_grant")
    oauth_error(refresh(rotated["refresh_token"], resource="https://example.com/mcp"), "invalid_target")
    oauth_error(
        http.post("/oauth/token", data={"grant_type": "password", "client_id": client_id}), "unsupported_grant_type"
    )
    oauth_error(
        http.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": rotated["refresh_token"]}),
        "invalid_request",
    )
    # Revoking by the refresh token ends the grant: both tokens stop working. An unknown token is still 200.
    again = tokens_ok(refresh(rotated["refresh_token"]))
    revoke(http, again["refresh_token"], client_id)
    signed_out(http, again["access_token"])
    oauth_error(refresh(again["refresh_token"]), "invalid_grant")
    revoke(http, "ufrt_" + secrets.token_urlsafe(32), client_id)
    oauth_error(http.post("/oauth/revoke", data={}), "invalid_request")


def test_a_rotated_away_token_after_the_grace_revokes_the_grant(
    http: httpx.Client, base: str, new_pass: tuple[str, Flow, str]
) -> None:
    key, _, _ = new_pass
    client_id = register(http, [CLAUDE_CALLBACK])
    flow = Flow.start(base, client_id, CLAUDE_CALLBACK)
    pasted = http.post("/oauth/authorize", data={**hidden_fields(page(http, flow)), "action": "pass", "pass": key})
    tokens = tokens_ok(flow.exchange(http, flow.code_from(pasted.headers["location"])))
    form = {"grant_type": "refresh_token", "client_id": client_id}
    rotated = tokens_ok(http.post("/oauth/token", data={**form, "refresh_token": tokens["refresh_token"]}))
    time.sleep(REFRESH_GRACE_S + 5)
    oauth_error(http.post("/oauth/token", data={**form, "refresh_token": tokens["refresh_token"]}), "invalid_grant")
    # Someone else may hold the old token: the whole grant is gone, the current tokens too.
    signed_out(http, rotated["access_token"])
    oauth_error(http.post("/oauth/token", data={**form, "refresh_token": rotated["refresh_token"]}), "invalid_grant")


def test_claude_code_with_a_pass_it_already_has(
    http: httpx.Client, base: str, new_pass: tuple[str, Flow, str]
) -> None:
    """Claude Code: its metadata document, an ephemeral loopback port, and the pasted pass; the 303 isn't followed."""
    key, _, _ = new_pass
    flow = Flow.start(base, CLAUDE_CODE_CIMD, f"http://localhost:{secrets.randbelow(20000) + 40000}/callback")
    fields = hidden_fields(page(http, flow))
    wrong = http.post(
        "/oauth/authorize", data={**fields, "action": "pass", "pass": "ufk_" + secrets.token_urlsafe(32)}
    )
    assert wrong.status_code == 400 and "isn't a valid Unfret pass" in html.unescape(wrong.text)
    pasted = http.post("/oauth/authorize", data={**fields, "action": "pass", "pass": key})
    assert pasted.status_code == 303, pasted.text
    code = flow.code_from(pasted.headers["location"])
    oauth_error(
        flow.exchange(http, code, code_verifier=pkce()[0]), "invalid_grant"
    )  # a wrong verifier spends the code
    oauth_error(flow.exchange(http, code), "invalid_grant")

    flow = Flow.start(base, CLAUDE_CODE_CIMD, "http://127.0.0.1:3118/callback")
    pasted = http.post("/oauth/authorize", data={**hidden_fields(page(http, flow)), "action": "pass", "pass": key})
    code = flow.code_from(pasted.headers["location"])
    oauth_error(flow.exchange(http, code, redirect_uri="http://127.0.0.1:3119/callback"), "invalid_grant")
    flow2 = Flow.start(base, CLAUDE_CODE_CIMD, "http://127.0.0.1:3118/callback")
    pasted = http.post("/oauth/authorize", data={**hidden_fields(page(http, flow2)), "action": "pass", "pass": key})
    tokens = tokens_ok(
        flow2.exchange(http, flow2.code_from(pasted.headers["location"]), resource="")
    )  # resource optional
    signed_in_works(http, tokens["access_token"])
    revoke(http, tokens["access_token"], CLAUDE_CODE_CIMD)  # by the access token, this time
    signed_out(http, tokens["access_token"])


def test_dcr_with_a_loopback_redirect(http: httpx.Client, base: str, new_pass: tuple[str, Flow, str]) -> None:
    key, _, _ = new_pass
    client_id = register(http, ["http://127.0.0.1/callback"], application_type="native")
    flow = Flow.start(base, client_id, "http://127.0.0.1:61234/callback")
    pasted = http.post("/oauth/authorize", data={**hidden_fields(page(http, flow)), "action": "pass", "pass": key})
    assert pasted.status_code == 303, pasted.text
    tokens = tokens_ok(flow.exchange(http, flow.code_from(pasted.headers["location"])))
    signed_in_works(http, tokens["access_token"])
    revoke(http, tokens["refresh_token"], client_id)
    signed_out(http, tokens["access_token"])
