"""
Shared results (:mod:`unfret.shares`): a shopper makes their own check public as a link, anonymously.

``POST /v1/shares/preview`` shows exactly what would be public, ``POST /v1/shares`` makes it so; both are sent
*without* ``Authorization`` (a request that carries one is refused), so nothing ties the share to an account.
``GET /v1/shares/{id}`` and the page ``/r/{id}`` show it, marked ``noindex``; ``DELETE /v1/shares/{id}`` with the
delete code takes it down, and ``POST /v1/shares/{id}/object`` (a shop owner) hides it at once for the operator to
review.
"""

from datetime import date, datetime
from typing import Any, Literal

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from unfret import shares
from unfret.config import settings
from unfret.routes.pages import PAGE_HEADERS
from unfret.routes.showcase import fill_page
from unfret.schemas.errors import (
    BadRequestError,
    CustomAPIError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from unfret.shopper import Verdict
from unfret.visitors import DailyLimit, RateLimit

router = APIRouter(prefix="/v1/shares", tags=["Shares"])
pages = APIRouter(include_in_schema=False)

NOTICE = "Anyone with the link sees this verdict and the product link. It never says who checked."
ROBOTS = "noindex, nofollow"
SHARE_HEADERS = {"X-Robots-Tag": ROBOTS, "Cache-Control": "no-cache", "Referrer-Policy": "no-referrer"}
_NOT_FOUND = "No such shared result (it may have been deleted, expired or taken down for review)."

# In memory, per visitor network (hashed): shares made, previews and objections.
_creations = DailyLimit(settings.shares_per_visitor_per_day, settings.shares_daily_cap)
_previews = RateLimit(30)
_objections = DailyLimit(settings.shares_per_visitor_per_day, settings.shares_daily_cap)


class SharedAnalysis(BaseModel):
    """The analysis result, as ``POST /v1/analyze`` returned it (only these fields are read; others are ignored)."""

    score: int = Field(ge=-1, le=100)
    explanation: str = Field(max_length=4000)
    assessment: dict[str, Any] | None = None
    verdict: Verdict | None = None


class ShareRequest(BaseModel):
    """A result to share, sent back with the check it came from."""

    share_token: str = Field(max_length=2000, description="The share_token from the analysis result.")
    url: str = Field(min_length=8, max_length=4000, description="The url that was checked, as it was sent.")
    result: SharedAnalysis = Field(description="The result, exactly as it was returned.")


class SharePreview(BaseModel):
    """Exactly what a share makes public."""

    title: str = Field(description="The product's title, as the page showed it (or the shop when it had none).")
    host: str = Field(description="The shop or marketplace.")
    product_url: str = Field(description="The product page (without tracking parameters), linked nofollow.")
    checked_on: date = Field(description="The day of the check; the page may have changed since.")
    checked_on_text: str = Field(description="The same day, in words.")
    band: Literal["reliable", "concerns", "careful", "warning"]
    label: str = Field(description="What Unfret would tell a shopper about this listing.")
    symbol: str
    headline: str
    result: dict[str, Any] = Field(
        description="verdict (band, label, symbol, headline, reasons, positives) and, with flags, assessment "
        "(flags with labels only, confidence, model_version). No score, no quotes from the page."
    )
    notice: str = Field(default=NOTICE, description="What to tell the sharer before they confirm.")


class SharedResult(SharePreview):
    """A shared result."""

    id: str
    link: str = Field(description="The share's page on this site, /r/<id>.")
    expires_at: datetime = Field(description="When it is deleted.")


class CreatedShare(BaseModel):
    """A new share. The delete code is shown this once and stored only as a hash: keep it to delete the share."""

    id: str
    link: str = Field(description="The share's page on this site, /r/<id>: the link to pass on.")
    delete_code: str = Field(description="Send as X-Delete-Code to DELETE /v1/shares/<id>.")
    expires_at: datetime


class Objection(BaseModel):
    """A shop owner's objection to a shared result."""

    reason: str = Field(default="", max_length=shares.MAX_OBJECTION_CHARS, description="Why (optional).")


class ShareRefusedError(CustomAPIError):
    """A share that can't be made (``error_code`` says why)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "NOT_SHAREABLE"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _anonymous(request: Request) -> None:
    if "authorization" in request.headers:
        raise BadRequestError(
            "A share is anonymous: send it without an Authorization header.", error_code="AUTHORIZATION_NOT_ALLOWED"
        )


def _pool(request: Request) -> Any:
    if request.app.db_pool is None:
        raise ServiceUnavailableError("Sharing is not available right now.")
    return request.app.db_pool


def _verified(body: ShareRequest) -> dict[str, Any]:
    secret = settings.session_secret.get_secret_value()
    if not secret:
        raise ServiceUnavailableError("Sharing is not available right now.")
    response = body.result.model_dump(mode="json", exclude_none=True)
    try:
        claim = shares.check_token(
            secret, body.share_token, url=body.url, response=response, ttl_s=settings.share_token_ttl_s
        )
        return shares.preview(claim, response)
    except shares.ShareError as exc:
        error = BadRequestError if exc.code == "BAD_SHARE_TOKEN" else ShareRefusedError
        raise error(exc.detail, error_code=exc.code) from exc


@router.post("/preview", response_model=SharePreview)
async def preview_share(body: ShareRequest, request: Request) -> SharePreview:
    """
    Exactly what ``POST /v1/shares`` would make public, without storing anything. Send it without Authorization.

    :param body: The share token, the checked url and the result.
    :param request: The incoming request.
    :return: The public fields and the notice to show the sharer.
    :raises TooManyRequestsError: Over 30 previews a minute from one visitor network.
    """
    _anonymous(request)
    if _previews.retry_after(_ip(request)) is not None:
        raise TooManyRequestsError("Too many previews in a minute; wait a moment.")
    return SharePreview.model_validate(_verified(body))


@router.post("", response_model=CreatedShare, status_code=201)
async def create_share(body: ShareRequest, request: Request) -> CreatedShare:
    """
    Make a result public as a link: ``/r/<id>``, for 90 days. Send it without Authorization; it names no account.

    The token must match the url and the result exactly (a share can only show a verdict Unfret gave for that page)
    and be under 7 days old. What is stored is what the preview shows: the verdict in the showcase's wording (no
    score, never "scam" or "fraud"), the product's title and link, the shop and the day of the check; no quotes from
    the page and nothing about who checked. A check that included checkout_content can't be shared. 20 shares per
    visitor network per day.

    :param body: The share token, the checked url and the result.
    :param request: The incoming request.
    :return: The id, the link and the delete code (shown this once).
    :raises TooManyRequestsError: Over today's limit.
    """
    _anonymous(request)
    pool = _pool(request)
    view = _verified(body)
    if not _creations.allow(_ip(request)):
        raise TooManyRequestsError("Too many shared results from here today; try again tomorrow.")
    created = await shares.create(pool, view, days=settings.share_days)
    return CreatedShare(
        id=created.share_id,
        link=f"/r/{created.share_id}",
        delete_code=created.delete_code,
        expires_at=created.expires_at,
    )


async def _visible(request: Request, share_id: str) -> dict[str, Any]:
    pool = request.app.db_pool
    row = None if pool is None else await shares.visible(pool, share_id)
    if row is None:
        raise NotFoundError(_NOT_FOUND)
    return shares.public_view(row)


@router.get("/{share_id}", response_model=SharedResult)
async def get_share(share_id: str, request: Request) -> JSONResponse:
    """
    A shared result: what a shopper's check of a product page showed that day.

    :param share_id: The share.
    :param request: The incoming request.
    :return: The shared result (404 once deleted, expired or hidden after an objection).
    """
    view = SharedResult.model_validate(await _visible(request, share_id))
    return JSONResponse(view.model_dump(mode="json"), headers=SHARE_HEADERS)


@router.delete("/{share_id}", status_code=204)
async def delete_share(
    share_id: str,
    request: Request,
    x_delete_code: str | None = Header(default=None, max_length=100, description="The delete code."),
) -> Response:
    """
    Delete a shared result with its delete code (X-Delete-Code), for good.

    :param share_id: The share.
    :param request: The incoming request.
    :param x_delete_code: The delete code shown when it was shared.
    :return: 204.
    :raises BadRequestError: Without the code.
    :raises ForbiddenError: With a wrong code.
    :raises NotFoundError: For no such share.
    """
    if not x_delete_code:
        raise BadRequestError("Send the delete code as X-Delete-Code.", error_code="DELETE_CODE_REQUIRED")
    outcome = await shares.delete(_pool(request), share_id, x_delete_code)
    if outcome == "missing":
        raise NotFoundError(_NOT_FOUND)
    if outcome == "wrong_code":
        raise ForbiddenError("That isn't this shared result's delete code.", error_code="WRONG_DELETE_CODE")
    return Response(status_code=204)


@router.post("/{share_id}/object")
async def object_to_share(share_id: str, body: Objection, request: Request) -> dict[str, bool]:
    """
    Object to a shared result (a shop owner): it is hidden at once, and Unfret reviews it.

    :param share_id: The share.
    :param body: Why, optionally.
    :param request: The incoming request.
    :return: ``{"hidden": true}``.
    :raises NotFoundError: For no such (visible) share.
    :raises TooManyRequestsError: Over today's limit.
    """
    pool = _pool(request)
    if not _objections.allow(_ip(request)):
        raise TooManyRequestsError("Too many objections from here today; contact us instead (kroshtan.com).")
    if not await shares.hide(pool, share_id, body.reason):
        raise NotFoundError(_NOT_FOUND)
    return {"hidden": True}


def render_share_page(view: dict[str, Any]) -> str:
    """
    A shared result's page, in the link checker's presentation (the showcase's renderer).

    :param view: The share's :func:`~unfret.shares.public_view`.
    :return: The page.
    """
    fields = {
        "title": view["title"],
        "description": f"{view['label']}: {view['headline']}",
        "host": view["host"],
        "product_url": view["product_url"],
        "checked_on": view["checked_on_text"],
        "band": view["band"],
        "symbol": view["symbol"],
        "label": view["label"],
        "headline": view["headline"],
        "robots": ROBOTS,
    }
    return fill_page("shared.html", fields, view["result"])


@pages.get("/r/{share_id}", response_class=HTMLResponse)
async def share_page(share_id: str, request: Request) -> HTMLResponse:
    """
    A shared result's page (not indexed by search engines).

    :param share_id: The share.
    :param request: The incoming request.
    :return: The page.
    """
    view = await _visible(request, share_id)
    return HTMLResponse(render_share_page(view), headers=PAGE_HEADERS | SHARE_HEADERS)
