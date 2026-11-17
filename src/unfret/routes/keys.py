"""
API keys: create, recover, and put one on a phone.

``GET /v1/keys/challenge``, ``POST /v1/keys`` (create, with free checks on request: :mod:`unfret.credits.free`),
``POST /v1/keys/recover`` (rotate), ``POST /v1/keys/move-free-checks`` and ``GET /v1/keys/phone-qr`` (a QR code that
puts the key on a phone).

Shoppers know a key as their **Unfret pass** and its recovery code as their **backup code**; the API keeps the
technical names.

A key is an account without a wallet: create it (empty), then top it up with ``/v1/credits/purchase-tx``
(``key_id``) or, once a provider is wired in, ``/v1/credits/fiat-checkout``. Use it as ``Authorization: Bearer
ufk_…`` wherever a session token works. The key and its recovery code are shown once and stored only as hashes.
"""

import io

import segno
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

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


class CreateKeyRequest(BaseModel):
    """A solved challenge."""

    challenge: str = Field(max_length=200)
    solution: str = Field(min_length=1, max_length=32)
    free: bool = Field(
        default=False,
        description="Ask for the free checks a new pass comes with (the browser extension does on install). Given "
        "only while this network hasn't had its share of free passes today; the response's free_checks says how many.",
    )


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


class MoveFreeChecksRequest(BaseModel):
    """Where to put this pass's free checks."""

    to_key_id: str = Field(description="The id of the pass to move them to (or the whole pass).", max_length=100)


class MoveFreeChecksResponse(BaseModel):
    """What moved."""

    moved: int = Field(description="Free checks moved (0 when there were none, or no such pass).")


def _door(request: Request) -> CreditsDoor:
    door = get_credits(request)
    if not door.sign_in_enabled or request.app.db_pool is None:
        raise ServiceUnavailableError("API keys are not available yet.")
    return door


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/challenge", response_model=ChallengeResponse)
def challenge(request: Request) -> ChallengeResponse:
    """
    A proof-of-work challenge for ``POST /v1/keys``: a few seconds of hashing in a browser, used once.

    :param request: The incoming request.
    :return: The challenge.
    """
    text, bits, expires = _door(request).key_guard.challenge()
    return ChallengeResponse(challenge=text, bits=bits, expires_at=expires)


@router.post("", response_model=KeyResponse, status_code=201)
async def create_key(body: CreateKeyRequest, request: Request) -> KeyResponse:
    """
    Create an empty API key with a solved challenge; a few per visitor network per day.

    :param body: The solved challenge.
    :param request: The incoming request.
    :return: The key and recovery code, shown this once.
    :raises BadRequestError: If the challenge is invalid, expired, used or unsolved.
    :raises TooManyRequestsError: If this network created too many keys today.
    """
    door = _door(request)
    if not door.key_guard.redeem(body.challenge, body.solution):
        raise BadRequestError("Challenge invalid, expired or unsolved; get a new one.", error_code="BAD_CHALLENGE")
    if not door.key_guard.creations.allow(_ip(request), daily_cap=False):
        raise TooManyRequestsError("Too many new keys from here today; try again tomorrow.")
    # Free checks only within this network's share of free passes today; past it, the pass is simply made empty.
    given = 0
    if body.free and settings.free_checks_per_pass > 0 and door.free_passes.allow(_ip(request)):
        given = settings.free_checks_per_pass
    issued = await keys.create(request.app.db_pool, free_checks=given)
    logger.info("api_key_created", free_checks=given)
    return KeyResponse(
        key_id=issued.key_id,
        key=issued.key,
        recovery_code=issued.recovery_code,
        account=issued.account,
        balance_usd_micros=0,
        free_checks=given,
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
    return KeyResponse(
        key_id=issued.key_id,
        key=issued.key,
        recovery_code=issued.recovery_code,
        account=issued.account,
        balance_usd_micros=await keys.balance(request.app.db_pool, issued.key_id),
        free_checks=await free.left(request.app.db_pool, issued.key_id),
    )


@router.post("/move-free-checks", response_model=MoveFreeChecksResponse)
async def move_free_checks(
    body: MoveFreeChecksRequest, request: Request, account: str = Depends(require_account)
) -> MoveFreeChecksResponse:
    """
    Move this pass's free checks to another pass: the extension does it when it switches to a pass its user bought.

    Only free checks move (a balance never does), and only from the pass that authenticates the request
    (``Authorization: Bearer ufk_…``).

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
    moved = await free.move(request.app.db_pool, account.removeprefix(keys.ACCOUNT_PREFIX), to_id)
    logger.info("free_checks_moved", checks=moved)
    return MoveFreeChecksResponse(moved=moved)


@router.get(
    "/phone-qr",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}, "description": "The QR code."}},
)
async def phone_qr(request: Request, account: str = Depends(require_account)) -> Response:
    """
    A QR code that puts this key on a phone: it opens the link checker (``/check``) signed in with it.

    For a shopper who bought checks in one browser and wants them in the phone app. The code holds
    ``<site>/check#pass=<key>``: the key is in the fragment, which a browser never sends anywhere; ``/check`` reads it,
    keeps it like a pasted key and clears the address bar. Only for an API key (send it as ``Authorization: Bearer``);
    anyone who scans the code can use the key's checks, so pages show it only on request.

    :param request: The incoming request (its ``Authorization`` header is the key).
    :param account: The account (injected by require_account).
    :return: The SVG, never cached.
    :raises BadRequestError: For a wallet's session: a wallet signs in on the phone itself.
    """
    if not keys.is_key_account(account):
        raise BadRequestError("Only an API key can be moved to a phone this way; sign in with the wallet there.")
    key = request.headers["authorization"].partition(" ")[2].strip()
    buffer = io.BytesIO()
    link = f"{settings.public_base_url.rstrip('/')}/check#pass={key}"
    segno.make(link, error="m").save(buffer, kind="svg", scale=5, border=2, dark="#12302a", xmldecl=False)
    return Response(buffer.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
