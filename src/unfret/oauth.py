"""
OAuth 2.1 for the remote MCP server (``/mcp``): Unfret is its own authorization server, and a token stands for a pass.

Hosted MCP clients (Claude's connectors and the like) can't be handed an ``Authorization`` header; they sign in with
OAuth instead. The person approving picks the Unfret pass the client may use, by pasting one or by making a new free
pass on the authorization page (:mod:`unfret.routes.oauth`), and the client gets tokens that act for that pass at
``/mcp`` and nowhere else. ``Bearer ufk_…`` keeps working at ``/mcp`` as before.

- **Clients** register with a Client ID Metadata Document (an HTTPS ``client_id`` fetched here, which the current MCP
  specification prefers) or with Dynamic Client Registration (RFC 7591). Either way they are public clients (no
  secret) and every redirect URI must be one :func:`redirect_allowed` accepts: Claude's callbacks (setting
  ``mcp_oauth_redirect_uris``) and, for local clients such as Claude Code, a loopback address on any port.
- **Codes** are single-use, short-lived and bound to the client, the redirect URI, a PKCE S256 challenge and the
  pass. Using one twice revokes what the first use issued.
- **Tokens** are opaque random strings: an access token (``ufat_…``, short-lived) and a refresh token (``ufrt_…``)
  that rotates on every use. Presenting a refresh token that was already rotated away revokes the grant (after a
  short grace for a client's own concurrent refreshes).
- **Revocation**: ``/oauth/revoke``; and a grant stops working at once when its pass is replaced from its backup
  code (the grant is bound to the pass's current hash) or retired (its rows are deleted with the pass).

Only SHA-256 hashes of codes and tokens are stored, with the pass's id, the client and expiry times; nothing about
the person, their network or what they check. A registered client keeps the name and redirect URIs it sent.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import asyncpg

from unfret import net
from unfret.config import Settings, settings
from unfret.credits import keys

type Executor = asyncpg.Pool | asyncpg.Connection

ACCESS_PREFIX = "ufat_"
REFRESH_PREFIX = "ufrt_"
CLIENT_PREFIX = "ufc_"
SCOPE = "checks"  # the one scope: use the pass's checks through the MCP server's tools
MCP_PATH = "/mcp"
# A client's concurrent refreshes (proactive and on a 401) may both present the token the first one rotated away:
# within this many seconds of a rotation that is refused but doesn't revoke the grant.
REFRESH_GRACE_S = 60
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9\-_]{43}$")  # base64url of a SHA-256, unpadded
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
_MAX_REDIRECT_URIS = 10
_MAX_NAME = 100
_CIMD_MAX_BYTES = 16 * 1024
_CIMD_TTL_S = 300.0


class OAuthError(ValueError):
    """An OAuth request refused, with its RFC 6749 error code."""

    def __init__(self, error: str, description: str) -> None:
        """
        Make the error.

        :param error: The error code (``invalid_request``, ``invalid_grant``, …).
        :param description: What was wrong, for a developer.
        """
        super().__init__(description)
        self.error = error
        self.description = description


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def issuer(config: Settings = settings) -> str:
    """
    The authorization server's issuer: the site's base URL.

    :param config: The settings.
    :return: E.g. ``https://unfret.net``.
    """
    return config.public_base_url.rstrip("/")


def resource(config: Settings = settings) -> str:
    """
    The protected resource (RFC 8707, RFC 9728): the MCP server's URL as clients add it.

    :param config: The settings.
    :return: E.g. ``https://unfret.net/mcp``.
    """
    return issuer(config) + MCP_PATH


def resource_metadata_url(config: Settings = settings) -> str:
    """
    Where the protected resource metadata is (the path-suffixed form, RFC 9728 §3.1).

    :param config: The settings.
    :return: The URL a 401's ``WWW-Authenticate`` points at.
    """
    return f"{issuer(config)}/.well-known/oauth-protected-resource{MCP_PATH}"


def protected_resource_metadata(config: Settings = settings) -> dict[str, object]:
    """
    RFC 9728 metadata for ``/mcp``.

    :param config: The settings.
    :return: The document.
    """
    return {
        "resource": resource(config),
        "authorization_servers": [issuer(config)],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [SCOPE],
        "resource_name": "Unfret",
        "resource_documentation": f"{issuer(config)}/developers#mcp",
    }


def authorization_server_metadata(config: Settings = settings) -> dict[str, object]:
    """
    RFC 8414 metadata: public clients only, PKCE S256, CIMD and DCR, the ``iss`` response parameter (RFC 9207).

    :param config: The settings.
    :return: The document.
    """
    base = issuer(config)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": f"{base}/developers#mcp",
    }


def challenge_header(error: str | None = None, description: str = "") -> str:
    """
    The ``WWW-Authenticate`` value of a 401 at ``/mcp`` while OAuth is on: where to sign in (RFC 9728 §5.1).

    :param error: ``invalid_token`` for a token that didn't work; ``None`` when there was none.
    :param description: What was wrong (no double quotes).
    :return: The header value.
    """
    parts = ['Bearer realm="Unfret"']
    if error:
        parts.append(f'error="{error}"')
        if description:
            parts.append(f'error_description="{description}"')
    parts += [f'resource_metadata="{resource_metadata_url()}"', f'scope="{SCOPE}"']
    return ", ".join(parts)


# ── Redirect URIs ────────────────────────────────────────────────────────────────────────────────────────────────


def _is_loopback(uri: str) -> bool:
    parts = urlsplit(uri)
    return parts.scheme == "http" and (parts.hostname or "") in _LOOPBACK_HOSTS | {"::1"}


def _without_port(uri: str) -> tuple[str, str, str, str]:
    parts = urlsplit(uri)
    host = parts.hostname or ""
    return parts.scheme, host, parts.path or "/", parts.query


def _well_formed(uri: str) -> bool:
    if len(uri) > 512 or any(c.isspace() for c in uri):
        return False
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return False
    return bool(parts.scheme and parts.hostname) and not parts.fragment and not parts.username and not parts.password


def redirect_allowed(uri: str, config: Settings = settings) -> bool:
    """
    Whether a redirect URI is one Unfret sends codes to: a configured one exactly, or a loopback address (any port).

    Loopback is for native clients on the user's own machine (Claude Code binds an ephemeral port, RFC 8252 §7.3);
    ``localhost`` is accepted like ``127.0.0.1`` because Claude Code declares it.

    :param uri: The redirect URI.
    :param config: The settings.
    :return: ``True`` if allowed.
    """
    if not _well_formed(uri):
        return False
    if uri in config.mcp_oauth_redirect_uris:
        return True
    return config.mcp_oauth_loopback_redirects and _is_loopback(uri)


def redirect_matches(requested: str, registered: list[str]) -> bool:
    """
    Whether a requested redirect URI is one the client registered: exactly, or a loopback one with any port.

    :param requested: The ``redirect_uri`` of the request.
    :param registered: The client's redirect URIs.
    :return: ``True`` if it matches.
    """
    if requested in registered:
        return True
    if not _is_loopback(requested) or not _well_formed(requested):
        return False
    wanted = _without_port(requested)
    return any(_is_loopback(uri) and _without_port(uri) == wanted for uri in registered)


def redirect_origin(uri: str) -> str:
    """
    A redirect URI's origin, for the authorization page's ``form-action``.

    :param uri: An allowed redirect URI.
    :return: ``scheme://host[:port]``.
    """
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


def redirect_host(uri: str) -> str:
    """
    The host a redirect URI sends the person back to, shown on the authorization page.

    :param uri: The redirect URI.
    :return: The host name.
    """
    return urlsplit(uri).hostname or uri


def is_loopback(uri: str) -> bool:
    """
    Whether a redirect URI is a loopback address (a program on the person's own computer).

    :param uri: The redirect URI.
    :return: ``True`` for ``http://localhost``, ``127.0.0.1`` or ``[::1]``.
    """
    return _is_loopback(uri)


# ── Clients ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Client:
    """A client as the authorization page shows it and the token endpoint checks it."""

    client_id: str
    name: str
    redirect_uris: list[str]
    metadata_document: bool  # identified by an HTTPS client_id (CIMD) rather than registered here

    @property
    def shown_as(self) -> str:
        """
        Who asks, as the page names it: a CIMD client's host (the document itself is self-asserted), else its name.

        :return: The text.
        """
        if self.metadata_document:
            return urlsplit(self.client_id).hostname or self.client_id
        return self.name or "An MCP client"


def _validate_redirects(uris: object, config: Settings) -> list[str]:
    if not isinstance(uris, list) or not uris or len(uris) > _MAX_REDIRECT_URIS:
        raise OAuthError("invalid_redirect_uri", f"redirect_uris: 1 to {_MAX_REDIRECT_URIS} URIs.")
    for uri in uris:
        if not isinstance(uri, str) or not redirect_allowed(uri, config):
            raise OAuthError(
                "invalid_redirect_uri",
                "Unfret only redirects to Claude's callbacks (https://claude.ai/api/mcp/auth_callback) and loopback "
                f"addresses: {str(uri)[:100]!r} is neither.",
            )
    return list(uris)


def _client_name(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise OAuthError("invalid_client_metadata", "client_name must be a string.")
    return " ".join(value.split())[:_MAX_NAME]


async def register(db: Executor, metadata: dict[str, object], config: Settings = settings) -> dict[str, object]:
    """
    Dynamic Client Registration (RFC 7591) of a public client.

    :param db: A pool or connection.
    :param metadata: The client's metadata (``redirect_uris``, ``client_name``, …).
    :param config: The settings.
    :return: The registration response.
    :raises OAuthError: For metadata Unfret doesn't accept (a secret-based client, a redirect elsewhere, …).
    """
    redirect_uris = _validate_redirects(metadata.get("redirect_uris"), config)
    method = metadata.get("token_endpoint_auth_method", "none")
    if method != "none":
        raise OAuthError("invalid_client_metadata", "Only public clients: token_endpoint_auth_method must be none.")
    grants = metadata.get("grant_types", ["authorization_code"])
    if not isinstance(grants, list) or not set(grants) <= {"authorization_code", "refresh_token"}:
        raise OAuthError("invalid_client_metadata", "grant_types: authorization_code and refresh_token only.")
    responses = metadata.get("response_types", ["code"])
    if not isinstance(responses, list) or not set(responses) <= {"code"}:
        raise OAuthError("invalid_client_metadata", "response_types: code only.")
    name = _client_name(metadata.get("client_name"))
    client_id = CLIENT_PREFIX + secrets.token_urlsafe(24)
    await db.execute(
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris) VALUES ($1, $2, $3)",
        client_id,
        name,
        redirect_uris,
    )
    return {
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }


# A Client ID Metadata Document's fetcher: (url) → body. net.fetch_public refuses private addresses at every hop.
type Fetcher = Callable[[str], Awaitable[bytes]]


async def _fetch_document(url: str) -> bytes:
    return await net.fetch_public(
        url, timeout=5.0, total_timeout=8.0, max_bytes=_CIMD_MAX_BYTES, headers={"Accept": "application/json"}
    )


fetch_document: Fetcher = _fetch_document
_documents: dict[str, tuple[float, Client]] = {}


def _cimd_url_ok(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    segments = parts.path.split("/")
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.path not in ("", "/")
        and not parts.fragment
        and not parts.username
        and not parts.password
        and "." not in segments
        and ".." not in segments
        and len(url) <= 512
    )


async def _metadata_document(url: str) -> Client:
    now = time.monotonic()
    cached = _documents.get(url)
    if cached and cached[0] > now:
        return cached[1]
    if not _cimd_url_ok(url):
        raise OAuthError("invalid_client", "A URL client_id must be an https URL with a path.")
    try:
        document = json.loads(await fetch_document(url))
    except (ValueError, OSError, TimeoutError) as exc:
        raise OAuthError("invalid_client", "The client's metadata document could not be fetched or read.") from exc
    except Exception as exc:  # httpx errors and refusals: all mean the same here
        raise OAuthError("invalid_client", "The client's metadata document could not be fetched.") from exc
    if not isinstance(document, dict) or document.get("client_id") != url:
        raise OAuthError("invalid_client", "The metadata document's client_id is not its own URL.")
    if document.get("token_endpoint_auth_method", "none") != "none":
        raise OAuthError("invalid_client", "Only public clients: token_endpoint_auth_method must be none.")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        raise OAuthError("invalid_client", "The metadata document has no redirect_uris.")
    client = Client(url, _client_name(document.get("client_name")), list(uris)[:50], metadata_document=True)
    if len(_documents) > 1000:
        _documents.clear()
    _documents[url] = (now + _CIMD_TTL_S, client)
    return client


def needs_fetch(client_id: str) -> bool:
    """
    Whether looking a client up would fetch its metadata document (an HTTPS ``client_id`` not cached).

    :param client_id: The ``client_id`` as sent.
    :return: ``True`` if it would.
    """
    cached = _documents.get(client_id)
    return client_id.startswith("https://") and not (cached and cached[0] > time.monotonic())


async def client(db: Executor, client_id: str, config: Settings = settings) -> Client:
    """
    The client behind a ``client_id``: a registered one, or an HTTPS URL's metadata document (fetched, cached briefly).

    :param db: A pool or connection.
    :param client_id: The ``client_id`` as sent.
    :param config: The settings.
    :return: The client.
    :raises OAuthError: ``invalid_client`` for an unknown client or a document that doesn't qualify.
    """
    if client_id.startswith("https://"):
        return await _metadata_document(client_id)
    if not client_id.startswith(CLIENT_PREFIX) or len(client_id) > 100:
        raise OAuthError("invalid_client", "Unknown client_id.")
    row = await db.fetchrow("SELECT client_name, redirect_uris FROM oauth_clients WHERE client_id = $1", client_id)
    if row is None:
        raise OAuthError("invalid_client", "Unknown client_id; register again.")
    return Client(client_id, row["client_name"], list(row["redirect_uris"]), metadata_document=False)


def check_redirect(found: Client, redirect_uri: str, config: Settings = settings) -> None:
    """
    Refuse a redirect URI the client didn't register or Unfret doesn't send codes to.

    :param found: The client.
    :param redirect_uri: The requested redirect URI.
    :param config: The settings.
    :raises OAuthError: ``invalid_request`` (shown on the page, never redirected to).
    """
    if not redirect_uri or not redirect_matches(redirect_uri, found.redirect_uris):
        raise OAuthError("invalid_request", "redirect_uri is not one of this client's redirect URIs.")
    if not redirect_allowed(redirect_uri, config):
        raise OAuthError("invalid_request", "Unfret doesn't send authorization codes to this redirect URI.")


def check_resource(value: str | None, config: Settings = settings) -> None:
    """
    Refuse a ``resource`` (RFC 8707) other than the MCP server: tokens are only ever for ``/mcp``.

    :param value: The parameter, if sent.
    :param config: The settings.
    :raises OAuthError: ``invalid_target``.
    """
    if not value:
        return
    parts = urlsplit(value)
    canonical = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip('/')}"
    if parts.query or parts.fragment or canonical != resource(config):
        raise OAuthError("invalid_target", f"The only resource here is {resource(config)}.")


def pkce_ok(verifier: str, challenge: str) -> bool:
    """
    Whether a PKCE ``code_verifier`` answers an S256 ``code_challenge`` (RFC 7636).

    :param verifier: The verifier.
    :param challenge: The challenge stored with the code.
    :return: ``True`` if it does.
    """
    if not _VERIFIER_RE.fullmatch(verifier):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(computed, challenge)


def challenge_ok(challenge: str, method: str) -> bool:
    """
    Whether an authorization request's PKCE challenge is an S256 one (plain is refused).

    :param challenge: ``code_challenge``.
    :param method: ``code_challenge_method``.
    :return: ``True`` if usable.
    """
    return method == "S256" and bool(_CHALLENGE_RE.fullmatch(challenge))


# ── Codes and tokens ─────────────────────────────────────────────────────────────────────────────────────────────


async def pass_hash(db: Executor, pass_key: str) -> tuple[str, bytes]:
    """
    Check a pass pasted on the authorization page.

    :param db: A pool or connection.
    :param pass_key: The pass (``ufk_…``).
    :return: Its id and the current hash of its key.
    :raises keys.ApiKeyError: For anything but a valid pass.
    """
    key_id, key = keys.parse(pass_key)
    stored = await db.fetchval("SELECT key_hash FROM api_keys WHERE id = $1", key_id)
    if stored is None or not hmac.compare_digest(bytes(stored), _hash(key)):
        raise keys.ApiKeyError("Unknown or replaced Unfret pass.")
    return key_id, bytes(stored)


async def issue_code(
    db: Executor,
    *,
    found: Client,
    key_id: str,
    key_hash: bytes,
    redirect_uri: str,
    code_challenge: str,
    config: Settings = settings,
) -> str:
    """
    An authorization code for a pass, after the person approved.

    :param db: A pool or connection.
    :param found: The client.
    :param key_id: The pass's id.
    :param key_hash: The pass's current key hash (the grant stops working when the pass is replaced).
    :param redirect_uri: The redirect URI it goes to (must come back with it).
    :param code_challenge: The PKCE S256 challenge.
    :param config: The settings.
    :return: The code (stored only as a hash).
    """
    code = secrets.token_urlsafe(32)
    await db.execute(
        "INSERT INTO oauth_codes (code_hash, client_id, key_id, key_hash, redirect_uri, code_challenge, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, NOW() + make_interval(secs => $7))",
        _hash(code),
        found.client_id,
        key_id,
        key_hash,
        redirect_uri,
        code_challenge,
        float(config.mcp_oauth_code_ttl_s),
    )
    return code


@dataclass(frozen=True)
class Tokens:
    """A token response, handed over once."""

    access_token: str
    refresh_token: str
    expires_in: int

    def as_response(self) -> dict[str, object]:
        """
        The RFC 6749 token response.

        :return: The JSON body.
        """
        return {
            "access_token": self.access_token,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": SCOPE,
        }


def _new_tokens(config: Settings) -> Tokens:
    return Tokens(
        ACCESS_PREFIX + secrets.token_urlsafe(32),
        REFRESH_PREFIX + secrets.token_urlsafe(32),
        config.mcp_oauth_access_ttl_s,
    )


async def exchange_code(
    pool: asyncpg.Pool,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    config: Settings = settings,
) -> Tokens:
    """
    The ``authorization_code`` grant: a code, its PKCE verifier and the same client and redirect URI, for tokens.

    :param pool: The database pool.
    :param code: The code.
    :param client_id: The client.
    :param redirect_uri: The redirect URI the code was sent to.
    :param code_verifier: The PKCE verifier.
    :param config: The settings.
    :return: The tokens.
    :raises OAuthError: ``invalid_grant`` for an unknown, expired, reused or mismatched code or a wrong verifier.
    """
    refused = "The code is invalid, expired or already used, or doesn't match."
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT client_id, key_id, key_hash, redirect_uri, code_challenge, expires_at > NOW() AS live, used, "
            "grant_id FROM oauth_codes WHERE code_hash = $1 FOR UPDATE",
            _hash(code),
        )
        if row is None:
            raise OAuthError("invalid_grant", refused)
        if row["used"]:
            # A code used twice: revoke what its first use issued (RFC 6749 §4.1.2); committed, then refused.
            if row["grant_id"] is not None:
                await conn.execute("DELETE FROM oauth_grants WHERE id = $1", row["grant_id"])
        else:
            await conn.execute("UPDATE oauth_codes SET used = TRUE WHERE code_hash = $1", _hash(code))
            current = await conn.fetchval("SELECT key_hash FROM api_keys WHERE id = $1", row["key_id"])
            matches = (
                row["live"]
                and row["client_id"] == client_id
                and row["redirect_uri"] == redirect_uri
                and pkce_ok(code_verifier, row["code_challenge"])
                and current is not None
                and bytes(current) == bytes(row["key_hash"])
            )
            if matches:
                tokens = _new_tokens(config)
                grant_id = await conn.fetchval(
                    "INSERT INTO oauth_grants (client_id, key_id, key_hash, access_hash, access_expires_at, "
                    "refresh_hash, refresh_expires_at) VALUES ($1, $2, $3, $4, NOW() + make_interval(secs => $5), $6, "
                    "NOW() + make_interval(secs => $7)) RETURNING id",
                    client_id,
                    row["key_id"],
                    bytes(row["key_hash"]),
                    _hash(tokens.access_token),
                    float(config.mcp_oauth_access_ttl_s),
                    _hash(tokens.refresh_token),
                    float(config.mcp_oauth_refresh_ttl_s),
                )
                await conn.execute("UPDATE oauth_codes SET grant_id = $2 WHERE code_hash = $1", _hash(code), grant_id)
                return tokens
    # Refused, but the code is spent: it was marked used in the committed transaction.
    raise OAuthError("invalid_grant", refused)


async def refresh(pool: asyncpg.Pool, *, refresh_token: str, client_id: str, config: Settings = settings) -> Tokens:
    """
    The ``refresh_token`` grant: new tokens, the refresh token rotated (the old one stops working).

    :param pool: The database pool.
    :param refresh_token: The refresh token.
    :param client_id: The client it was issued to.
    :param config: The settings.
    :return: The new tokens.
    :raises OAuthError: ``invalid_grant`` for an unknown, expired or rotated-away token, another client's token, or a
        pass that was replaced or retired.
    """
    hashed = _hash(refresh_token)
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT g.id, g.client_id, g.refresh_expires_at > NOW() AS live, k.key_hash = g.key_hash AS same_pass "
            "FROM oauth_grants g LEFT JOIN api_keys k ON k.id = g.key_id WHERE g.refresh_hash = $1 FOR UPDATE OF g",
            hashed,
        )
        if row is None:
            # A rotated-away token used again: someone else may hold it. Revoke (committed, then refused), unless it
            # is the client's own concurrent refresh right after a rotation.
            await conn.execute(
                "DELETE FROM oauth_grants WHERE prev_refresh_hash = $1 "
                "AND rotated_at < NOW() - make_interval(secs => $2)",
                hashed,
                float(REFRESH_GRACE_S),
            )
        elif row["client_id"] != client_id:
            pass  # another client's token: refused, and left alone
        elif not row["live"] or not row["same_pass"]:
            await conn.execute("DELETE FROM oauth_grants WHERE id = $1", row["id"])
        else:
            tokens = _new_tokens(config)
            await conn.execute(
                "UPDATE oauth_grants SET access_hash = $2, access_expires_at = NOW() + make_interval(secs => $3), "
                "prev_refresh_hash = refresh_hash, refresh_hash = $4, "
                "refresh_expires_at = NOW() + make_interval(secs => $5), rotated_at = NOW() WHERE id = $1",
                row["id"],
                _hash(tokens.access_token),
                float(config.mcp_oauth_access_ttl_s),
                _hash(tokens.refresh_token),
                float(config.mcp_oauth_refresh_ttl_s),
            )
            return tokens
    raise OAuthError("invalid_grant", "The refresh token is invalid, expired or revoked; sign in again.")


async def revoke(db: Executor, token: str, client_id: str | None = None) -> None:
    """
    Revoke the grant behind an access or refresh token (RFC 7009): both tokens stop working.

    An unknown token is not an error (RFC 7009 §2.2).

    :param db: A pool or connection.
    :param token: The token.
    :param client_id: The client, if it said; another client's token is left alone.
    """
    hashed = _hash(token)
    await db.execute(
        "DELETE FROM oauth_grants WHERE (access_hash = $1 OR refresh_hash = $1) "
        "AND ($2::text IS NULL OR client_id = $2)",
        hashed,
        client_id,
    )


async def authenticate(db: Executor, access_token: str) -> str:
    """
    The pass behind an access token, at ``/mcp``.

    :param db: A pool or connection.
    :param access_token: The token (``ufat_…``).
    :return: The pass's ledger account (``key:<id>``).
    :raises keys.ApiKeyError: For an unknown, expired or revoked token, or one whose pass was replaced or retired.
    """
    if not access_token.startswith(ACCESS_PREFIX) or len(access_token) > 100:
        raise keys.ApiKeyError("Not an access token.")
    key_id = await db.fetchval(
        "SELECT g.key_id FROM oauth_grants g JOIN api_keys k ON k.id = g.key_id AND k.key_hash = g.key_hash "
        "WHERE g.access_hash = $1 AND g.access_expires_at > NOW()",
        _hash(access_token),
    )
    if key_id is None:
        raise keys.ApiKeyError("Unknown, expired or revoked access token.")
    return keys.account_of(key_id)


async def purge(pool: asyncpg.Pool, config: Settings = settings) -> None:
    """
    Housekeeping: expired codes and grants, and registered clients unused for ``mcp_oauth_client_idle_days``.

    :param pool: The database pool.
    :param config: The settings.
    """
    await pool.execute("DELETE FROM oauth_codes WHERE expires_at < NOW() - INTERVAL '1 day'")
    await pool.execute("DELETE FROM oauth_grants WHERE refresh_expires_at < NOW()")
    await pool.execute(
        "DELETE FROM oauth_clients c WHERE created_on < CURRENT_DATE - $1::int "
        "AND NOT EXISTS (SELECT 1 FROM oauth_grants g WHERE g.client_id = c.client_id) "
        "AND NOT EXISTS (SELECT 1 FROM oauth_codes o WHERE o.client_id = c.client_id)",
        config.mcp_oauth_client_idle_days,
    )
