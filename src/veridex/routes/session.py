"""Sign-In With Solana: ``GET /v1/auth/nonce`` and ``POST /v1/auth/verify``."""

import base64
import binascii
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from veridex.auth import get_credits
from veridex.config import settings
from veridex.credits import siws
from veridex.schemas.errors import ServiceUnavailableError, UnauthorizedError

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/auth", tags=["Sign-in"])


class NonceResponse(BaseModel):
    """The fields a wallet signs (Wallet Standard ``solana:signIn`` input)."""

    domain: str
    uri: str
    statement: str
    nonce: str
    issued_at: str
    expiration_time: str
    version: str
    chain_id: str


class VerifyRequest(BaseModel):
    """A signed sign-in message."""

    address: str = Field(max_length=64, description="The wallet's public key (base58).")
    signed_message: str = Field(max_length=4096, description="The exact bytes the wallet signed (base64).")
    signature: str = Field(max_length=128, description="The ed25519 signature (base64).")


class SessionResponse(BaseModel):
    """A session for a wallet."""

    token: str
    address: str
    expires_at: str


@router.get("/nonce", response_model=NonceResponse)
def nonce(request: Request) -> NonceResponse:
    """
    Start a sign-in: a fresh, short-lived nonce and the message fields to sign.

    :param request: The incoming request.
    :return: The sign-in input.
    :raises ServiceUnavailableError: If sign-in is not configured.
    """
    door = get_credits(request)
    if not door.sign_in_enabled:
        raise ServiceUnavailableError("Sign-in is not available yet.")
    fields = siws.nonce_response(
        door.secret,
        domain=settings.siws_domain,
        uri=settings.public_base_url,
        chain_id=door.chain_id,
        ttl_s=settings.nonce_ttl_s,
    )
    return NonceResponse(**fields)


@router.post("/verify", response_model=SessionResponse)
async def verify(body: VerifyRequest, request: Request) -> SessionResponse:
    """
    Finish a sign-in: check the signed message and return a session token.

    The signature only proves control of the wallet; it grants no spending rights. A wallet's first sign-in may
    come with the free trial (only for wallets with on-chain history).

    :param body: The signed message.
    :param request: The incoming request.
    :return: The session.
    :raises ServiceUnavailableError: If sign-in is not configured.
    :raises UnauthorizedError: If the message or signature is not acceptable.
    """
    door = get_credits(request)
    if not door.sign_in_enabled:
        raise ServiceUnavailableError("Sign-in is not available yet.")
    try:
        message = base64.b64decode(body.signed_message, validate=True)
        signature = base64.b64decode(body.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UnauthorizedError("Malformed signed message or signature.") from exc
    try:
        address = siws.verify_sign_in(
            door.secret, domain=settings.siws_domain, address=body.address, signed_message=message, signature=signature
        )
    except siws.SignInError as exc:
        raise UnauthorizedError(str(exc)) from exc
    pool = request.app.db_pool
    if pool is not None:
        await door.first_sign_in(pool, address, request.client.host if request.client else "unknown")
    token, expires = siws.issue_token(door.secret, address, settings.session_ttl_s)
    logger.info("signed_in")
    return SessionResponse(
        token=token, address=address, expires_at=datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
