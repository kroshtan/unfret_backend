"""
``POST /v1/labels`` ("Wrong result?") and ``/v1/contributors``: reports, contributor profiles, the leaderboard.

One flow (:mod:`unfret.reports`, :mod:`unfret.crowd`): a report keeps the page it is about, scrubbed, never who
sent it. ``POST /v1/reports`` is its older, compatible form. No sign-in, no IP address kept or used.
"""

from typing import Literal, NoReturn

import asyncpg
from fastapi import APIRouter, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from unfret import crowd, reports
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

router = APIRouter(tags=["Labels"])

KEY_DESCRIPTION = (
    "Optional contributor key: 32 random bytes in base64url (43 characters, no padding), made on the device and kept "
    "there. Send it only with labels and profile calls, never with a check. Unfret keeps a keyed hash of it."
)


class LabelRequest(BaseModel):
    """A "Wrong result?" report: the level you think is right, and which findings are wrong or missing."""

    model_config = ConfigDict(populate_by_name=True)

    label_token: str = Field(max_length=1500, description="The label_token from the analysis result.")
    request: AnalyzeRequest = Field(
        description="The analysed request, exactly as it was sent: the report keeps that page (scrubbed of personal "
        "details); a URL-only check's page is fetched again."
    )
    level: int | None = Field(
        default=None,
        ge=0,
        le=4,
        description="The page's OVERALL risk level as you see it, higher is worse: 0 no concern, 1 pressure and "
        "tricks, 2 may cost more than it's worth, 3 may not work as described, 4 may not arrive or may not be safe.",
    )
    wrong: list[str] = Field(
        default_factory=list,
        max_length=crowd.MAX_TYPES,
        validation_alias=AliasChoices("wrong", "disagree"),
        description="Findings of this result that are wrong (verdict.reasons[].type or risk.because[].type). "
        "`disagree` is accepted as the same.",
    )
    missing: list[str] = Field(
        default_factory=list,
        max_length=crowd.MAX_TYPES,
        description="Findings this result should have shown: reason types from the copy catalogue it didn't show.",
    )
    explanation: str = Field(default="", max_length=4000, description="The result's explanation, as returned.")
    verdict: Verdict | None = Field(default=None, description="The result's verdict, as returned.")
    contributor_key: str | None = Field(default=None, max_length=64, description=KEY_DESCRIPTION)


class LabelResponse(BaseModel):
    """The report is in."""

    status: reports.Outcome = Field(
        description="queued (the page is in the review queue), already_reported (it was: yours confirms it), "
        "already_known (reviewed recently)."
    )
    updated: bool = Field(
        default=False, description="This contributor had reported the page before: this replaces it."
    )
    anonymous: bool = Field(description="True without a contributor key: it counts only where others agree.")


class ContributorRequest(BaseModel):
    """Make or change a contributor profile."""

    contributor_key: str = Field(max_length=64, description=KEY_DESCRIPTION)
    handle: str | None = Field(
        default=None,
        max_length=24,
        description='A pseudonymous handle for the leaderboard (3-24 letters, digits, - or _); "" removes it.',
    )
    public: bool | None = Field(default=None, description="Show on the public leaderboard (needs a handle).")


class ContributorKey(BaseModel):
    """A contributor key, alone."""

    contributor_key: str = Field(max_length=64, description=KEY_DESCRIPTION)


class Contributor(BaseModel):
    """A contributor's profile."""

    handle: str | None
    public: bool
    points: int = Field(description="Reports whose level agreed with the settled answer (the leaderboard's score).")
    labels: int = Field(description="Reports sent.")
    settled: int = Field(description="Labels on pages whose answer has settled.")
    agreed: int = Field(description="Of those, how many agreed with the settled answer.")
    trusted: bool = Field(description="Past probation: labels count fully.")
    rank: int | None = Field(default=None, description="Place on the public leaderboard, when on it.")


class LeaderboardEntry(BaseModel):
    """One public contributor."""

    rank: int
    handle: str
    agreed: int
    settled: int


class Leaderboard(BaseModel):
    """Contributors who opted in, past probation, by labels that agreed with the settled answer."""

    entries: list[LeaderboardEntry]


class LabelLimitError(CustomAPIError):
    """No more labels today."""

    status_code = 429
    error_code = "LABEL_LIMIT"


_ERRORS: dict[str, type[CustomAPIError]] = {
    "BAD_LABEL_TOKEN": BadRequestError,
    "BAD_LABEL": BadRequestError,
    "BAD_CONTRIBUTOR_KEY": BadRequestError,
    "BAD_HANDLE": BadRequestError,
    "HANDLE_TAKEN": ConflictError,
    "ALREADY_LABELLED": ConflictError,
    "LABEL_LIMIT": TooManyRequestsError,
    "LABELS_PAUSED": ServiceUnavailableError,
    "QUEUE_FULL": ServiceUnavailableError,
}


def _refused(exc: crowd.LabelError | reports.ReportError) -> NoReturn:
    raise _ERRORS[exc.code](exc.detail, error_code=exc.code) from exc


def _pool(request: Request) -> asyncpg.Pool:
    pool = request.app.db_pool
    if pool is None or not settings.crowd_labels or not settings.session_secret.get_secret_value():
        raise ServiceUnavailableError("Reports aren't taken right now.", error_code="LABELS_OFF")
    return pool


@router.post("/v1/labels", response_model=LabelResponse, status_code=201)
async def send_label(body: LabelRequest, request: Request) -> LabelResponse:
    """
    Report a wrong result: the level you think is right, and which findings are wrong or missing.

    Send the result's ``label_token`` back with the analysed request exactly as it was sent: the report keeps that
    page (scrubbed of personal details) for review, never who sent it. Reports never change a result; reviewed and
    agreeing reports become training data. Without a contributor key a report is anonymous, weighs little and counts
    only where others agree; one per result. With a key: one per page (a new one replaces the old), 50 a day, and
    points on the leaderboard (opt-in).

    :param body: The report.
    :param request: The incoming request.
    :return: What happened.
    :raises crowd.LabelError: (as an HTTP error) a token for another page.
    """
    pool = _pool(request)
    secret = settings.session_secret.get_secret_value()
    try:
        shown = crowd.check_token(secret, body.label_token)
        if shown.input_hash != reports.input_hash(body.request):
            raise crowd.LabelError("BAD_LABEL_TOKEN", "The page data doesn't match the check it claims to be.")
        done = await reports.submit(
            pool,
            settings,
            body=await reports.with_page(body.request),
            shown=shown,
            level=body.level,
            wrong=body.wrong,
            missing=body.missing,
            contributor_key=body.contributor_key,
            explanation=body.explanation,
            verdict=body.verdict.model_dump(mode="json") if body.verdict else None,
        )
    except (crowd.LabelError, reports.ReportError) as exc:
        _refused(exc)
    if done.report_id is not None:
        second_opinion.schedule(pool, settings, done.report_id)
    return LabelResponse(status=done.outcome, updated=done.status == "updated", anonymous=body.contributor_key is None)


@router.post("/v1/contributors", response_model=Contributor)
async def contributor_profile(body: ContributorRequest, request: Request) -> Contributor:
    """
    Make, change or read a contributor profile (fields left out stay as they are).

    :param body: The key and what to change.
    :param request: The incoming request.
    :return: The profile.
    """
    pool = _pool(request)
    try:
        data = await crowd.profile(pool, settings, body.contributor_key, handle=body.handle, public=body.public)
    except crowd.LabelError as exc:
        _refused(exc)
    return Contributor.model_validate(data)


@router.post("/v1/contributors/forget", response_model=dict[str, Literal[True, False]])
async def forget_contributor(body: ContributorKey, request: Request) -> dict[str, bool]:
    """
    Delete a contributor profile. Labels already sent stay, unlinked, and no longer count.

    :param body: The key.
    :param request: The incoming request.
    :return: ``{"deleted": bool}``.
    """
    pool = _pool(request)
    try:
        return {"deleted": await crowd.forget(pool, settings, body.contributor_key)}
    except crowd.LabelError as exc:
        _refused(exc)


@router.get("/v1/contributors/leaderboard", response_model=Leaderboard)
async def leaderboard(request: Request, limit: int = 50) -> Leaderboard:
    """
    The public leaderboard (pseudonymous handles of contributors who opted in).

    :param request: The incoming request.
    :param limit: How many (at most 100).
    :return: The entries.
    """
    pool = _pool(request)
    rows = await crowd.leaderboard(pool, settings, limit)
    return Leaderboard(entries=[LeaderboardEntry.model_validate(r) for r in rows])
