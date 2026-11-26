"""
The public live stats: ``/live``, its stream mode, ``GET /v1/stats/live``, and the review of nicknames.

``/live?stream=1`` is 1920×1080, with no navigation, for a 24/7 stream, with a QR code to the support page drawn here
as inline SVG. ``?app=play`` is the Google Play app's variant (:func:`unfret.routes.pages.play_text`): no support
button, no word about donations, and a QR code to the home page. The operator reviews leaderboard nicknames at
``/v1/admin/nicknames``.

The page is one file with its CSS and JS inline (``unfret/live.html``, not under ``static/``), allowed by their
hashes in the Content Security Policy: no other script or style, no request but to this API, no font but the site's own
(Figtree, from ``/static/fonts``), no cookie, nothing from another origin.
"""

import base64
import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import segno
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from unfret import live_stats, nicknames
from unfret.auth import require_admin
from unfret.config import settings
from unfret.routes.pages import PLAY_APP, play_text, web_variant
from unfret.schemas.errors import NotFoundError, ServiceUnavailableError

logger = structlog.get_logger("unfret")

router = APIRouter(include_in_schema=False)
admin = APIRouter(prefix="/v1/admin/nicknames", dependencies=[Depends(require_admin)], include_in_schema=False)

TEMPLATE = Path(__file__).resolve().parent.parent / "live.html"
CACHE = {"Cache-Control": "public, max-age=60"}
_INLINE_RE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)
_NAV_RE = re.compile(r"\s*<!-- nav -->.*?<!-- /nav -->", re.DOTALL)
_STREAM_RE = re.compile(r"\s*<!-- stream -->.*?<!-- /stream -->", re.DOTALL)
_STREAM_MARK_RE = re.compile(r"\s*<!-- /?stream -->")


def _data_block(value: Any) -> str:
    # JSON inside <script type="application/json">: nothing in it can close the element or start markup.
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def qr_svg(text: str) -> str:
    """
    A QR code as inline SVG: dark modules on white with the quiet zone, one path, no namespace URL in it.

    :param text: What it encodes.
    :return: The ``<svg>`` element.
    """
    code = segno.make(text, error="m", micro=False)
    border = 4
    size = code.symbol_size(border=border)[0]
    path = "".join(
        f"M{x} {y}h1v1h-1z"
        for y, row in enumerate(code.matrix_iter(border=border))
        for x, dark in enumerate(row)
        if dark
    )
    return (
        f'<svg viewBox="0 0 {size} {size}" role="img" aria-label="QR code" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/><path d="{path}" fill="#0a1310"/></svg>'
    )


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
        "connect-src 'self'; font-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
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
async def live(request: Request, stream: int = 0, app: str | None = None) -> HTMLResponse:
    """
    The live stats page; ``?stream=1`` for the stream (1920×1080, dark, large type, no navigation).

    ``?app=play`` is the Google Play app's variant: nothing about donations or support (Google Play's payments policy).

    :param request: The incoming request.
    :param stream: ``1`` for the stream mode.
    :param app: ``play`` for the Google Play app's variant.
    :return: The page, with the numbers as of now (the page polls for new ones every minute).
    """
    play = app == PLAY_APP
    page = play_text(_template()) if play else web_variant(_template())
    try:
        data: dict[str, Any] | None = await _stats(request)
    except Exception as exc:  # noqa: BLE001 - the page still loads and polls
        logger.warning("live_stats_failed", error=type(exc).__name__)
        data = None
    base = settings.public_base_url.rstrip("/")
    if stream == 1:
        page = _STREAM_MARK_RE.sub("", _NAV_RE.sub("", page, count=1))
        path = "" if play else "/support"
        fields = {
            "{{qr}}": qr_svg(base + (path or "/")),
            "{{qr_caption}}": "Check a shop free" if play else "Keep checks free",
            "{{qr_host}}": html.escape(urlsplit(base).netloc),
            "{{qr_path}}": path,
        }
        for key, value in fields.items():
            page = page.replace(key, value, 1)
    else:
        page = _STREAM_RE.sub("", page)
    mode = ("stream" if stream == 1 else "page") + (" play" if play else "")
    page = page.replace("{{mode}}", mode, 1).replace("{{home}}", f"/?app={PLAY_APP}" if play else "/", 1)
    page = page.replace("{{data}}", _data_block(data), 1)
    policy = csp(page)
    headers = {
        **CACHE,
        "Content-Security-Policy": policy,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if play:
        headers["X-Robots-Tag"] = "noindex"
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
