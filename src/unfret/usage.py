"""
Anonymous usage events: what kind of thing happened and when, nothing about who did it or what they checked.

One row per event in ``usage_events``: a real timestamp, a ``kind`` (:data:`KINDS`), the ``route`` it came in by
(:data:`ROUTES`, or none) and ``attrs``, only the coarse enums :data:`KINDS` allows for that kind. Never a pass (its
id or a hash of it), a key, an address, a wallet, a user agent, an OAuth client, or the URL or domain of a checked
page: there is no field for any of them, and :func:`record` drops an event whose attributes aren't in its kind's
list. "Returning" (a check by a pass made more than a day before) is worked out from the pass's day of creation by
the writer and stored as a yes/no only; the pass itself never leaves the memory of this process.

Recording never slows or fails a request: :func:`record` puts the event on a bounded in-process queue and returns;
one background task (:func:`start`) writes the queue in batches. A full queue, a write that fails or recording
switched off (``UNFRET_USAGE_EVENTS_ENABLED=false``) drops the event; :data:`dropped` counts those, and the writer
logs them. The hourly housekeeping deletes events older than ``usage_events_retention_days`` (400).

Checks are recorded by :class:`UsageMiddleware` around ``POST /v1/check``, ``/v1/analyze`` and
``/v1/analyze/stream``: the routes fill in the tier, how it was paid and the pass (:func:`note`), and the middleware
records the outcome from the response.
"""

import asyncio
import contextlib
import contextvars
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from unfret.config import settings

logger = structlog.get_logger("unfret")

# How an event came in. Checks: the API (/v1/check), the remote MCP server (/mcp, whose tools call /v1/check in
# process), the browser extension, the website and the Android app (the last three call /v1/analyze).
ROUTES = frozenset({"api", "mcp-remote", "extension", "site", "app"})
_BOOL = frozenset({True, False})
TOOLS = frozenset({"check_store_quick", "check_store_deep", "get_checks_left", "get_pricing", "other"})
# Every kind of event, and for each the only attributes it may carry and the only values each may take.
KINDS: dict[str, dict[str, frozenset[Any]]] = {
    # A new pass: where it was made (the extension, the website, the Android app, the MCP sign-in page, the API).
    "pass_created": {
        "source": frozenset({"extension", "website", "android", "mcp-oauth", "api"}),
        "developer": _BOOL,
        "free_checks": _BOOL,
    },
    # A quick or deep check: free (a quick check, a free check), paid from a pass, or paid over x402 (on Solana or
    # Base), and how it ended. returning: a check by a pass made more than a day before (only with a pass).
    "check": {
        "tier": frozenset({"quick", "deep"}),
        "payment": frozenset({"free", "pass", "x402"}),
        "network": frozenset({"solana", "base"}),
        "outcome": frozenset({"ok", "refused", "unreadable", "error"}),
        "returning": _BOOL,
    },
    # Money in: a card order (Polar, its currency), checks bought for a pass on Solana, or an x402 check paid on
    # Solana or Base.
    "payment": {
        "method": frozenset({"card", "solana", "base"}),
        "purpose": frozenset({"pass", "x402"}),
        "provider": frozenset({"polar"}),
        "currency": frozenset({"usd", "eur", "usdc", "sol"}),
    },
    "mcp_tools_list": {},
    "mcp_tool_call": {"tool": TOOLS},
    # The MCP sign-in: the page shown, a new pass made on it, and a client connected (its first tokens).
    "oauth_authorize_shown": {},
    "oauth_pass_created": {},
    "oauth_connected": {},
}

_QUEUE_MAX = 10_000
_BATCH = 500

# Events not stored, by why: "full" (the queue), "write" (the insert failed), "invalid" (a kind, route or attribute
# outside the lists above: a bug, never stored).
dropped: Counter[str] = Counter()


def valid(kind: str, route: str | None, attrs: dict[str, Any]) -> bool:
    """
    Whether an event is one :data:`KINDS` and :data:`ROUTES` allow, attribute by attribute.

    :param kind: The event's kind.
    :param route: Its route, or ``None``.
    :param attrs: Its attributes.
    :return: ``True`` if every part is on the lists.
    """
    allowed = KINDS.get(kind)
    if allowed is None or (route is not None and route not in ROUTES):
        return False
    return all(
        name in allowed and type(value) in (str, bool) and value in allowed[name] for name, value in attrs.items()
    )


type _Event = tuple[datetime, str, str | None, dict[str, Any], str | None]


@dataclass
class _Writer:
    """The queue of events not yet written, and the task writing it (none until :func:`start`)."""

    queue: asyncio.Queue[_Event] | None = None
    task: asyncio.Task[None] | None = None


_writer = _Writer()


def record(kind: str, route: str | None = None, *, pass_account: str | None = None, **attrs: Any) -> None:
    """
    Queue one event (its attributes as keywords, ``None`` ones left out); returns at once and never raises.

    :param kind: One of :data:`KINDS`.
    :param route: One of :data:`ROUTES`, or ``None``.
    :param pass_account: The pass behind a check (``key:<id>``), only to work out ``returning``; never stored.
    """
    try:
        queue = _writer.queue
        if not settings.usage_events_enabled or queue is None:
            return
        attrs = {k: v for k, v in attrs.items() if v is not None}
        if not valid(kind, route, attrs):
            dropped["invalid"] += 1
            logger.warning("usage_event_invalid", kind=kind)
            return
        account = pass_account if pass_account and pass_account.startswith("key:") else None
        queue.put_nowait((datetime.now(UTC), kind, route, attrs, account))
    except asyncio.QueueFull:
        dropped["full"] += 1
    except Exception:  # noqa: BLE001 - recording must never break a request
        dropped["error"] += 1


async def _returning(pool: asyncpg.Pool, accounts: set[str]) -> dict[str, bool]:
    ids = [account.removeprefix("key:") for account in accounts]
    rows = await pool.fetch(
        "SELECT id, created_on < CURRENT_DATE - 1 AS old FROM api_keys WHERE id = ANY($1::text[])", ids
    )
    return {f"key:{row['id']}": row["old"] for row in rows}


async def _write(pool: asyncpg.Pool, batch: list[_Event]) -> None:
    accounts = {account for *_, account in batch if account is not None}
    old = await _returning(pool, accounts) if accounts else {}
    rows = []
    for at, kind, route, attrs, account in batch:
        stored = {**attrs, "returning": old[account]} if account is not None and account in old else attrs
        rows.append((at, kind, route, json.dumps(stored)))
    await pool.executemany("INSERT INTO usage_events (at, kind, route, attrs) VALUES ($1, $2, $3, $4::jsonb)", rows)
    await _count_checks(pool, batch)


async def _count_checks(pool: asyncpg.Pool, batch: list[_Event]) -> None:
    # Checks that answered, a number a UTC day and tier in usage_counters, kept for good (the /live page's totals;
    # the events themselves go after usage_events_retention_days).
    counts = Counter(
        (f"checks_{attrs['tier']}", at.astimezone(UTC).strftime("%Y-%m-%d"))
        for at, kind, _, attrs, _ in batch
        if kind == "check" and attrs.get("outcome") == "ok" and attrs.get("tier") in {"quick", "deep"}
    )
    if counts:
        await pool.executemany(
            "INSERT INTO usage_counters (name, period, count) VALUES ($1, $2, $3) ON CONFLICT (name, period) "
            "DO UPDATE SET count = usage_counters.count + EXCLUDED.count",
            [(name, period, n) for (name, period), n in counts.items()],
        )


async def _run(pool: asyncpg.Pool, queue: asyncio.Queue[_Event]) -> None:
    while True:
        batch = [await queue.get()]
        while len(batch) < _BATCH and not queue.empty():
            batch.append(queue.get_nowait())
        try:
            await _write(pool, batch)
        except Exception as exc:  # noqa: BLE001 - dropped and counted, never retried
            dropped["write"] += len(batch)
            total = sum(dropped.values())
            logger.warning("usage_events_dropped", events=len(batch), error=type(exc).__name__, total=total)
        finally:
            for _ in batch:
                queue.task_done()


def start(pool: asyncpg.Pool) -> None:
    """
    Start writing events to ``usage_events`` (the app's lifespan; tests).

    :param pool: The database pool.
    """
    _writer.queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    _writer.task = asyncio.create_task(_run(pool, _writer.queue))


async def flush(timeout_s: float = 5.0) -> None:
    """
    Wait until every queued event is written (or dropped), at most ``timeout_s``.

    :param timeout_s: The longest wait.
    """
    if _writer.queue is not None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout_s):
                await _writer.queue.join()


async def stop() -> None:
    """Write what is queued (a few seconds at most), then stop the writer."""
    await flush()
    task, _writer.queue, _writer.task = _writer.task, None, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def purge(pool: asyncpg.Pool, days: int) -> None:
    """
    Delete events and uptime probes older than ``days`` (housekeeping).

    :param pool: The database pool.
    :param days: How long they are kept.
    """
    await pool.execute("DELETE FROM usage_events WHERE at < NOW() - make_interval(days => $1)", days)
    await pool.execute("DELETE FROM uptime_probes WHERE at < NOW() - make_interval(days => $1)", days)


# --- Checks -------------------------------------------------------------------------------------------------------

CHECK_PATHS = frozenset({"/v1/check", "/v1/analyze", "/v1/analyze/stream"})
_INTERNAL_HOST = "unfret.internal"  # the remote MCP server's in-process calls (unfret.mcp_remote)
_EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://", "safari-web-extension://")
_UNREADABLE = re.compile(rb'"error_code"\s*:\s*"(PAGE_[A-Z_]+|INVALID_URL)"')


@dataclass
class CheckNote:
    """What the routes know about the check being answered, for its event."""

    route: str
    tier: str | None = None
    payment: str | None = None
    network: str | None = None
    account: str | None = field(default=None, repr=False)
    unreadable: bool = False
    skip: bool = False


_current: contextvars.ContextVar[CheckNote | None] = contextvars.ContextVar("usage_check", default=None)


def note(**values: Any) -> None:
    """Tell the current check's event some :class:`CheckNote` fields: tier, payment, network, account, and so on."""
    current = _current.get()
    if current is not None:
        for name, value in values.items():
            setattr(current, name, value)


def client_of(headers: Headers, host: str | None = None) -> str:
    """
    Which of Unfret's own clients sent a request, by its ``Origin`` and ``User-Agent``; used, never stored.

    :param headers: The request's headers.
    :param host: The host the request was sent to.
    :return: ``mcp-remote``, ``extension``, ``app``, ``site`` or ``api``.
    """
    if host == _INTERNAL_HOST:
        return "mcp-remote"
    origin = headers.get("origin", "")
    if origin.startswith(_EXTENSION_ORIGINS):
        return "extension"
    if headers.get("user-agent", "").startswith("Unfret-Android/"):
        return "app"
    if origin and origin.rstrip("/") == settings.public_base_url.rstrip("/"):
        return "site"
    return "api"


def _outcome(status: int, body: bytes, stream: bool, note_: CheckNote) -> str:
    if status == 200:
        if stream:
            if not body.startswith(b"event: result"):
                return "error"  # failed, or the client hung up before the result
            return "unreadable" if note_.unreadable else "ok"
        return "unreadable" if note_.unreadable else "ok"
    if status == 422 and _UNREADABLE.search(body):
        return "unreadable"
    return "error" if status >= 500 else "refused"


class UsageMiddleware:
    """Record one ``check`` event per request to the check routes, with its outcome; never changes the response."""

    def __init__(self, app: ASGIApp) -> None:
        """
        Wrap the app.

        :param app: The ASGI app.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Pass the request on, watching the response's status and (small) body for the outcome.

        :param scope: The ASGI scope.
        :param receive: The ASGI receive channel.
        :param send: The ASGI send channel.
        """
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in CHECK_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").split(":")[0]
        current = CheckNote(route="api" if scope["path"] == "/v1/check" else client_of(headers))
        if host == _INTERNAL_HOST:
            current.route = "mcp-remote"
        token = _current.set(current)
        status = 500
        seen = bytearray()
        stream = scope["path"].endswith("/stream")

        async def watching(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if stream:  # one event a chunk: only its name matters ("event: result")
                    head = chunk[:32]
                    if head.startswith((b"event: result", b"event: error")):
                        seen[:] = head
                elif len(seen) < 4096:  # a JSON answer: an error's code is near its start
                    seen.extend(chunk[: 4096 - len(seen)])
            await send(message)

        try:
            await self.app(scope, receive, watching)
        finally:
            _current.reset(token)
            _record_check(current, scope, headers, status, bytes(seen))


def _record_check(current: CheckNote, scope: Scope, headers: Headers, status: int, body: bytes) -> None:
    stream = scope["path"].endswith("/stream")
    try:
        if current.skip:
            return
        payment = current.payment
        if payment is None:
            if scope["path"] == "/v1/check" and headers.get("payment-signature"):
                payment = "x402"
            elif headers.get("authorization"):
                payment = "free" if current.tier == "quick" else "pass"
            else:
                payment = "free"
        record(
            "check",
            current.route,
            pass_account=current.account,
            tier=current.tier,
            payment=payment,
            network=current.network if payment == "x402" else None,
            outcome=_outcome(status, body, stream, current),
        )
    except Exception:  # noqa: BLE001 - never breaks the response
        dropped["error"] += 1
