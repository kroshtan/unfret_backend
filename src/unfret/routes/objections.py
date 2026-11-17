"""
``POST /v1/objections``: a shop owner or seller objects to a result, asks for a correction or for their data to go.

Sent by the ``/objection`` page, without sign-in, into the operator's queue (:mod:`unfret.objections`). What is stored
is what the sender wrote; the visitor network is only counted, in memory, to limit requests.
"""

from typing import Literal

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from unfret import objections
from unfret.config import settings
from unfret.schemas.errors import CustomAPIError, ServiceUnavailableError, TooManyRequestsError
from unfret.visitors import DailyLimit

router = APIRouter(prefix="/v1/objections", tags=["Shop owners"])

# In memory, per visitor network (hashed): requests sent.
_requests = DailyLimit(settings.objections_per_visitor_per_day, settings.objections_daily_cap)


class ObjectionRequest(BaseModel):
    """A shop owner's or seller's request."""

    kind: objections.Kind = Field(
        description="correction (a result about the site is wrong), removal (delete what Unfret keeps about the "
        "site or a contact detail), other."
    )
    site: str = Field(
        min_length=4,
        max_length=objections.MAX_SITE_CHARS,
        description="The shop, page or ad it is about: a link or a domain. Anything after ? or # is dropped.",
    )
    message: str = Field(
        default="",
        max_length=objections.MAX_MESSAGE_CHARS,
        description="What is wrong, or which contact details (e-mail, phone, address) to remove.",
    )
    reply_to: str = Field(
        default="",
        max_length=objections.MAX_REPLY_TO_CHARS,
        description="An e-mail address to answer at (optional; without one Unfret can't reply).",
    )


class ObjectionReceived(BaseModel):
    """The request is in the queue."""

    status: Literal["received"] = "received"
    answer_within_days: int = Field(description="The longest it takes to handle (data requests: one month).")


class ObjectionRefusedError(CustomAPIError):
    """A request that names no site or no valid reply address."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "BAD_OBJECTION"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("", response_model=ObjectionReceived, status_code=201)
async def send_objection(body: ObjectionRequest, request: Request) -> ObjectionReceived:
    """
    Object to a result, ask for a correction, or ask Unfret to remove what it keeps about a site or contact detail.

    No sign-in. Stored as sent (the site without anything after ``?`` or ``#``), with the day; no IP address or time.
    Deleted once handled, or after 90 days. 5 requests per visitor network per day.

    :param body: The request.
    :param request: The incoming request.
    :return: That it was received.
    :raises TooManyRequestsError: Over today's limit.
    :raises ServiceUnavailableError: Without a database, or with a full queue.
    :raises ObjectionRefusedError: When the site or the reply address isn't one.
    """
    pool = request.app.db_pool
    if pool is None:
        raise ServiceUnavailableError("This form isn't available right now; please e-mail hello@unfret.net.")
    try:
        # Checked first, so a typo doesn't use up the day's requests.
        objections.site_of(body.site)
        objections.reply_address(body.reply_to)
        if not _requests.allow(_ip(request)):
            raise TooManyRequestsError("Too many requests from here today; please e-mail hello@unfret.net instead.")
        await objections.submit(pool, kind=body.kind, site=body.site, message=body.message, reply_to=body.reply_to)
    except objections.ObjectionError as exc:
        if exc.code == "QUEUE_FULL":
            raise ServiceUnavailableError(exc.detail, error_code=exc.code) from exc
        raise ObjectionRefusedError(exc.detail, error_code=exc.code) from exc
    return ObjectionReceived(answer_within_days=30)
