"""
OAuth for the remote MCP server: discovery, client registration, the sign-in page, tokens and revocation.

``/.well-known/oauth-protected-resource[/mcp]`` (RFC 9728) and ``/.well-known/oauth-authorization-server``
(RFC 8414) say where to sign in; ``POST /oauth/register`` (RFC 7591) registers a public client; ``/oauth/authorize``
is the page where the person picks the pass (paste one, or make a new free pass with the same proof of work and
limits as ``POST /v1/keys``); ``POST /oauth/token`` exchanges a code (PKCE S256) or a refresh token;
``POST /oauth/revoke`` (RFC 7009) ends a grant. The rules and the storage are :mod:`unfret.oauth`. All of it answers
404 while ``mcp_oauth_enabled`` is off. Nothing here pays or buys: the only thing a sign-in hands over is the use of a
pass's checks through ``/mcp``.
"""

import html
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from unfret import oauth
from unfret.auth import get_credits
from unfret.config import settings
from unfret.credits import keys
from unfret.routes.keys import issue_pass
from unfret.routes.pages import PAGE_HEADERS, fill_page
from unfret.schemas.errors import CustomAPIError, NotFoundError
from unfret.visitors import DailyLimit, RateLimit

logger = structlog.get_logger("unfret")

router = APIRouter(include_in_schema=False)

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_MAX_FORM_BYTES = 8 * 1024
# The authorization request's parameters, carried through the page's forms unchanged.
_PARAMS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope")
_PARAMS_AND_RESOURCE = (*_PARAMS, "resource")

# Registrations come from the client's servers (Anthropic's, for Claude): generous per network, capped per day.
_registrations = RateLimit(settings.mcp_oauth_registrations_per_minute)
_registrations_today = DailyLimit(10**9, settings.mcp_oauth_registrations_daily_cap)
# Wrong passes pasted on the sign-in page, per network (the person's own).
_bad_passes = RateLimit(30)
# Refused code exchanges, per network. Token requests come from the client's servers (Anthropic's, for Claude), shared
# by all its users, so this is generous, and a refused refresh (a grant that ended) doesn't count at all.
_bad_codes = RateLimit(120)
# Client ID Metadata Documents fetched for the sign-in page, per network (cached ones aren't fetched again).
_document_fetches = RateLimit(20)


def _enabled() -> None:
    if not settings.mcp_oauth_enabled:
        raise NotFoundError("Not found.")


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status, headers=_NO_STORE)


async def _form(request: Request) -> dict[str, str]:
    """
    An ``application/x-www-form-urlencoded`` body; a parameter sent twice is refused (RFC 6749 §3.1).

    :param request: The incoming request.
    :return: The parameters.
    :raises oauth.OAuthError: For another content type, a body too large, or a repeated parameter.
    """
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        raise oauth.OAuthError("invalid_request", "Send the parameters as application/x-www-form-urlencoded.")
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise oauth.OAuthError("invalid_request", "Request too large.")
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=bool(body), max_num_fields=30)
    except (ValueError, UnicodeDecodeError) as exc:
        raise oauth.OAuthError("invalid_request", "Malformed form body.") from exc
    form: dict[str, str] = {}
    for name, value in pairs:
        if name in form:
            raise oauth.OAuthError("invalid_request", f"{name} sent twice.")
        form[name] = value
    return form


# ── Discovery ────────────────────────────────────────────────────────────────────────────────────────────────────


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource() -> JSONResponse:
    """
    RFC 9728 metadata for ``/mcp``: the resource and its authorization server (this site).

    :return: The document.
    """
    _enabled()
    return JSONResponse(oauth.protected_resource_metadata(), headers={"Cache-Control": "public, max-age=300"})


@router.get("/.well-known/oauth-authorization-server")
def authorization_server() -> JSONResponse:
    """
    RFC 8414 metadata of Unfret's authorization server.

    :return: The document.
    """
    _enabled()
    return JSONResponse(oauth.authorization_server_metadata(), headers={"Cache-Control": "public, max-age=300"})


# ── Registration ─────────────────────────────────────────────────────────────────────────────────────────────────


@router.post("/oauth/register")
async def register(request: Request) -> JSONResponse:
    """
    Dynamic Client Registration (RFC 7591): a public client whose redirect URIs are Claude's callbacks or loopback.

    :param request: The incoming request (a JSON body).
    :return: 201 with the client, or 400 with ``invalid_redirect_uri`` / ``invalid_client_metadata``.
    """
    _enabled()
    if request.app.db_pool is None:
        return _oauth_error("temporarily_unavailable", "Sign-in is not available right now.", 503)
    if (wait := _registrations.retry_after(_ip(request))) is not None or not _registrations_today.allow(_ip(request)):
        response = _oauth_error("temporarily_unavailable", "Too many registrations; retry shortly.", 429)
        response.headers["Retry-After"] = str(max(1, int(wait or 60) + 1))
        return response
    try:
        metadata = await request.json()
    except ValueError:
        return _oauth_error("invalid_client_metadata", "Send the client metadata as a JSON object.")
    if not isinstance(metadata, dict):
        return _oauth_error("invalid_client_metadata", "Send the client metadata as a JSON object.")
    try:
        registered = await oauth.register(request.app.db_pool, metadata)
    except oauth.OAuthError as exc:
        return _oauth_error(exc.error, exc.description)
    logger.info("oauth_client_registered")
    return JSONResponse(registered, status_code=201, headers=_NO_STORE)


# ── The sign-in page ─────────────────────────────────────────────────────────────────────────────────────────────


def _redirect(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    """
    Send the browser back to the client with a code or an error, the ``state`` and the issuer (RFC 9207).

    :param redirect_uri: The client's (already checked) redirect URI.
    :param params: ``code`` or ``error`` (and ``error_description``), and ``state`` when the client sent one.
    :return: A 303 to it.
    """
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query += [(k, v) for k, v in params.items() if v] + [("iss", oauth.issuer())]
    return RedirectResponse(urlunsplit(parts._replace(query=urlencode(query))), status_code=303, headers=_NO_STORE)


def _error_page(description: str, status: int = 400) -> HTMLResponse:
    """
    A request that can't be sent back to its client (unknown client, redirect URI not allowed): said on the page.

    :param description: What was wrong.
    :param status: The status.
    :return: The page.
    """
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow"><title>Can\'t connect · Unfret</title>'
        '<link rel="stylesheet" href="/static/site.css"></head><body><main class="mine">'
        "<h1>This sign-in link doesn't work</h1>"
        f"<p>{html.escape(description)}</p><p>Start connecting Unfret again from the app you came from.</p>"
        '<p><a href="/">Unfret</a></p></main></body></html>'
    )
    return HTMLResponse(body, status_code=status, headers={**PAGE_HEADERS, **_NO_STORE})


async def _checked_request(request: Request, params: dict[str, str]) -> tuple[oauth.Client, Response | None]:
    """
    Validate an authorization request: first what decides whether it may be sent back, then the rest.

    :param request: The incoming request.
    :param params: Its parameters.
    :return: The client, and a redirect with the error when the request is refused after that.
    :raises oauth.OAuthError: For an unknown client, a redirect URI that isn't allowed, or too many metadata documents
        fetched for this network (for the caller to show on a page: never redirected to).
    """
    client_id = params.get("client_id", "")
    if oauth.needs_fetch(client_id) and _document_fetches.retry_after(_ip(request)) is not None:
        raise oauth.OAuthError("temporarily_unavailable", "Too many sign-ins from here just now; wait a minute.")
    found = await oauth.client(request.app.db_pool, client_id)
    redirect_uri = params.get("redirect_uri", "")
    oauth.check_redirect(found, redirect_uri)
    state = params.get("state", "")
    if params.get("response_type") != "code":
        return found, _redirect(redirect_uri, {"error": "unsupported_response_type", "state": state})
    if not oauth.challenge_ok(params.get("code_challenge", ""), params.get("code_challenge_method", "")):
        description = "PKCE is required: code_challenge with code_challenge_method=S256."
        return found, _redirect(
            redirect_uri, {"error": "invalid_request", "error_description": description, "state": state}
        )
    try:
        oauth.check_resource(params.get("resource"))
    except oauth.OAuthError as exc:
        return found, _redirect(
            redirect_uri, {"error": exc.error, "error_description": exc.description, "state": state}
        )
    return found, None


def _hidden_fields(params: dict[str, str]) -> str:
    return "\n          ".join(
        f'<input type="hidden" name="{name}" value="{html.escape(params[name])}">'
        for name in _PARAMS_AND_RESOURCE
        if params.get(name)
    )


def _authorize_page(
    request: Request,
    found: oauth.Client,
    params: dict[str, str],
    error: str = "",
    status: int = 200,
    *,
    form_kind: str = "create",
) -> HTMLResponse:
    """
    The page: who asks, where it goes back to, Continue (a pass is made), or a pass the person already has.

    The free checks a new pass comes with are named only while this network would still get them (its first pass).

    :param request: The incoming request.
    :param found: The client.
    :param params: The (checked) authorization request.
    :param error: A message to show (a pass that didn't work, a limit reached).
    :param status: The status.
    :param form_kind: Which way the error came from (``pass`` keeps the paste form open).
    :return: The page; its ``form-action`` allows this site and the client's redirect origin only.
    """
    redirect_uri = params["redirect_uri"]
    loopback = ""
    if oauth.is_loopback(redirect_uri):
        loopback = (
            '<p class="small"><strong>This goes back to a program on your own computer</strong> (such as Claude '
            "Code). Continue only if you started connecting Unfret there just now.</p>"
        )
    page = fill_page(
        "oauth-authorize.html",
        {
            "asker": found.shown_as,
            "back_to": oauth.redirect_host(redirect_uri),
        },
        None,
        {
            "hidden": _hidden_fields(params),
            "loopback_note": loopback,
            "checks_line": _checks_line(request),
            "pass_open": " open" if form_kind == "pass" else "",
            "error": f'<p class="notice" role="alert">{html.escape(error)}</p>' if error else "",
        },
    )
    csp = PAGE_HEADERS["Content-Security-Policy"].replace(
        "form-action 'none'", f"form-action 'self' {oauth.redirect_origin(redirect_uri)}"
    )
    return HTMLResponse(
        page, status_code=status, headers={**PAGE_HEADERS, **_NO_STORE, "Content-Security-Policy": csp}
    )


def _checks_line(request: Request) -> str:
    door = get_credits(request)
    if settings.free_checks_per_pass > 0 and door.free_passes.available(_ip(request)):
        return f"; it includes {settings.free_checks_per_pass} free deep checks"
    return ""


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize(request: Request) -> Response:
    """
    The authorization endpoint: the page where the person approves and picks the pass.

    :param request: The incoming request (the authorization request in its query).
    :return: The page, an error page, or a redirect with an error.
    """
    _enabled()
    if request.app.db_pool is None:
        return _error_page("Sign-in is not available right now; try again later.", 503)
    params = {name: request.query_params.get(name, "") for name in _PARAMS_AND_RESOURCE}
    try:
        found, refused = await _checked_request(request, params)
    except oauth.OAuthError as exc:
        return _error_page(exc.description)
    return refused or _authorize_page(request, found, params)


async def _chosen_pass(
    request: Request, form: dict[str, str], found: oauth.Client, params: dict[str, str]
) -> tuple[str, bytes, keys.IssuedKey | None, int] | Response:
    """
    The pass the person picked: a new free one (a solved proof of work, ``POST /v1/keys``'s limits) or a pasted one.

    :param request: The incoming request.
    :param form: The page's form.
    :param found: The client.
    :param params: The authorization request.
    :return: The pass's id and key hash, the new pass (or ``None``) and its free checks; or the page again with what
        went wrong.
    """
    if form.get("action") == "create":
        try:
            new, given = await issue_pass(request, form.get("challenge", ""), form.get("solution", ""), free=True)
        except CustomAPIError as exc:
            return _authorize_page(request, found, params, f"{exc.detail} Or use a pass you already have.", 400)
        return new.key_id, keys.hash_of(new.key), new, given
    ip = _ip(request)
    if _bad_passes.waiting(ip) is not None:
        return _authorize_page(
            request, found, params, "Too many passes that didn't work; wait a minute.", 429, form_kind="pass"
        )
    try:
        key_id, key_hash = await oauth.pass_hash(request.app.db_pool, form.get("pass", ""))
    except keys.ApiKeyError:
        _bad_passes.retry_after(ip)
        message = "That isn't a valid Unfret pass (it may have been replaced with its backup code)."
        return _authorize_page(request, found, params, message, 400, form_kind="pass")
    return key_id, key_hash, None, 0


@router.post("/oauth/authorize", response_class=HTMLResponse)
async def approve(request: Request) -> Response:
    """
    The person approved: with a pasted pass, or by making a new free pass (a solved proof of work).

    :param request: The incoming request (the page's form).
    :return: A redirect to the client with a code; for a new pass, a page that shows it once and links on; or the
        page again with what went wrong.
    """
    _enabled()
    pool = request.app.db_pool
    if pool is None or not get_credits(request).sign_in_enabled:
        return _error_page("Sign-in is not available right now; try again later.", 503)
    try:
        form = await _form(request)
        params = {name: form.get(name, "") for name in _PARAMS_AND_RESOURCE}
        found, refused = await _checked_request(request, params)
    except oauth.OAuthError as exc:
        return _error_page(exc.description)
    if refused is not None:
        return refused
    chosen = await _chosen_pass(request, form, found, params)
    if isinstance(chosen, Response):
        return chosen
    key_id, key_hash, new, given = chosen
    code = await oauth.issue_code(
        pool,
        found=found,
        key_id=key_id,
        key_hash=key_hash,
        redirect_uri=params["redirect_uri"],
        code_challenge=params["code_challenge"],
    )
    logger.info("oauth_code_issued", new_pass=new is not None)
    back = _redirect(params["redirect_uri"], {"code": code, "state": params["state"]})
    if new is None:
        return back
    page = fill_page(
        "oauth-new-pass.html",
        {
            "key": new.key,
            "recovery_code": new.recovery_code,
            "checks_line": f" (your pass includes {given} free deep checks)" if given else "",
            "asker": found.shown_as,
            "back_to": oauth.redirect_host(params["redirect_uri"]),
            "continue_url": back.headers["location"],
        },
        None,
    )
    return HTMLResponse(page, headers={**PAGE_HEADERS, **_NO_STORE})


# ── Tokens ───────────────────────────────────────────────────────────────────────────────────────────────────────


async def _grant(pool: asyncpg.Pool, form: dict[str, str]) -> oauth.Tokens:
    """
    Run a token request's grant.

    :param pool: The database pool.
    :param form: The request's parameters.
    :return: The tokens.
    :raises oauth.OAuthError: For a request that is refused (RFC 6749 §5.2).
    """
    client_id = form.get("client_id", "")
    if not client_id:
        raise oauth.OAuthError("invalid_request", "client_id is required (public clients send it).")
    oauth.check_resource(form.get("resource"))
    grant = form.get("grant_type")
    if grant == "authorization_code":
        return await oauth.exchange_code(
            pool,
            code=form.get("code", ""),
            client_id=client_id,
            redirect_uri=form.get("redirect_uri", ""),
            code_verifier=form.get("code_verifier", ""),
        )
    if grant == "refresh_token":
        return await oauth.refresh(pool, refresh_token=form.get("refresh_token", ""), client_id=client_id)
    raise oauth.OAuthError("unsupported_grant_type", "authorization_code or refresh_token.")


@router.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    """
    The token endpoint (public clients, no secret): ``authorization_code`` with PKCE, or ``refresh_token``.

    :param request: The incoming request (form-encoded).
    :return: The tokens, or an RFC 6749 error.
    """
    _enabled()
    pool = request.app.db_pool
    if pool is None:
        return _oauth_error("temporarily_unavailable", "Sign-in is not available right now.", 503)
    ip = _ip(request)
    if (wait := _bad_codes.waiting(ip)) is not None:
        response = _oauth_error("temporarily_unavailable", "Too many failed requests; retry shortly.", 429)
        response.headers["Retry-After"] = str(max(1, int(wait) + 1))
        return response
    form: dict[str, str] = {}
    try:
        form = await _form(request)
        tokens = await _grant(pool, form)
    except oauth.OAuthError as exc:
        if exc.error == "invalid_grant" and form.get("grant_type") == "authorization_code":
            _bad_codes.retry_after(ip)
        return _oauth_error(exc.error, exc.description)
    logger.info("oauth_tokens_issued", grant=form["grant_type"])
    return JSONResponse(tokens.as_response(), headers=_NO_STORE)


@router.post("/oauth/revoke")
async def revoke(request: Request) -> Response:
    """
    Revoke a grant by its access or refresh token (RFC 7009): 200 whether or not it was known.

    :param request: The incoming request (form-encoded ``token``, optional ``token_type_hint`` and ``client_id``).
    :return: An empty 200, or 400 for a malformed request.
    """
    _enabled()
    pool = request.app.db_pool
    if pool is None:
        return _oauth_error("temporarily_unavailable", "Sign-in is not available right now.", 503)
    try:
        form = await _form(request)
    except oauth.OAuthError as exc:
        return _oauth_error(exc.error, exc.description)
    if not form.get("token"):
        return _oauth_error("invalid_request", "token is required.")
    await oauth.revoke(pool, form["token"], form.get("client_id") or None)
    logger.info("oauth_revoked")
    return Response(status_code=200, headers=_NO_STORE)
