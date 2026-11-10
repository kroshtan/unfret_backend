"""
Sign-In With Solana: ``GET /v1/auth/nonce`` and ``POST /v1/auth/verify``, plus phone pairing (``/v1/auth/pair``).

Phone pairing lets the extension sign in with a mobile wallet: the extension starts a pairing and shows its connect
URL as a QR code; the phone opens it in the wallet's in-app browser and signs a message naming the pairing's
confirmation code; the extension then collects the session once (see :mod:`unfret.credits.pairing`).
"""

import base64
import binascii
from datetime import UTC, datetime
from typing import NoReturn

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from unfret.auth import get_credits
from unfret.config import settings
from unfret.credits import CreditsDoor, siws
from unfret.credits.pairing import Pairing, PairingError, PairState, pairing_statement
from unfret.schemas.errors import (
    ConflictError,
    CustomAPIError,
    GoneError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
)

logger = structlog.get_logger("unfret")

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


class VerifyRequest(BaseModel):
    """A signed sign-in message."""

    address: str = Field(max_length=64, description="The wallet's public key (base58).")
    signed_message: str = Field(max_length=4096, description="The exact bytes the wallet signed (base64).")
    signature: str = Field(max_length=128, description="The ed25519 signature (base64).")
    pair_id: str | None = Field(
        default=None, max_length=64, description="Attach the session to this phone pairing (sign-in on a phone)."
    )


class SessionResponse(BaseModel):
    """A session for a wallet."""

    token: str
    address: str
    expires_at: str


class PairResponse(BaseModel):
    """A new phone pairing, for the extension to show as a QR code."""

    pair_id: str
    confirm_code: str
    connect_url: str
    expires_at: str


class PairInfoResponse(BaseModel):
    """What the phone page shows: the code to compare and when the pairing expires."""

    confirm_code: str
    expires_at: str


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _door(request: Request) -> CreditsDoor:
    door = get_credits(request)
    if not door.sign_in_enabled:
        raise ServiceUnavailableError("Sign-in is not available yet.")
    return door


def _pairing_failure(exc: PairingError) -> NoReturn:
    # 410 for an expired pairing, 409 once it's signed in, 401 for a wrong code, 404 otherwise.
    error: CustomAPIError
    if exc.kind == "expired":
        error = GoneError(str(exc), error_code="PAIRING_EXPIRED")
    elif exc.kind == "bound":
        error = ConflictError(str(exc), error_code="PAIRING_USED")
    elif exc.kind == "mismatch":
        error = UnauthorizedError(str(exc), error_code="PAIRING_MISMATCH")
    else:
        error = NotFoundError(str(exc), error_code="PAIRING_UNKNOWN")
    raise error from exc


def _pairing(door: CreditsDoor, pair_id: str) -> Pairing:
    try:
        return door.pairings.info(pair_id)
    except PairingError as exc:
        _pairing_failure(exc)


@router.get("/nonce", response_model=NonceResponse)
def nonce(request: Request, pair: str | None = Query(default=None, max_length=64)) -> NonceResponse:
    """
    Start a sign-in: a fresh, short-lived nonce and the message fields to sign.

    With ``pair`` (a phone pairing), the statement names the pairing's confirmation code, and ``/verify`` only
    accepts that exact statement for that pairing.

    :param request: The incoming request.
    :param pair: The phone pairing id, when signing in on a phone for a desktop extension.
    :return: The sign-in input.
    """
    door = _door(request)
    statement = siws.STATEMENT
    if pair is not None:
        statement = pairing_statement(siws.STATEMENT, _pairing(door, pair).code)
    fields = siws.nonce_response(
        door.secret,
        domain=settings.siws_domain,
        uri=settings.public_base_url,
        ttl_s=settings.nonce_ttl_s,
        statement=statement,
    )
    return NonceResponse(**fields)


@router.post("/verify", response_model=SessionResponse)
async def verify(body: VerifyRequest, request: Request) -> SessionResponse:
    """
    Finish a sign-in: check the signed message and return a session token.

    The signature only proves control of the wallet; it grants no spending rights. A new wallet starts at
    zero checks.

    :param body: The signed message.
    :param request: The incoming request.
    :return: The session.
    :raises UnauthorizedError: If the message or signature is not acceptable.
    """
    door = _door(request)
    pairing = _pairing(door, body.pair_id) if body.pair_id is not None else None
    try:
        message = base64.b64decode(body.signed_message, validate=True)
        signature = base64.b64decode(body.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UnauthorizedError("Malformed signed message or signature.") from exc
    try:
        address = siws.verify_sign_in(
            door.secret,
            domain=settings.siws_domain,
            address=body.address,
            signed_message=message,
            signature=signature,
            statement=pairing_statement(siws.STATEMENT, pairing.code) if pairing is not None else None,
        )
    except siws.SignInError as exc:
        raise UnauthorizedError(str(exc)) from exc
    token, expires = siws.issue_token(door.secret, address, settings.session_ttl_s)
    if body.pair_id is not None and pairing is not None:
        try:
            door.pairings.bind(body.pair_id, pairing.code, token, address, expires)
        except PairingError as exc:
            _pairing_failure(exc)
        logger.info("signed_in", via="phone_pairing")
    else:
        logger.info("signed_in")
    return SessionResponse(token=token, address=address, expires_at=_iso(expires))


@router.post("/pair", response_model=PairResponse, status_code=201)
def start_pairing(request: Request) -> PairResponse:
    """
    Start a phone sign-in for the extension: a pairing id, its confirmation code and the URL to open on the phone.

    The pairing lives five minutes in memory. The extension shows ``connect_url`` as a QR code and the code next to
    it, then polls ``GET /v1/auth/pair/{pair_id}``.

    :param request: The incoming request.
    :return: The pairing.
    :raises TooManyRequestsError: If this visitor started too many pairings (or the server holds too many).
    """
    door = _door(request)
    created = door.pairings.create(request.client.host if request.client else "unknown")
    if created is None:
        raise TooManyRequestsError("Too many sign-in attempts; wait a few minutes.")
    pair_id, pairing = created
    return PairResponse(
        pair_id=pair_id,
        confirm_code=pairing.code,
        connect_url=f"{settings.public_base_url}/connect?pair={pair_id}",
        expires_at=_iso(pairing.expires),
    )


@router.get("/pair/{pair_id}/info", response_model=PairInfoResponse)
def pairing_info(pair_id: str, request: Request) -> PairInfoResponse:
    """
    What the phone shows before signing: the confirmation code to compare with the computer's screen.

    :param pair_id: The pairing id from the connect URL.
    :param request: The incoming request.
    :return: The code and the pairing's expiry.
    """
    pairing = _pairing(_door(request), pair_id[:64])
    return PairInfoResponse(confirm_code=pairing.code, expires_at=_iso(pairing.expires))


@router.get("/pair/{pair_id}", response_model=SessionResponse, responses={202: {"description": "Not signed in yet"}})
def collect_pairing(pair_id: str, request: Request) -> SessionResponse | JSONResponse:
    """
    The extension's poll: the session once the phone has signed in (handed over exactly once).

    :param pair_id: The pairing id.
    :param request: The incoming request.
    :return: 200 with the session (then the pairing is gone), or 202 while waiting.
    :raises GoneError: If the pairing expired before anyone signed in.
    :raises NotFoundError: If the pairing is unknown (or its session was already collected).
    """
    state, pairing = _door(request).pairings.poll(pair_id[:64])
    if state is PairState.READY and pairing is not None and pairing.token and pairing.address:
        return SessionResponse(
            token=pairing.token, address=pairing.address, expires_at=_iso(pairing.session_expires or 0)
        )
    if state is PairState.PENDING:
        return JSONResponse({"status": "pending"}, status_code=202, headers={"Cache-Control": "no-store"})
    if state is PairState.EXPIRED:
        raise GoneError("This pairing expired; start again.", error_code="PAIRING_EXPIRED")
    raise NotFoundError("Unknown pairing.", error_code="PAIRING_UNKNOWN")
