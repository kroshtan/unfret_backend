"""
"Spotted recently": the published showcase cases (:mod:`veridex.showcase`), as JSON and as a page per case.

Only cases the owner published are served; drafts and removed cases are a 404 everywhere. The JSON is public and
the same for everyone, so it may be cached briefly.
"""

import html
import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from veridex import showcase
from veridex.routes.pages import PAGE_HEADERS, STATIC_DIR
from veridex.schemas.errors import NotFoundError

router = APIRouter(tags=["Showcase"])

CACHE_CONTROL = "public, max-age=300"
_MAX_LIMIT = 24


class ShowcaseSummary(BaseModel):
    """A published case, as listed under "Spotted recently"."""

    id: int
    title: str = Field(description="The product's title, as the page showed it.")
    host: str = Field(description="The shop or marketplace.")
    product_url: str = Field(description="The product page that was checked.")
    checked_at: datetime = Field(description="When Unfret checked it; the page may have changed since.")
    checked_on: str = Field(description="The day it was checked, in words.")
    published_at: datetime
    band: Literal["reliable", "concerns", "careful", "warning"] = Field(
        description="The verdict band (warning: the band with the most warning signs)."
    )
    label: str = Field(description="What Unfret would tell a shopper about this listing.")
    symbol: str = Field(description="The band's symbol, as in the extension and the link checker.")
    headline: str = Field(description="One sentence about what the page showed.")
    page_url: str = Field(description="The case's page on this site.")
    screenshot_url: str | None = Field(default=None, description="A screenshot of the page when it was checked.")


class ShowcaseCase(ShowcaseSummary):
    """A published case with its full result."""

    result: dict[str, Any] = Field(
        description="The result as the link checker shows it: verdict (band, label, symbol, headline, reasons, "
        "positives) and the details (assessment flags)."
    )


class ShowcaseList(BaseModel):
    """The published cases, newest first."""

    items: list[ShowcaseSummary]


def _cached(model: BaseModel) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"), headers={"Cache-Control": CACHE_CONTROL})


@router.get("/v1/showcase", response_model=ShowcaseList)
async def showcase_list(
    request: Request, limit: int = Query(default=6, ge=1, le=_MAX_LIMIT, description="How many cases.")
) -> JSONResponse:
    """
    Real checks of real product pages, run by Unfret itself and reviewed before publication, newest first.

    Never a user's check. Each case is what the page showed on the day it was checked.

    :param request: The incoming request.
    :param limit: How many cases (at most 24).
    :return: The cases.
    """
    pool = request.app.db_pool
    rows = [] if pool is None else await showcase.published(pool, limit)
    items = [ShowcaseSummary.model_validate(showcase.public_view(row)) for row in rows]
    return _cached(ShowcaseList(items=items))


async def _published(request: Request, case_id: int) -> dict[str, Any]:
    pool = request.app.db_pool
    row = None if pool is None else await showcase.published_case(pool, case_id)
    if row is None:
        raise NotFoundError("No such case.")
    return row


@router.get("/v1/showcase/{case_id}", response_model=ShowcaseCase)
async def showcase_case(case_id: int, request: Request) -> JSONResponse:
    """
    One published case with its full result.

    :param case_id: The case.
    :param request: The incoming request.
    :return: The case.
    """
    return _cached(ShowcaseCase.model_validate(showcase.public_view(await _published(request, case_id))))


def _data_block(value: Any) -> str:
    # JSON inside <script type="application/json">: nothing in it can close the element or start markup.
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render_case_page(view: dict[str, Any]) -> str:
    """
    The case page: its facts and verdict in HTML (readable without scripts), the full result as a data block.

    :param view: The case's :func:`~veridex.showcase.public_view`.
    :return: The page.
    """
    shot = ""
    if view["screenshot_url"]:
        shot = (
            f'<figure class="shot"><img src="{html.escape(view["screenshot_url"])}" alt="The product page as Unfret '
            f'saw it on {html.escape(view["checked_on"])}" loading="lazy"><figcaption class="muted small">The page '
            "when it was checked.</figcaption></figure>"
        )
    fields = {
        "title": view["title"],
        "description": f"{view['label']}: {view['headline']}",
        "host": view["host"],
        "product_url": view["product_url"],
        "checked_on": view["checked_on"],
        "band": view["band"],
        "symbol": view["symbol"],
        "label": view["label"],
        "headline": view["headline"],
    }
    page = (STATIC_DIR / "showcase.html").read_text(encoding="utf-8")
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", html.escape(str(value)))
    page = page.replace("{{screenshot}}", shot)
    return page.replace("{{data}}", _data_block(view["result"]))


@router.get("/showcase/{case_id}", response_class=HTMLResponse, include_in_schema=False)
async def case_page(case_id: int, request: Request) -> HTMLResponse:
    """
    A published case's page, in the link checker's presentation.

    :param case_id: The case.
    :param request: The incoming request.
    :return: The page.
    """
    return HTMLResponse(
        render_case_page(showcase.public_view(await _published(request, case_id))), headers=PAGE_HEADERS
    )


@router.get("/showcase/{case_id}/screenshot", include_in_schema=False)
async def case_screenshot(case_id: int, request: Request) -> Response:
    """
    A published case's screenshot.

    :param case_id: The case.
    :param request: The incoming request.
    :return: The image.
    :raises NotFoundError: For a case that isn't published or has no screenshot.
    """
    pool = request.app.db_pool
    found = None if pool is None else await showcase.screenshot(pool, case_id)
    if found is None:
        raise NotFoundError("No such screenshot.")
    data, kind = found
    return Response(
        data,
        media_type=kind,
        headers={
            "Cache-Control": CACHE_CONTROL,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
