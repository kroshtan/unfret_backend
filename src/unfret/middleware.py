import json
from time import perf_counter

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# JSON escaping and the other request fields make the body somewhat larger than the page it carries.
_BODY_HEADROOM_BYTES = 64 * 1024


async def _reject(send: Send, max_bytes: int) -> None:
    body = json.dumps(
        {"detail": f"Request body exceeds {max_bytes} bytes.", "error_code": "PAYLOAD_TOO_LARGE"}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _GuardedExchange:
    """The ``receive``/``send`` pair of one HTTP request, counting body bytes and timing the response."""

    def __init__(self, receive: Receive, send: Send, max_bytes: int) -> None:
        self._receive = receive
        self._send = send
        self._max_bytes = max_bytes
        self._start = perf_counter()
        self._received = 0
        self._response_started = False
        self.rejected = False

    async def receive(self) -> Message:
        message = await self._receive()
        if message["type"] != "http.request" or self.rejected:
            return message
        self._received += len(message.get("body", b""))
        if self._received <= self._max_bytes:
            return message
        # Answer 413 ourselves and tell the app the client went away, so it stops reading. Whatever the app
        # then tries to send (typically a 500 for the disconnect) is dropped.
        self.rejected = True
        if not self._response_started:
            await _reject(self._send, self._max_bytes)
        return {"type": "http.disconnect"}

    async def send(self, message: Message) -> None:
        if self.rejected:
            return
        if message["type"] == "http.response.start":
            self._response_started = True
            elapsed = f"{perf_counter() - self._start:.3f}".encode()
            message = {**message, "headers": [*message.get("headers", []), (b"x-process-time", elapsed)]}
        await self._send(message)


class RequestGuardMiddleware:
    """
    Pure ASGI middleware: cap request body size and add an ``x-process-time`` response header.

    The cap is checked against ``Content-Length`` up front and again while a chunked body streams in, so
    an oversized page is refused before it is buffered and parsed.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        """
        Wrap ``app``.

        :param app: The ASGI application to protect.
        :param max_body_bytes: Largest accepted page; the limit adds headroom for the JSON envelope.
        """
        self.app = app
        self.max_bytes = max_body_bytes + _BODY_HEADROOM_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Handle one ASGI call.

        :param scope: The connection scope.
        :param receive: Receives request messages.
        :param send: Sends response messages.
        :raises Exception: Whatever the wrapped app raises, unless the body was already rejected with 413.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = dict(scope["headers"]).get(b"content-length", b"")
        if content_length.isdigit() and int(content_length) > self.max_bytes:
            await _reject(send, self.max_bytes)
            return

        exchange = _GuardedExchange(receive, send, self.max_bytes)
        try:
            await self.app(scope, exchange.receive, exchange.send)
        except Exception:
            if not exchange.rejected:
                raise
