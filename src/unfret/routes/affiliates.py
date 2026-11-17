"""
Affiliates and bonus codes: checking a code, attributing a pass, an affiliate's stats, the operator's side.

Public: ``POST /v1/credits/codes/check`` (what a code gives, before the buyer pays), ``POST /v1/affiliates/attribute``
(a page that holds a pass and a stored ``?ref=`` code: the browser extension's pass on ``/welcome``) and ``GET
/v1/affiliates/stats`` (an affiliate's own numbers, with the secret token from their stats link). Admin, behind
``UNFRET_ADMIN_TOKEN`` (``make affiliates``, ``make bonus-codes``): affiliates, bonus codes, the monthly report,
payouts. See :mod:`unfret.credits.affiliates`.
"""

from datetime import date
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from unfret.auth import require_account, require_admin
from unfret.config import settings
from unfret.credits import affiliates
from unfret.schemas.errors import NotFoundError, ServiceUnavailableError, TooManyRequestsError
from unfret.visitors import DailyLimit

logger = structlog.get_logger("unfret")

router = APIRouter(tags=["Credits"])
admin = APIRouter(prefix="/v1/admin", tags=["Admin"], dependencies=[Depends(require_admin)], include_in_schema=False)

# Code look-ups and attributions per visitor network a day (in memory, salted hashes): enough for a buyer's typos,
# too few to try codes until one works.
LOOKUPS = DailyLimit(settings.code_checks_per_ip_per_day, 1_000_000)


class InvalidCodeError(NotFoundError):
    """The code can't be used: unknown, ended or used up (one answer for all, so it tells nothing about codes)."""

    error_code = "INVALID_CODE"


def _pool(request: Request) -> Any:
    if request.app.db_pool is None:
        raise ServiceUnavailableError("Codes are not available yet.")
    return request.app.db_pool


def _limit(request: Request) -> None:
    if not LOOKUPS.allow(request.client.host if request.client else "unknown"):
        raise TooManyRequestsError("Too many codes tried from here today; try again tomorrow.")


class CodeCheckRequest(BaseModel):
    """A code a buyer typed (or a link carried)."""

    code: str = Field(max_length=40, description="The bonus or affiliate code, as typed.")


class CodeCheckResponse(BaseModel):
    """What a code gives on a purchase."""

    code: str = Field(description="The code, as it will be applied (upper case).")
    label: str = Field(description='The offer in a few words, e.g. "+20% checks" or "+20% checks on your first pack".')
    bonus_pct: int | None = Field(description="Percent more checks with the pack, or null.")
    first_pack_only: bool = Field(
        description="The bonus is for a pass's first pack only (an affiliate's code), and only on a pass that hasn't "
        "bought before."
    )


@router.post("/v1/credits/codes/check", response_model=CodeCheckResponse)
async def check_code(body: CodeCheckRequest, request: Request) -> CodeCheckResponse:
    """
    What a bonus or affiliate code gives, so the checkout can show it before the buyer pays.

    Pass the code as ``code`` to ``/v1/credits/fiat-checkout`` or ``/v1/credits/purchase-tx`` to use it: one code per
    purchase. 404 ``INVALID_CODE`` for any code that can't be used (unknown, ended, used up: one answer for all);
    429 after too many tries from one network in a day.

    :param body: The code.
    :param request: The incoming request.
    :return: The offer.
    :raises InvalidCodeError: If the code can't be used.
    """
    pool = _pool(request)
    _limit(request)
    code = await affiliates.usable(pool, body.code)
    if code is None:
        raise InvalidCodeError(affiliates.NOT_VALID)
    offer = affiliates.offer(settings, code)
    return CodeCheckResponse(
        code=offer.code,
        label=offer.label(),
        bonus_pct=offer.bonus_pct,
        first_pack_only=offer.first_pack_only,
    )


class AttributeRequest(BaseModel):
    """The affiliate code an Unfret page kept from a ``?ref=`` link."""

    code: str = Field(max_length=40)


class AttributeResponse(BaseModel):
    """Whether the pass now counts for the code's affiliate."""

    status: Literal["attributed", "already", "not_eligible", "invalid"] = Field(
        description="attributed; already (it came through an affiliate before: the first one stays); not_eligible "
        "(a wallet, or a pass that has bought checks before); invalid (no such affiliate code, or it ended)."
    )


@router.post("/v1/affiliates/attribute", response_model=AttributeResponse)
async def attribute(
    body: AttributeRequest, request: Request, account: str = Depends(require_account)
) -> AttributeResponse:
    """
    Count the calling pass for the affiliate whose link brought its user (``unfret.net/?ref=CODE``).

    For a page or app that holds a pass made without the code: the browser extension makes its pass on install, and
    the welcome page relays the code the site kept. Only a pass that has never bought checks, and only once: the first
    affiliate stays. Nothing is stored but the pass, the affiliate and the time.

    :param body: The code.
    :param request: The incoming request.
    :param account: The pass (``Authorization: Bearer ufk_…``).
    :return: What happened.
    """
    pool = _pool(request)
    _limit(request)
    return AttributeResponse(status=await affiliates.attribute(pool, account, body.code))


class MoneyMonth(BaseModel):
    """Amounts in one currency's cents."""

    net: int = Field(description="Net revenue of purchases on your passes: after VAT and payment fees.")
    commission: int
    reversed: int = Field(description="Commission taken back after refunds and chargebacks.")
    paid: int


class StatsMonth(BaseModel):
    """One month of an affiliate's numbers."""

    month: str
    passes: int = Field(description="Passes that came through your link or code.")
    first_checks: int = Field(description="Of those, passes that made their first check this month.")
    purchases: int
    money: dict[str, MoneyMonth] = Field(description="Per currency (usd, eur).")


class Balance(BaseModel):
    """An affiliate's balance in one currency, in cents."""

    payable: int = Field(
        description="Commission past its 30-day hold, less what was paid; below zero after a refund "
        "that came after a payout (the next payouts make up for it)."
    )
    held: int = Field(description="Commission still in its 30-day hold (the refund window).")
    due: int = Field(description="What the next payout pays: the payable amount once it reaches the minimum.")
    carried_over: int = Field(description="What waits for a later payout (below the minimum, or below zero).")


class Rules(BaseModel):
    """How payouts work."""

    hold_days: int
    min_payout_cents: int


class StatsResponse(BaseModel):
    """An affiliate's own numbers: counts and amounts only, nothing about any buyer."""

    name: str
    active: bool
    codes: list[str]
    months: list[StatsMonth] = Field(description="The last 13 months with any activity, newest first.")
    balance: dict[str, Balance] = Field(description="Per currency (usd, eur): what is payable, held, due, carried.")
    rules: Rules


@router.get("/v1/affiliates/stats", response_model=StatsResponse)
async def stats(
    request: Request,
    response: Response,
    x_unfret_stats: str = Header(default="", max_length=100, description="The token from your stats link (ufa_…)."),
) -> StatsResponse:
    """
    An affiliate's numbers per month, for the stats page their secret link opens (``/affiliates/stats#ufa_…``).

    The token travels in the link's fragment (never sent to a server by a browser) and from the page in the
    ``X-Unfret-Stats`` header; the operator can replace or revoke it. 404 ``UNKNOWN_STATS_LINK`` otherwise.

    :param request: The incoming request.
    :param response: The response (never cached).
    :param x_unfret_stats: The token.
    :return: The numbers.
    :raises NotFoundError: For an unknown or revoked token.
    """
    response.headers["Cache-Control"] = "no-store"
    found = await affiliates.stats(_pool(request), settings, x_unfret_stats)
    if found is None:
        raise NotFoundError("This stats link isn't valid (any more).", error_code="UNKNOWN_STATS_LINK")
    return StatsResponse.model_validate(found)


# ── Operator ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _admin_pool(request: Request) -> Any:
    if request.app.db_pool is None:
        raise HTTPException(503, "Database not ready.")
    return request.app.db_pool


def links(code: str, token: str | None) -> dict[str, str]:
    """
    The links to hand an affiliate.

    :param code: Their code.
    :param token: Their stats token (``None``/empty: none).
    :return: ``{"ref": …, "stats": …}``.
    """
    base = settings.public_base_url.rstrip("/")
    out = {"ref": f"{base}/?ref={code}"}
    if token:
        out["stats"] = f"{base}/affiliates/stats#{token}"
    return out


class AffiliateCreate(BaseModel):
    """A new affiliate."""

    code: str = Field(min_length=3, max_length=24)
    name: str = Field(min_length=1, max_length=100)
    payout_note: str = Field(default="", max_length=500)


class AffiliateUpdate(BaseModel):
    """Switch an affiliate on or off, change the payout note, or replace or revoke the stats link."""

    active: bool | None = None
    payout_note: str | None = Field(default=None, max_length=500)
    stats: Literal["rotate", "revoke"] | None = None


class PaidRequest(BaseModel):
    """A payout made by hand."""

    note: str = Field(default="", max_length=500)


class BonusCodeCreate(BaseModel):
    """A new bonus code."""

    code: str = Field(min_length=3, max_length=24)
    bonus_pct: int = Field(ge=1, le=200)
    expires_on: date | None = None
    max_uses: int | None = Field(default=None, ge=1, le=1_000_000)
    affiliate: str | None = Field(default=None, max_length=24, description="One of the affiliate's codes.")


@admin.get("/affiliates")
async def affiliates_list(request: Request) -> dict[str, Any]:
    """
    Every affiliate with their codes, passes and what they are owed.

    :param request: The incoming request.
    :return: The affiliates.
    """
    return {"affiliates": await affiliates.list_affiliates(_admin_pool(request), settings)}


@admin.post("/affiliates", status_code=201)
async def affiliates_create(body: AffiliateCreate, request: Request) -> dict[str, Any]:
    """
    Make an affiliate and their code; the stats link is shown this once.

    :param body: The code, name and payout note.
    :param request: The incoming request.
    :return: The affiliate and their links.
    :raises HTTPException: 409 for a malformed or taken code.
    """
    try:
        made = await affiliates.create_affiliate(_admin_pool(request), body.code, body.name, body.payout_note)
    except affiliates.CodeError as exc:
        raise HTTPException(409, str(exc)) from exc
    logger.info("affiliate_created")
    return {"id": made.id, "code": made.code, "name": made.name, "links": links(made.code, made.stats_token)}


@admin.post("/affiliates/{code}")
async def affiliates_update(code: str, body: AffiliateUpdate, request: Request) -> dict[str, Any]:
    """
    Switch an affiliate on or off, change their payout note, or replace or revoke their stats link.

    :param code: One of their codes.
    :param body: What to change.
    :param request: The incoming request.
    :return: The affiliate's code and, after a rotation, the new links.
    :raises HTTPException: 404 for an unknown affiliate.
    """
    pool = _admin_pool(request)
    if not await affiliates.update_affiliate(pool, code, active=body.active, payout_note=body.payout_note):
        raise HTTPException(404, "No affiliate with that code.")
    out: dict[str, Any] = {"code": code.upper()}
    if body.stats:
        token = await affiliates.rotate_stats(pool, code, revoke=body.stats == "revoke")
        out["links"] = links(code.upper(), token)
    return out


@admin.delete("/affiliates/{code}")
async def affiliates_delete(code: str, request: Request) -> dict[str, Any]:
    """
    Remove an affiliate made by mistake, with their codes and attributions; refused once money is on the books.

    :param code: One of their codes.
    :param request: The incoming request.
    :return: The code removed.
    :raises HTTPException: 404 for an unknown affiliate, 409 when they have earnings or payouts (switch them off).
    """
    done = await affiliates.delete_affiliate(_admin_pool(request), code)
    if done is None:
        raise HTTPException(404, "No affiliate with that code.")
    if not done:
        raise HTTPException(409, "They have earnings or payouts: switch them off instead.")
    logger.info("affiliate_deleted")
    return {"code": code.upper(), "deleted": True}


@admin.get("/affiliates/report")
async def affiliates_report(request: Request, month: str | None = None) -> dict[str, Any]:
    """
    The monthly report: per affiliate and month, passes, first checks, purchases, net, commission, reversed, paid.

    :param request: The incoming request.
    :param month: Only this month (``YYYY-MM``).
    :return: The report.
    :raises HTTPException: 400 for a malformed month.
    """
    try:
        return await affiliates.report(_admin_pool(request), settings, month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@admin.post("/affiliates/{code}/paid")
async def affiliates_paid(code: str, body: PaidRequest, request: Request) -> dict[str, Any]:
    """
    Record a payout made by hand: what is due now, per currency.

    Past the 30-day hold, at least the minimum unless the affiliate was switched off, less refunds that came after
    earlier payouts.

    :param code: One of the affiliate's codes.
    :param body: A note (how it was paid).
    :param request: The incoming request.
    :return: What was recorded per currency (empty: nothing due).
    :raises HTTPException: 404 for an unknown affiliate.
    """
    paid = await affiliates.mark_paid(_admin_pool(request), settings, code, body.note)
    if paid is None:
        raise HTTPException(404, "No affiliate with that code.")
    logger.info("affiliate_paid", **{f"{c}_minor": v for c, v in paid.items()})
    return {"code": code.upper(), "paid": paid}


@admin.post("/affiliates/earnings/{earning_id}/reverse")
async def affiliates_reverse(earning_id: int, request: Request) -> dict[str, Any]:
    """
    Reverse an earning in full (a purchase refunded by hand, e.g. crypto).

    :param earning_id: The earning.
    :param request: The incoming request.
    :return: What was taken back.
    :raises HTTPException: 404 for an unknown earning.
    """
    taken = await affiliates.reverse_earning(_admin_pool(request), earning_id)
    if taken is None:
        raise HTTPException(404, "No such earning.")
    return {"earning_id": earning_id, "reversed_minor": taken}


@admin.get("/bonus-codes")
async def bonus_codes_list(request: Request) -> dict[str, Any]:
    """
    Every bonus code with its uses.

    :param request: The incoming request.
    :return: The codes.
    """
    return {"codes": await affiliates.list_codes(_admin_pool(request))}


@admin.post("/bonus-codes", status_code=201)
async def bonus_codes_create(body: BonusCodeCreate, request: Request) -> dict[str, Any]:
    """
    Make a bonus code: more checks with a pack, optionally with a last day, a number of uses and an affiliate.

    :param body: The code and what it gives.
    :param request: The incoming request.
    :return: The code and its label.
    :raises HTTPException: 409 for a malformed or taken code or an unknown affiliate.
    """
    try:
        made = await affiliates.create_code(
            _admin_pool(request),
            body.code,
            bonus_pct=body.bonus_pct,
            expires_on=body.expires_on,
            max_uses=body.max_uses,
            affiliate=body.affiliate,
        )
    except affiliates.CodeError as exc:
        raise HTTPException(409, str(exc)) from exc
    logger.info("bonus_code_created", bonus_pct=made.bonus_pct)
    return {**made.__dict__, "label": affiliates.offer(settings, made).label()}


@admin.post("/bonus-codes/{code}")
async def bonus_codes_update(code: str, body: AffiliateUpdate, request: Request) -> dict[str, Any]:
    """
    Switch a bonus code on or off.

    :param code: The code.
    :param body: ``active``.
    :param request: The incoming request.
    :return: The code.
    :raises HTTPException: 400 without ``active``, 404 for an unknown code.
    """
    if body.active is None:
        raise HTTPException(400, "Say active: true or false.")
    changed = await affiliates.set_code_active(_admin_pool(request), code, body.active)
    if changed is None:
        raise HTTPException(404, "No such code.")
    return changed.__dict__
