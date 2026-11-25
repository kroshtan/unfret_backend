"""
The public live stats: ``/live``, its stream mode, ``GET /v1/stats/live``, and the review of nicknames.

``/live?stream=1`` is 1920×1080, with no navigation, for a 24/7 stream. The operator reviews leaderboard nicknames at
``/v1/admin/nicknames``.

The page is one file with its CSS and JS inline (``unfret/live.html``, not under ``static/``), allowed by their
hashes in the Content Security Policy: no other script or style, no request but to this API, no cookie, nothing from
another origin.
"""

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from unfret import live_stats, nicknames
from unfret.auth import require_admin
from unfret.config import settings
from unfret.schemas.errors import NotFoundError, ServiceUnavailableError

logger = structlog.get_logger("unfret")

router = APIRouter(include_in_schema=False)
admin = APIRouter(prefix="/v1/admin/nicknames", dependencies=[Depends(require_admin)], include_in_schema=False)

TEMPLATE = Path(__file__).resolve().parent.parent / "live.html"
CACHE = {"Cache-Control": "public, max-age=60"}
_INLINE_RE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)
_NAV_RE = re.compile(r"\s*<!-- nav -->.*?<!-- /nav -->", re.DOTALL)


def _data_block(value: Any) -> str:
    # JSON inside <script type="application/json">: nothing in it can close the element or start markup.
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def csp(page: str) -> str:
    """
    The page's Content Security Policy: its own inline script and style by hash, requests to this API only.

    :param page: The page's HTML template.
    :return: The policy.
    """
    hashes: dict[str, list[str]] = {"script": [], "style": []}
    for kind, body in _INLINE_RE.findall(page):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        hashes[kind].append(f"'sha256-{digest}'")
    return (
        f"default-src 'none'; script-src {' '.join(hashes['script'])}; style-src {' '.join(hashes['style'])}; "
        "connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


async def _stats(request: Request) -> dict[str, Any]:
    pool = request.app.db_pool
    if pool is None:
        raise ServiceUnavailableError("The live stats aren't available right now.")
    return await live_stats.cached(pool, settings)


@router.get("/v1/stats/live")
async def stats(request: Request) -> JSONResponse:
    """
    Unfret's live numbers: counts only (``unfret.live_stats``), at most a minute old.

    :param request: The incoming request.
    :return: The stats, ``Cache-Control: public, max-age=60``.
    """
    return JSONResponse(await _stats(request), headers=CACHE)


@router.get("/live", response_class=HTMLResponse)
async def live(request: Request, stream: int = 0) -> HTMLResponse:
    """
    The live stats page; ``?stream=1`` for the stream (1920×1080, dark, large type, no navigation).

    :param request: The incoming request.
    :param stream: ``1`` for the stream mode.
    :return: The page, with the numbers as of now (the page polls for new ones every minute).
    """
    page = _template()
    policy = csp(page)
    try:
        data: dict[str, Any] | None = await _stats(request)
    except Exception as exc:  # noqa: BLE001 - the page still loads and polls
        logger.warning("live_stats_failed", error=type(exc).__name__)
        data = None
    if stream == 1:
        page = _NAV_RE.sub("", page, count=1)
    page = page.replace("{{mode}}", "stream" if stream == 1 else "page", 1).replace("{{data}}", _data_block(data), 1)
    headers = {
        **CACHE,
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return HTMLResponse(page, headers=headers)


_DECISIONS: dict[str, nicknames.Review] = {"approve": "approved", "reject": "rejected", "pending": "pending"}


class NicknameReview(BaseModel):
    """A nickname's review."""

    handle: str
    review: nicknames.Review
    public: bool
    agreed: int


@admin.get("", response_model=list[NicknameReview])
async def list_nicknames(
    request: Request, review: Literal["pending", "approved", "rejected", "all"] = "pending"
) -> list[NicknameReview]:
    """
    Nicknames to review (``?review=pending``, the default), approved, rejected or all.

    :param request: The incoming request.
    :param review: Which.
    :return: Nickname, review, whether it opted in to the leaderboard, verified reports.
    """
    rows = await nicknames.listed(request.app.db_pool, None if review == "all" else review)
    return [NicknameReview.model_validate(r) for r in rows]


@admin.post("/{handle}/{decision}", response_model=dict[str, str])
async def review_nickname(
    request: Request, handle: str, decision: Literal["approve", "reject", "pending"]
) -> dict[str, str]:
    """
    Approve or reject a nickname for ``/live`` (or put it back to pending).

    :param request: The incoming request.
    :param handle: The nickname, exactly.
    :param decision: ``approve``, ``reject`` or ``pending``.
    :return: The nickname and its review now.
    :raises NotFoundError: Without a contributor of that nickname.
    """
    review = _DECISIONS[decision]
    if not await nicknames.review(request.app.db_pool, handle, review):
        raise NotFoundError("No contributor has that nickname.")
    live_stats.clear()
    logger.info("nickname_reviewed", review=review)
    return {"handle": handle, "review": review}
