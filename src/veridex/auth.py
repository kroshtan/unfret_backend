"""Bearer-token authentication for the credits door: the caller is a wallet that signed in with SIWS."""

from fastapi import Request

from veridex.config import settings
from veridex.credits import CreditsDoor
from veridex.credits.siws import SignInError
from veridex.routes.check import get_x402
from veridex.schemas.errors import ServiceUnavailableError, UnauthorizedError


def get_credits(request: Request) -> CreditsDoor:
    """
    Return the app's credits door, creating it from settings on first use.

    :param request: The incoming request.
    :return: The door.
    """
    app = request.app
    if getattr(app, "credits", None) is None:
        x402 = get_x402(request)
        hot = x402.hot.pubkey if x402.hot is not None else None
        app.credits = CreditsDoor(settings, x402.rpc, hot)
    door: CreditsDoor = app.credits
    return door


def require_wallet(request: Request) -> str:
    """
    FastAPI dependency: the wallet behind the ``Authorization: Bearer`` session token.

    :param request: The incoming request.
    :return: The wallet address (base58).
    :raises ServiceUnavailableError: If sign-in is not configured.
    :raises UnauthorizedError: If the token is missing, forged or expired.
    """
    door = get_credits(request)
    if not door.sign_in_enabled:
        raise ServiceUnavailableError("Sign-in is not available yet.")
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UnauthorizedError("Sign in first (Authorization: Bearer <token>).")
    try:
        return door.check_token(token.strip())
    except SignInError as exc:
        raise UnauthorizedError(str(exc)) from exc
