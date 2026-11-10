"""
Invites: ``POST /v1/invites/redeem``.

A pack comes with invite codes to give away (:mod:`unfret.credits.invites`); whoever gets one redeems it here with
their own new account, a wallet's session or an API key, for ``invite_checks`` checks.
"""

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
    NotFoundError,
    ServiceUnavailableError,
)

router = APIRouter(prefix="/v1/invites", tags=["Credits"])


class RedeemRequest(BaseModel):
    """An invite code."""

    code: str = Field(max_length=100, description="The invite code (ufi_…), as given.")


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
    else:
        error = ForbiddenError(str(exc), error_code="INVITE_NOT_FOR_THIS_ACCOUNT")
    raise error from exc


@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest, request: Request, account: str = Depends(require_account)) -> RedeemResponse:
    """
    Redeem an invite for the signed-in wallet or the API key: it adds ``invite_checks`` checks (``/v1/pricing``).

    Only for an account that never had any checks: a wallet we've never credited (no purchase, refund or reward) or
    an API key that was never topped up, and only once per account. Each code works once. Nothing about the code's
    buyer is known here, and the account keeps only the fact that it redeemed one (not which). Refusals: 400
    ``INVALID_INVITE`` (not a code), 404 ``UNKNOWN_INVITE``, 409 ``INVITE_USED``, 403 ``INVITE_NOT_FOR_THIS_ACCOUNT``
    (the account has had checks, or already redeemed an invite).

    :param body: The code.
    :param request: The incoming request.
    :param account: The redeeming account (injected by require_account).
    :return: What was added and the new balance.
    :raises ServiceUnavailableError: Without a database.
    """
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
