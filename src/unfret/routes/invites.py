"""
Invites: ``POST /v1/invites/redeem``.

A pack comes with gift codes to give to friends (:mod:`unfret.credits.invites`); whoever gets one redeems it here
with their own account, a wallet's session or a pass, for ``invite_checks`` checks. Intro codes (named codes the
operator handed out, e.g. ``FRIENDS``) are retired.
"""

from datetime import UTC, datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from unfret.auth import require_account
from unfret.config import settings
from unfret.credits import invites
from unfret.schemas.errors import (
    BadRequestError,
    ConflictError,
    CustomAPIError,
    ForbiddenError,
    GoneError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from unfret.visitors import RateLimit

router = APIRouter(prefix="/v1/invites", tags=["Credits"])

# Redemptions per visitor network (in memory, hashed), whatever their outcome.
_attempts = RateLimit(settings.redeem_per_ip_per_10min, window_s=600)


class RedeemRequest(BaseModel):
    """An invite code."""

    code: str = Field(max_length=100, description="The gift code (ufi_…), as given.")


class RedeemResponse(BaseModel):
    """What the invite added."""

    checks_added: int
    usd_micros_added: int
    balance_usd_micros: int
    checks_remaining: int


def _refuse(exc: invites.InviteError) -> NoReturn:
    error: CustomAPIError
    if exc.kind == "invalid":
        error = BadRequestError(str(exc), error_code="INVALID_INVITE")
    elif exc.kind == "unknown":
        error = NotFoundError(str(exc), error_code="UNKNOWN_INVITE")
    elif exc.kind == "used":
        error = ConflictError(str(exc), error_code="INVITE_USED")
    elif exc.kind == "ended":
        error = GoneError(str(exc), error_code="INVITE_ENDED")
    elif exc.kind == "own":
        error = ForbiddenError(str(exc), error_code="INVITE_OWN_PACK")
    elif exc.kind == "daily_cap":
        error = TooManyRequestsError(str(exc), error_code="INTRO_DAILY_CAP")
    elif exc.kind == "monthly":
        again = invites.next_month(datetime.now(UTC).date()).isoformat()
        error = TooManyRequestsError(str(exc), error_code="INVITE_MONTHLY_LIMIT", metadata={"next_month": again})
    else:
        error = ForbiddenError(str(exc), error_code="INVITE_NOT_FOR_THIS_ACCOUNT")
    raise error from exc


@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest, request: Request, account: str = Depends(require_account)) -> RedeemResponse:
    """
    Redeem an invite for the signed-in wallet or the API key: it adds ``invite_checks`` checks (``/v1/pricing``).

    The code is a gift code that came with a pack (``ufi_…``): any account may redeem it, new or in use, except the
    one whose purchase issued it; each code works once, and an account redeems one a calendar month (UTC). The account
    keeps only the fact that it redeemed one and the month of the latest (not which). Intro codes (``FRIENDS``) are
    retired: they answer 410 ``INVITE_ENDED``. Refusals: 400 ``INVALID_INVITE`` (not a code), 404 ``UNKNOWN_INVITE``,
    409 ``INVITE_USED``, 403 ``INVITE_OWN_PACK`` (it came with this account's own pack: give it to a friend), 429
    ``INVITE_MONTHLY_LIMIT`` (this account already redeemed a gift code this month; the code stays unused, and
    ``metadata.next_month`` is the day it can again), 410 ``INVITE_ENDED``; while intro codes are on again, also 403
    ``INVITE_NOT_FOR_THIS_ACCOUNT`` (an intro code on an account that has had checks) and 429 ``INTRO_DAILY_CAP``;
    429 ``TOO_MANY_REQUESTS`` (too many codes tried from one visitor network in ten minutes).

    :param body: The code.
    :param request: The incoming request.
    :param account: The redeeming account (injected by require_account).
    :return: What was added and the new balance.
    :raises ServiceUnavailableError: Without a database.
    :raises TooManyRequestsError: Over the redemptions allowed from this visitor network.
    """
    if (wait := _attempts.retry_after(request.client.host if request.client else "unknown")) is not None:
        raise TooManyRequestsError(
            "Too many codes tried from here; wait a few minutes.", metadata={"retry_after_s": max(1, int(wait) + 1)}
        )
    if request.app.db_pool is None:
        raise ServiceUnavailableError("Invites are not available yet.")
    try:
        done = await invites.redeem(request.app.db_pool, settings, account, body.code)
    except invites.InviteError as exc:
        _refuse(exc)
    return RedeemResponse(
        checks_added=done.usd_micros // settings.analysis_price_usd_micros,
        usd_micros_added=done.usd_micros,
        balance_usd_micros=done.balance_usd_micros,
        checks_remaining=max(done.balance_usd_micros, 0) // settings.analysis_price_usd_micros,
    )
