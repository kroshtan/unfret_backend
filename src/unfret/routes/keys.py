"""
API keys: create, recover, and put one on a phone.

``GET /v1/keys/challenge``, ``POST /v1/keys`` (create; a developer pass with its free checks:
:mod:`unfret.credits.free`), ``POST /v1/keys/recover`` (rotate), ``POST /v1/keys/move-free-checks`` (deprecated) and
``GET /v1/keys/phone-qr`` (a QR code that puts the key on a phone).

Shoppers know a key as their **Unfret pass** and its recovery code as their **backup code**; the API keeps the
technical names.

A key is an account without a wallet: create it (empty), then top it up with ``/v1/credits/purchase-tx``
(``key_id``) or, once a provider is wired in, ``/v1/credits/fiat-checkout``. Use it as ``Authorization: Bearer
ufk_…`` wherever a session token works. The key and its recovery code are shown once and stored only as hashes.
"""

import io
from typing import Literal

import segno
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from unfret import android_app_links, usage
from unfret.auth import get_credits, require_account
from unfret.config import settings
from unfret.credits import CreditsDoor, free, keys
from unfret.schemas.errors import BadRequestError, ServiceUnavailableError, TooManyRequestsError, UnauthorizedError

logger = structlog.get_logger("unfret")

router = APIRouter(prefix="/v1/keys", tags=["API keys"])


class ChallengeResponse(BaseModel):
    """A proof-of-work challenge for creating a key."""

    challenge: str
    bits: int = Field(
        description="Find a solution with SHA-256(challenge + ':' + solution) starting with this "
        "many zero bits (solution: at most 32 characters, e.g. a decimal counter)."
    )
    expires_at: float
    developer: bool = Field(
        default=False,
        description="A challenge for a developer pass (asked with ?developer=true): harder, and the only kind "
        'POST /v1/keys with "developer": true accepts.',
    )


class CreateKeyRequest(BaseModel):
    """A solved challenge."""

    challenge: str = Field(max_length=200)
    solution: str = Field(min_length=1, max_length=32)
    free: bool = Field(
        default=False,
        description="Deprecated and ignored: a shopper needs no pass (quick and deep checks are free without one), so "
        "only a developer pass comes with free checks.",
    )
    developer: bool = Field(
        default=False,
        description="Ask for a developer pass: more free deep checks (developer_free_checks, 20), for building and "
        "testing an agent against /v1/check, the MCP server or the clients. Its free checks count on POST /v1/check "
        "only, never on /v1/analyze (the website, the extension, the phone app); bought checks on it work everywhere. "
        "It needs a challenge from GET /v1/keys/challenge?developer=true (a harder proof of work). For trying "
        "Unfret out: one a day per network; past that the pass is made empty. Takes the place of free.",
    )
    source: PassSource | None = Field(
        default=None,
        description="Which client makes the pass (extension, website, android or api), for Unfret's anonymous usage "
        "counts only: never stored with the pass. Left out, it is guessed from the request's Origin.",
    )


PassSource = Literal["extension", "website", "android", "api"]
_SOURCE_OF_CLIENT: dict[str, PassSource] = {"extension": "extension", "site": "website", "app": "android"}


class RecoverRequest(BaseModel):
    """A recovery code."""

    recovery_code: str = Field(max_length=100)


class KeyResponse(BaseModel):
    """A key and its recovery code: shown this once, stored only as hashes. Save both."""

    key_id: str = Field(description="The public id: name it to top up the key (memos, checkout).")
    key: str = Field(description="Send as Authorization: Bearer <key>.")
    recovery_code: str = Field(description="Gets a new key (and a new code) with the same balance if the key is lost.")
    account: str = Field(description="The ledger account, key:<id>.")
    balance_usd_micros: int
    free_checks: int = Field(default=0, description="Free deep checks on this pass (spent before its balance).")
    developer: bool = Field(
        default=False,
        description="A developer pass: its free_checks count on POST /v1/check only (the API, the MCP server, the "
        "clients), not on /v1/analyze (the website, the extension, the phone app).",
    )


class MoveFreeChecksRequest(BaseModel):
    """Where to put this pass's free checks."""

    to_key_id: str = Field(description="The id of the pass to move them to (or the whole pass).", max_length=100)


class MoveFreeChecksResponse(BaseModel):
    """What moved."""

    moved: int = Field(
        description="Free checks moved (0 when there were none, no such pass, or either is a developer pass)."
    )


def _door(request: Request) -> CreditsDoor:
    door = get_credits(request)
    if not door.sign_in_enabled or request.app.db_pool is None:
        raise ServiceUnavailableError("API keys are not available yet.")
    return door


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/challenge", response_model=ChallengeResponse)
def challenge(request: Request, developer: bool = False) -> ChallengeResponse:
    """
    A proof-of-work challenge for ``POST /v1/keys``: a few seconds of hashing in a browser, used once.

    With ``?developer=true``, the challenge a developer pass needs: ``developer_pow_extra_bits`` harder (16 times the
    work at the default 4). A developer pass is for trying Unfret out: one per network a day.

    :param request: The incoming request.
    :param developer: Ask for a developer pass's challenge.
    :return: The challenge.
    """
    text, bits, expires = _door(request).key_guard.challenge(developer=developer)
    return ChallengeResponse(challenge=text, bits=bits, expires_at=expires, developer=developer)


async def issue_pass(
    request: Request,
    challenge: str,
    solution: str,
    *,
    developer: bool = False,
    source: str = "api",
) -> tuple[keys.IssuedKey, int]:
    """
    Make a pass for a solved challenge, within the network's limits: ``POST /v1/keys`` and the MCP sign-in page.

    :param request: The incoming request.
    :param challenge: The challenge, as issued.
    :param solution: The caller's answer.
    :param developer: A developer pass (its harder challenge, its own free checks).
    :param source: Which client makes it, for the anonymous ``pass_created`` event (:mod:`unfret.usage`).
    :return: The pass and how many free checks it came with.
    :raises BadRequestError: If the challenge is invalid, expired, used or unsolved.
    :raises TooManyRequestsError: If this network created too many keys today.
    """
    door = _door(request)
    guard = door.key_guard
    if not guard.redeem(challenge, solution, min_bits=guard.developer_bits if developer else 0):
        detail = "Challenge invalid, expired or unsolved; get a new one."
        if developer:
            detail += " A developer pass needs a developer challenge: GET /v1/keys/challenge?developer=true."
        raise BadRequestError(detail, error_code="BAD_CHALLENGE")
    if not door.key_guard.creations.allow(_ip(request), daily_cap=False):
        raise TooManyRequestsError("Too many new keys from here today; try again tomorrow.")
    # A developer pass's free checks only within this network's share of developer passes today; past it, the pass
    # is simply made empty. A shopper's pass never has free checks (their deep checks are free without a pass).
    given = 0
    if developer and settings.developer_free_checks > 0 and door.developer_passes.allow(_ip(request)):
        given = settings.developer_free_checks
    issued = await keys.create(request.app.db_pool, free_checks=given, developer=developer)
    logger.info("api_key_created", free_checks=given, developer=developer)
    usage.record("pass_created", source=source, developer=developer, free_checks=given > 0)
    return issued, given


@router.post("", response_model=KeyResponse, status_code=201)
async def create_key(body: CreateKeyRequest, request: Request) -> KeyResponse:
    """
    Create an empty API key with a solved challenge; a few per visitor network per day.

    400 ``BAD_CHALLENGE`` if the challenge is invalid, expired, used or unsolved; 429 if this network created too many
    keys today.

    :param body: The solved challenge.
    :param request: The incoming request.
    :return: The key and recovery code, shown this once.
    """
    source = body.source or _SOURCE_OF_CLIENT.get(usage.client_of(request.headers), "api")
    issued, given = await issue_pass(request, body.challenge, body.solution, developer=body.developer, source=source)
    return KeyResponse(
        key_id=issued.key_id,
        key=issued.key,
        recovery_code=issued.recovery_code,
        account=issued.account,
        balance_usd_micros=0,
        free_checks=given,
        developer=body.developer,
    )


@router.post("/recover", response_model=KeyResponse)
async def recover_key(body: RecoverRequest, request: Request) -> KeyResponse:
    """
    Replace a lost key: a new key and a new recovery code, the same id and balance. The old key and code stop working.

    :param body: The recovery code.
    :param request: The incoming request.
    :return: The new key and recovery code, shown this once.
    :raises TooManyRequestsError: If this network tried too many recoveries today.
    :raises UnauthorizedError: If the code is not (any longer) valid.
    """
    door = _door(request)
    if not door.key_guard.recoveries.allow(_ip(request)):
        raise TooManyRequestsError("Too many recovery attempts from here today; try again tomorrow.")
    try:
        issued = await keys.recover(request.app.db_pool, body.recovery_code)
    except keys.ApiKeyError as exc:
        raise UnauthorizedError(str(exc), error_code="INVALID_RECOVERY_CODE") from exc
    logger.info("api_key_recovered")
    left = await free.left(request.app.db_pool, issued.key_id, api=True)
    return KeyResponse(
        key_id=issued.key_id,
        key=issued.key,
        recovery_code=issued.recovery_code,
        account=issued.account,
        balance_usd_micros=await keys.balance(request.app.db_pool, issued.key_id),
        free_checks=left,
        developer=bool(
            await request.app.db_pool.fetchval("SELECT developer FROM api_keys WHERE id = $1", issued.key_id)
        ),
    )


@router.post("/move-free-checks", response_model=MoveFreeChecksResponse)
async def move_free_checks(
    body: MoveFreeChecksRequest, request: Request, account: str = Depends(require_account)
) -> MoveFreeChecksResponse:
    """
    Deprecated: always moves nothing.

    A shopper's pass no longer has free checks (deep checks are free without a pass), and a developer pass's never
    moved. Kept so older extensions get an answer.

    :param body: The other pass.
    :param request: The incoming request.
    :param account: The account (injected by require_account).
    :return: How many moved.
    :raises BadRequestError: For a wallet's session, or a malformed pass id.
    """
    if not keys.is_key_account(account):
        raise BadRequestError("Only a pass has free checks to move.")
    try:
        to_id = keys.key_id_from(body.to_key_id)
    except keys.ApiKeyError as exc:
        raise BadRequestError("Not a pass id.") from exc
    _door(request)
    del to_id
    return MoveFreeChecksResponse(moved=0)


@router.get(
    "/phone-qr",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "The QR code."}},
)
async def phone_qr(request: Request, account: str = Depends(require_account)) -> Response:
    """
    A QR code that puts this key on a phone: in the Unfret app when it's installed, else in the link checker.

    For a shopper who bought checks in one browser and wants them on their phone. The code holds
    ``<site>/pass#pass=<key>``: the key is in the fragment, which a browser never sends anywhere. On Android with the
    app installed the link opens the app (a verified App Link, :mod:`unfret.android_app_links`); anywhere else
    ``/pass`` forwards to ``/check`` with the fragment, which asks before it keeps the key like a pasted one and clears
    the address bar. Only for an API key (send it as ``Authorization: Bearer``); anyone who scans the code can use the
    key's checks, so pages show it only on request.

    :param request: The incoming request (its ``Authorization`` header is the key).
    :param account: The account (injected by require_account).
    :return: The SVG, never cached.
    :raises BadRequestError: For a wallet's session: a wallet signs in on the phone itself.
    """
    if not keys.is_key_account(account):
        raise BadRequestError("Only an API key can be moved to a phone this way; sign in with the wallet there.")
    key = request.headers["authorization"].partition(" ")[2].strip()
    buffer = io.BytesIO()
    link = f"{settings.public_base_url.rstrip('/')}{android_app_links.PHONE_LINK_PATH}#pass={key}"
    segno.make(link, error="m").save(buffer, kind="svg", scale=5, border=2, dark="#12302a", xmldecl=False)
    return Response(buffer.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
