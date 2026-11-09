"""
API keys: ``GET /v1/keys/challenge``, ``POST /v1/keys`` (create) and ``POST /v1/keys/recover`` (rotate).

A key is an account without a wallet: create it (empty), then top it up with ``/v1/credits/purchase-tx``
(``key_id``) or, once a provider is wired in, ``/v1/credits/fiat-checkout``. Use it as ``Authorization: Bearer
ufk_…`` wherever a session token works. The key and its recovery code are shown once and stored only as hashes.
"""

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from veridex.auth import get_credits
from veridex.credits import CreditsDoor, keys
from veridex.schemas.errors import BadRequestError, ServiceUnavailableError, TooManyRequestsError, UnauthorizedError

logger = structlog.get_logger("veridex")

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
    if not door.key_guard.creations.allow(_ip(request)):
        raise TooManyRequestsError("Too many new keys from here today; try again tomorrow.")
    issued = await keys.create(request.app.db_pool)
    logger.info("api_key_created")
    return KeyResponse(
        key_id=issued.key_id,
        key=issued.key,
        recovery_code=issued.recovery_code,
        account=issued.account,
        balance_usd_micros=0,
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
    )
