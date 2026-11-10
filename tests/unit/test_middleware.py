from collections.abc import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from unfret.middleware import _BODY_HEADROOM_BYTES, RequestGuardMiddleware

LIMIT = 1_000


async def echo_length(request: Request) -> JSONResponse:
    return JSONResponse({"received": len(await request.body())})


def client() -> httpx.AsyncClient:
    app = RequestGuardMiddleware(Starlette(routes=[Route("/", echo_length, methods=["POST"])]), max_body_bytes=LIMIT)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_accepts_small_body_and_times_it() -> None:
    async with client() as c:
        response = await c.post("/", content=b"x" * 10)
    assert response.status_code == 200
    assert response.json() == {"received": 10}
    assert float(response.headers["x-process-time"]) >= 0


async def test_rejects_oversized_content_length() -> None:
    async with client() as c:
        response = await c.post("/", content=b"x" * (LIMIT + _BODY_HEADROOM_BYTES + 1))
    assert response.status_code == 413
    assert response.json()["error_code"] == "PAYLOAD_TOO_LARGE"


async def test_rejects_oversized_streamed_body() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b"x" * 1_000

    async with client() as c:
        response = await c.post("/", content=chunks())  # chunked: no Content-Length to check up front
    assert response.status_code == 413
