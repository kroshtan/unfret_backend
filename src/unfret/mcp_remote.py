"""
The remote MCP server: Unfret's tools over streamable HTTP at ``/mcp``, for hosted clients (Claude's connectors, …).

Hosted by the API itself rather than as a separate service: the tools are the API's own routes (``POST /v1/check``,
``GET /v1/credits``, ``GET /v1/pricing``), called in-process through the app, so a check from MCP is authenticated,
limited and charged by exactly the code that serves ``/v1/check`` with a pass, with no second deploy, no hop over the
network and no copy of the rules to keep in step. The MCP transport runs **stateless** with JSON responses: every
request stands on its own, so any process can answer it, a deploy drops nothing, and nothing is kept between calls.

**Authentication: an Unfret pass only** (``Authorization: Bearer ufk_…``), checked before the request reaches the
MCP layer; anything else is 401 with ``WWW-Authenticate: Bearer``. This server never pays from a wallet and has no
code that could: deep checks are paid from the pass, quick checks are free. (The stdio package in ``mcp/`` keeps its
wallet mode for agents that run it themselves.) Too many bad passes from one network are answered 429 before any
lookup.

The tools only ever call the three routes in :data:`ROUTES`, with nothing but the caller's pass: no route that buys,
tops up or pays (x402, wallets, card checkout) is reachable from here, and a payment header sent to ``/mcp`` is never
passed on. Every tool declares a title, a one-sentence description and explicit ``readOnlyHint`` /
``destructiveHint`` / ``idempotentHint`` / ``openWorldHint``. Nothing about the checked page or the caller is logged
here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
import structlog
from mcp.server.mcpserver import Context, Icon, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from unfret import __version__
from unfret.api import UnfretAPI
from unfret.config import settings
from unfret.credits import keys
from unfret.visitors import RateLimit

logger = structlog.get_logger("unfret")

PATH = "/mcp"
_INTERNAL = "http://unfret.internal"  # the in-process calls' base URL; nothing resolves it
# Every route a tool may call, and the only ones: a check (paid from the pass) and two reads. Nothing that pays.
ROUTES: frozenset[tuple[str, str]] = frozenset({("POST", "/v1/check"), ("GET", "/v1/credits"), ("GET", "/v1/pricing")})
_bad_passes = RateLimit(settings.x402_check_per_minute)  # failed pass checks per visitor network

server: MCPServer = MCPServer(
    "unfret",
    title="Unfret",
    description="Scam, dropshipping and dark-pattern signals for a store or product page, before an agent buys.",
    instructions=(
        "Check a store or product listing before buying from it. Act on `action`: allow = go ahead, warn = ask "
        "your human first, block = don't buy. check_store_quick (rules on the page itself) is free; "
        "check_store_deep (everything, including an AI read of the listing, its reviews and photos) uses one check "
        "from the Unfret pass this connector was set up with. get_checks_left says how many it has; get_pricing what "
        "checks cost. Flags are evidence, never a verdict that a shop is a scam."
    ),
    website_url="https://unfret.net",
    icons=[Icon(src="https://unfret.net/static/icons/icon.svg", mime_type="image/svg+xml")],
    version=__version__,
)

_api: UnfretAPI | None = None
_mcp_app: ASGIApp | None = None


def _readonly(title: str, *, open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=open_world
    )


def _caller(ctx: Context) -> tuple[str, tuple[str, int]]:
    """
    The pass and network of the MCP request a tool runs for.

    :param ctx: The tool's context.
    :return: The ``Authorization`` header (already checked) and the client address.
    :raises RuntimeError: Outside an HTTP request (the gate guarantees one).
    """
    request = ctx.request_context.request
    headers = getattr(request, "headers", None)
    if headers is None or not headers.get("authorization"):
        raise RuntimeError("no HTTP request")  # pragma: no cover - the gate refuses those
    client = getattr(request, "client", None)
    return headers["authorization"], (client.host, client.port) if client else ("unknown", 0)


async def _call(ctx: Context, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
    """
    Call one of the API's routes in-process, as the MCP request's caller (same pass, same network).

    :param ctx: The tool's context.
    :param method: ``GET`` or ``POST``.
    :param path: The route.
    :param body: The JSON body, if any.
    :return: The response.
    :raises RuntimeError: While the server isn't attached to the app.
    :raises ValueError: For a route outside :data:`ROUTES`.
    """
    if (method, path) not in ROUTES:
        raise ValueError(f"not a route the MCP server calls: {method} {path}")
    if _api is None:
        raise RuntimeError("the MCP server is not attached")  # pragma: no cover
    authorization, client = _caller(ctx)
    transport = httpx.ASGITransport(app=_api, client=client)
    timeout = httpx.Timeout(settings.analysis_timeout_s + 30)
    async with httpx.AsyncClient(transport=transport, base_url=_INTERNAL, timeout=timeout) as http:
        # Only the pass goes along: never a payment header, so /v1/check always charges the pass (never x402).
        return await http.request(method, path, json=body, headers={"Authorization": authorization})


def _error(response: httpx.Response) -> dict[str, Any]:
    """
    A refused call as a tool result: what happened and what to do next. Nothing was ever charged for these.

    :param response: The API's answer.
    :return: ``status``, ``ok``, ``error_code``, ``error`` and ``next_step``.
    """
    try:
        problem = response.json()
    except ValueError:
        problem = {}
    code = str(problem.get("error_code") or response.status_code)
    retry = (problem.get("metadata") or {}).get("retry_after_s") or response.headers.get("retry-after")
    base = settings.public_base_url.rstrip("/")
    status, step = "failed", "Nothing was charged; retry later."
    if code == "INSUFFICIENT_CREDITS":
        status = "no_checks_left"
        step = f"Nothing was charged. Use check_store_quick (free), or buy checks for this pass at {base}/checkout."
    elif code == "FREE_CHECKS_PAUSED":
        status = "unavailable"
        step = "Nothing was charged. Free deep checks are back after 00:00 UTC; check_store_quick works now."
    elif response.status_code == 429:
        status = "unavailable"
        step = f"Nothing was charged; retry in {retry} s." if retry else "Nothing was charged; retry shortly."
    elif code.startswith("PAGE_") or code == "INVALID_URL":
        status = "page_unreadable"
        step = "Nothing was charged. Send the page's HTML as page_content (marketplaces block server fetches)."
    elif response.status_code == 422:
        status = "invalid_request"
        step = "Nothing was charged; fix the arguments."
    return {"status": status, "ok": False, "error_code": code, "error": problem.get("detail", ""), "next_step": step}


_URL = Annotated[str, Field(description="The store or product page URL (https).")]
_POLICY = Annotated[
    Literal["strict", "balanced", "lenient"],
    Field(description="How cautious the returned action is; strict warns and blocks soonest."),
]
_PAGE = Annotated[
    str | None,
    Field(description="The page's HTML, if you have it. Needed for marketplaces such as Amazon, which block fetches."),
]


async def _check(ctx: Context, tier: str, url: str, policy: str, page_content: str | None) -> dict[str, Any]:
    body: dict[str, Any] = {"url": url, "tier": tier, "policy": policy}
    if page_content:
        body["page_content"] = page_content
    response = await _call(ctx, "POST", "/v1/check", body)
    if response.status_code != 200:
        return _error(response)
    verdict = response.json()
    pass_fields = {
        k: verdict.pop(k, None) for k in ("charged", "free_check", "free_checks_left", "balance_usd_micros")
    }
    return {"status": "ok", "ok": True, "verdict": verdict, **pass_fields}


# Two tools rather than one with a tier: the free one changes nothing, the deep one uses one of the pass's checks.
@server.tool(
    title="Quick check of a store (free)",
    description=(
        "Runs Unfret's free, rule-based check of a store or product page (fetched from url, or read from "
        "page_content) and returns a verdict with action (allow, warn or block), risk_score, the flags found and "
        "whether a deep check is worth it, without using any of the pass's checks."
    ),
    annotations=ToolAnnotations(
        title="Quick check of a store (free)",
        read_only_hint=True,  # it only reads a page; nothing on the pass or elsewhere changes
        destructive_hint=False,
        idempotent_hint=True,  # free: a repeat changes nothing either
        open_world_hint=True,  # it fetches and reads a third-party web page
    ),
)
async def check_store_quick(
    url: _URL, ctx: Context, policy: _POLICY = "balanced", page_content: _PAGE = None
) -> dict[str, Any]:
    """
    Quick, free check of a store or product page: rules on the page itself.

    Discount tricks, dropshipping apps, fake timers, missing legal information, …; no AI read, reviews or photos. Use
    it to screen; ``verdict.deep_check.suggested`` says whether check_store_deep is worth it for this page (and
    ``adds`` what it would add).
    Act on ``action``: allow = go ahead, warn = ask your human first, block = don't buy.

    :param url: The store or product page URL.
    :param ctx: The MCP request context.
    :param policy: strict, balanced or lenient.
    :param page_content: The page's HTML, if available.
    :return: ``status`` (``ok``, ``page_unreadable``, ``unavailable``, ``invalid_request`` or ``failed``), ``ok``, the
        ``verdict`` (action, risk_score, confidence, flags, band), or ``error`` and ``next_step``.
    """
    return await _check(ctx, "quick", url, policy, page_content)


@server.tool(
    title="Deep check of a store (uses a check)",
    description=(
        "Runs Unfret's full check of a store or product page (the quick check's rules plus an AI read of the listing, "
        "its reviews, photos, price history and domain age), using one check from the Unfret pass (free checks "
        "first), and returns the verdict with action (allow, warn or block), risk_score and flags, whether it was "
        "charged and the checks left on the pass."
    ),
    annotations=ToolAnnotations(
        title="Deep check of a store (uses a check)",
        # It only reads the page and changes nothing there; the one thing it uses up is a check the caller prepaid
        # for this, which its title and description say.
        read_only_hint=True,
        destructive_hint=False,  # nothing is deleted or overwritten
        idempotent_hint=False,  # a repeat can use another check (only the same pass's repeat within the hour is free)
        open_world_hint=True,  # it fetches and reads a third-party web page
    ),
)
async def check_store_deep(
    url: _URL, ctx: Context, policy: _POLICY = "balanced", page_content: _PAGE = None
) -> dict[str, Any]:
    """
    Full check of a store or product page before buying; uses one prepaid check from the Unfret pass.

    Everything in the quick check plus an AI read of the listing, its reviews, photos, price history and domain age
    (the pass's free checks are used first).
    Act on ``action``: allow = go ahead, warn = ask your human first, block = don't buy.

    :param url: The store or product page URL.
    :param ctx: The MCP request context.
    :param policy: strict, balanced or lenient.
    :param page_content: The page's HTML, if available.
    :return: ``status`` (``ok``, ``no_checks_left``, ``page_unreadable``, ``unavailable``, ``invalid_request`` or
        ``failed``), ``ok``, the ``verdict`` (action, risk_score, confidence, flags, band) with ``charged`` and the
        pass's ``free_checks_left`` and ``balance_usd_micros``, or ``error`` and ``next_step``.
    """
    return await _check(ctx, "deep", url, policy, page_content)


@server.tool(
    title="Checks left on the pass",
    description=(
        "Returns how many deep checks the Unfret pass has left (free and bought), its balance and whether free deep "
        "checks are paused today."
    ),
    annotations=_readonly("Checks left on the pass"),
)
async def get_checks_left(ctx: Context) -> dict[str, Any]:
    """
    How many deep checks the Unfret pass has left (free and bought), and whether free deep checks are paused today.

    Counted as ``/v1/check`` counts them: a developer pass's free checks (``api_free_checks_remaining`` in
    ``/v1/credits``, which work on the API only) are in both counts.

    :param ctx: The MCP request context.
    :return: ``status``, ``ok``, ``checks_remaining``, ``free_checks_remaining``, ``free_checks_paused``,
        ``balance_usd_micros`` and ``buy_more_at``, or ``error``.
    """
    response = await _call(ctx, "GET", "/v1/credits")
    if response.status_code != 200:
        return _error(response)
    credits = response.json()
    api_free = int(credits.get("api_free_checks_remaining") or 0)
    return {
        "status": "ok",
        "ok": True,
        "checks_remaining": int(credits.get("checks_remaining") or 0) + api_free,
        "free_checks_remaining": int(credits.get("free_checks_remaining") or 0) + api_free,
        **{k: credits.get(k) for k in ("free_checks_paused", "balance_usd_micros")},
        "buy_more_at": f"{settings.public_base_url.rstrip('/')}/checkout",
    }


@server.tool(
    title="Unfret prices",
    description=(
        "Returns Unfret's current prices: what a quick and a deep check cost, the check packs a pass can buy and the "
        "free checks."
    ),
    annotations=_readonly("Unfret prices"),
)
async def get_pricing(ctx: Context) -> dict[str, Any]:
    """
    Unfret's current prices: what a check costs, the packs a pass can buy, and the free checks.

    :param ctx: The MCP request context.
    :return: ``status``, ``ok`` and ``pricing`` (the ``GET /v1/pricing`` document), or ``error``.
    """
    response = await _call(ctx, "GET", "/v1/pricing")
    if response.status_code != 200:
        return _error(response)
    return {"status": "ok", "ok": True, "pricing": response.json()}


def _unauthorized(detail: str, *, error: str = "invalid_token") -> JSONResponse:
    challenge = f'Bearer realm="Unfret", error="{error}", error_description="{detail}"'
    if error == "invalid_request":
        challenge = 'Bearer realm="Unfret"'  # no credentials at all: no error code (RFC 6750 §3.1)
    return JSONResponse(
        {"detail": detail, "error_code": "INVALID_API_KEY" if error == "invalid_token" else "UNAUTHORIZED"},
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )


class _Endpoint:
    """``/mcp`` as an ASGI app (a class, so Starlette routes it as one rather than as a request handler)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Refuse anything without a valid Unfret pass, then hand the request to the MCP transport.

        :param scope: The ASGI scope.
        :param receive: The ASGI receive channel.
        :param send: The ASGI send channel.
        """
        response = await _gate(scope)
        if response is None and _mcp_app is None:  # outside the app's lifespan
            response = JSONResponse({"detail": "Not running.", "error_code": "SERVICE_UNAVAILABLE"}, 503)
        if response is not None:
            await response(scope, receive, send)
            return
        assert _mcp_app is not None  # noqa: S101 - checked just above
        await _mcp_app(scope, receive, send)


endpoint = _Endpoint()


async def _gate(scope: Scope) -> JSONResponse | None:
    """
    Check the request's pass.

    :param scope: The ASGI scope.
    :return: The refusal, or ``None`` to go on.
    """
    if scope["type"] != "http" or _api is None:
        return JSONResponse({"detail": "Not running.", "error_code": "SERVICE_UNAVAILABLE"}, 503)
    ip = scope["client"][0] if scope.get("client") else "unknown"
    if (wait := _bad_passes.waiting(ip)) is not None:
        return JSONResponse(
            {"detail": "Too many requests without a valid pass; slow down.", "error_code": "RATE_LIMITED"},
            status_code=429,
            headers={"Retry-After": str(max(1, int(wait) + 1))},
        )
    scheme, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        _bad_passes.retry_after(ip)
        return _unauthorized(
            "Set the connector up with an Unfret pass: Authorization: Bearer ufk_... "
            f"(a developer pass with free checks: {settings.public_base_url.rstrip('/')}/developers).",
            error="invalid_request",
        )
    pool = _api.db_pool
    if pool is None:
        return JSONResponse({"detail": "Unfret passes are not available.", "error_code": "SERVICE_UNAVAILABLE"}, 503)
    try:
        await keys.authenticate(pool, token)
    except keys.ApiKeyError:
        _bad_passes.retry_after(ip)
        return _unauthorized("Not a valid Unfret pass (unknown, mistyped or replaced by a backup code).")
    return None


@asynccontextmanager
async def running(api: UnfretAPI) -> AsyncIterator[None]:
    """
    Serve ``/mcp`` for the app's lifetime (a fresh transport each time: one can only run once).

    :param api: The app whose routes the tools call.
    :yield: While it serves.
    """
    global _api, _mcp_app
    mcp_app = server.streamable_http_app(
        streamable_http_path=PATH,
        stateless_http=True,
        json_response=True,
        # A pass in a header is the only credential (no cookies, nothing ambient), so DNS rebinding gains nothing;
        # the SDK's localhost-only host check would refuse every real host name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async with server.session_manager.run():
        _api, _mcp_app = api, mcp_app
        try:
            yield
        finally:
            _api, _mcp_app = None, None
