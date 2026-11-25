"""
``POST /v1/reports``: the older "Wrong result?", kept for the extension and app versions in the stores.

The same report as ``POST /v1/labels`` (:mod:`unfret.reports`): its ``report_token`` (from a result with an account)
proves the check, its ``reason`` becomes the level (real_brand 0, scam 4; junk and other say "something wrong"
without one), and it keeps the page it is about, scrubbed, never who sent it. It is anonymous: the account it was
sent with is only used to check the token.
"""

from typing import NoReturn

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from unfret import crowd, reports
from unfret.auth import require_account
from unfret.config import settings
from unfret.reports import second_opinion
from unfret.schemas.errors import (
    BadRequestError,
    ConflictError,
    CustomAPIError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from unfret.schemas.requests import AnalyzeRequest
from unfret.shopper import Verdict

router = APIRouter(prefix="/v1/reports", tags=["Reports"])


class ReportRequest(BaseModel):
    """A wrong result, sent back with the check it came from (the older form of ``POST /v1/labels``)."""

    report_token: str = Field(max_length=600, description="The report_token from the analysis result.")
    reason: reports.Reason = Field(
        description="What the page really is: real_brand (a genuine product or shop: level 0), scam (level 4), junk "
        "(dropship or low-quality), other."
    )
    consent: bool | None = Field(
        default=None, description="Ignored (kept for older clients): a report always keeps the page it is about."
    )
    request: AnalyzeRequest = Field(description="The analysed request, exactly as it was sent.")
    explanation: str = Field(max_length=4000, description="The result's explanation, exactly as it was returned.")
    verdict: Verdict | None = Field(default=None, description="The result's verdict, as returned.")


class ReportResponse(BaseModel):
    """What happened to the report."""

    status: reports.Outcome = Field(
        description="queued (in the review queue), already_reported (someone reported this page first: your report "
        "counts as confirmation), already_known (reviewed recently)."
    )
    reports_used: int = Field(default=0, description="Always 0: reports are no longer counted per account.")
    reports_per_month: int = Field(description="Kept for older clients; reports are limited per result now.")


class QueueFullError(CustomAPIError):
    """The review queue is full."""

    status_code = 503
    error_code = "QUEUE_FULL"


_ERRORS: dict[str, type[CustomAPIError]] = {
    "BAD_REPORT_TOKEN": BadRequestError,
    "BAD_LABEL": BadRequestError,
    "ALREADY_REPORTED_BY_YOU": ConflictError,
    "LABEL_LIMIT": TooManyRequestsError,
    "LABELS_PAUSED": ServiceUnavailableError,
    "QUEUE_FULL": QueueFullError,
}


def _refused(exc: reports.ReportError | crowd.LabelError) -> NoReturn:
    # 400 for a token that doesn't match, 409 for a second report of the same result, 503 when full or paused.
    code = "ALREADY_REPORTED_BY_YOU" if exc.code == "ALREADY_LABELLED" else exc.code
    raise _ERRORS[code](exc.detail, error_code=code) from exc


@router.post("", response_model=ReportResponse, status_code=201)
async def report(body: ReportRequest, request: Request, account: str = Depends(require_account)) -> ReportResponse:
    """
    Report that a result is wrong (the older form of ``POST /v1/labels``; new clients use that).

    Send back the ``report_token`` from the result with the request and explanation exactly as they were, so the
    report provably concerns that check (for a URL-only check, the page is fetched again now). The report keeps the
    page, scrubbed of personal details, never who sent it. A page already in the queue counts the report as
    confirmation; a page reviewed recently is ``already_known``. One report per result.

    :param body: The report.
    :param request: The incoming request.
    :param account: The account the result was for (only to check the token).
    :return: The outcome.
    :raises ServiceUnavailableError: If reports are off (no session secret).
    """
    secret = settings.session_secret.get_secret_value()
    pool = request.app.db_pool
    if not secret or pool is None:
        raise ServiceUnavailableError("Reports are not available yet.")
    try:
        claim = reports.check_token(
            secret, body.report_token, account=account, body=body.request, explanation=body.explanation
        )
        verdict = body.verdict.model_dump(mode="json") if body.verdict else None
        risk = (verdict or {}).get("risk") or {}
        shown = crowd.Shown(
            input_hash=reports.input_hash(body.request),
            tier="quick" if body.request.tier == "quick" else "deep",
            score=claim.score,
            level=None,  # the token doesn't vouch for a level
            types=tuple(str(b["type"]) for b in risk.get("because", [])),
            token_hash=crowd.token_hash(body.report_token),
        )
        done = await reports.submit(
            pool,
            settings,
            body=await reports.with_page(body.request),
            shown=shown,
            level=reports.REASON_LEVEL.get(body.reason),
            wrong=[],
            missing=[],
            reason=body.reason,
            explanation=body.explanation,
            verdict=verdict,
        )
    except (reports.ReportError, crowd.LabelError) as exc:
        _refused(exc)
    if done.report_id is not None:
        second_opinion.schedule(pool, settings, done.report_id)
    return ReportResponse(status=done.outcome, reports_per_month=settings.reports_per_month)
