"""``POST /v1/reports``: report a wrong result, sharing that one check's page data for review."""

from typing import Literal, NoReturn

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from veridex import reports
from veridex.auth import require_account
from veridex.config import settings
from veridex.schemas.errors import (
    BadRequestError,
    ConflictError,
    CustomAPIError,
    ServiceUnavailableError,
    TooManyRequestsError,
)
from veridex.schemas.requests import AnalyzeRequest
from veridex.shopper import Verdict

router = APIRouter(prefix="/v1/reports", tags=["Reports"])

CONSENT = (
    "I share this one check's page facts, page text and result with Unfret for review. Personal details are removed "
    "before it is stored; once reviewed, it is kept without any link to my account."
)


class ReportRequest(BaseModel):
    """A wrong result, sent back with the check it came from."""

    report_token: str = Field(max_length=600, description="The report_token from the analysis result.")
    reason: reports.Reason = Field(
        description="What the page really is: real_brand (a genuine product or shop), scam, junk (dropship or "
        "low-quality), other."
    )
    consent: Literal[True] = Field(description="Must be true: " + CONSENT)
    request: AnalyzeRequest = Field(description="The analysed request, exactly as it was sent.")
    explanation: str = Field(max_length=4000, description="The result's explanation, exactly as it was returned.")
    verdict: Verdict | None = Field(default=None, description="The result's verdict, as returned.")


class ReportResponse(BaseModel):
    """What happened to the report."""

    status: reports.Outcome = Field(
        description="queued (in the review queue), already_reported (someone reported this page first: your report "
        "counts as confirmation), already_known (reviewed recently)."
    )
    reports_used: int = Field(description="Reports this account made this month, this one included.")
    reports_per_month: int


class QueueFullError(CustomAPIError):
    """The review queue is full."""

    status_code = 503
    error_code = "QUEUE_FULL"


_ERRORS: dict[str, type[CustomAPIError]] = {
    "BAD_REPORT_TOKEN": BadRequestError,
    "REPORT_LIMIT": TooManyRequestsError,
    "ALREADY_REPORTED_BY_YOU": ConflictError,
    "QUEUE_FULL": QueueFullError,
}


def _refused(exc: reports.ReportError) -> NoReturn:
    # 400 for a token that doesn't match, 429 over the monthly limit, 409 for a second report, 503 when full.
    error = _ERRORS[exc.code](exc.detail, metadata=exc.metadata, error_code=exc.code)
    raise error from exc


@router.post("", response_model=ReportResponse, status_code=201)
async def report(body: ReportRequest, request: Request, account: str = Depends(require_account)) -> ReportResponse:
    """
    Report that a result is wrong. With ``consent``, this one check's page data is shared for review.

    Send back the ``report_token`` from the result with the request and explanation exactly as they were, so the
    report provably concerns a check this account got. Personal details are scrubbed before anything is stored.
    A page already in the queue counts the report as confirmation; a page reviewed recently is ``already_known``.
    If the review shows the score was far off (30+ points outside the label's zone), the first reporter gets free
    checks. Limits: a few reports per account per month, one per page.

    :param body: The report.
    :param request: The incoming request.
    :param account: The reporting account (injected by require_account).
    :return: The outcome.
    :raises ServiceUnavailableError: If reports are off (no session secret) or the queue is full.
    """
    secret = settings.session_secret.get_secret_value()
    if not secret or request.app.db_pool is None:
        raise ServiceUnavailableError("Reports are not available yet.")
    try:
        claim = reports.check_token(
            secret, body.report_token, account=account, body=body.request, explanation=body.explanation
        )
        done = await reports.submit(
            request.app.db_pool,
            settings,
            account=account,
            claim=claim,
            body=body.request,
            reason=body.reason,
            explanation=body.explanation,
            verdict=body.verdict.model_dump(mode="json") if body.verdict else None,
        )
    except reports.ReportError as exc:
        _refused(exc)
    return ReportResponse(status=done.outcome, reports_used=done.used, reports_per_month=done.limit)
