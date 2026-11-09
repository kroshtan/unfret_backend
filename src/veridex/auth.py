"""
Bearer authentication for the credits door: a wallet's SIWS session token, or an API key (``ufk_…``).

Either way the caller is a ledger account: the wallet's address, or ``key:<id>`` for a key.
"""

import hmac

from fastapi import Request

from veridex.config import settings
from veridex.credits import CreditsDoor, keys
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
        app.credits = CreditsDoor(settings, x402.rpc, hot, x402.prices)
    door: CreditsDoor = app.credits
    return door


def is_admin(request: Request) -> bool:
    """
    Whether the request carries the operator's ``Authorization: Bearer`` token (constant-time comparison).

    :param request: The incoming request.
    :return: ``False`` also while no admin token is configured.
    """
    expected = settings.admin_token.get_secret_value()
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), expected.encode())


def require_admin(request: Request) -> None:
    """
    FastAPI dependency: only the operator may call this.

    :param request: The incoming request.
    :raises UnauthorizedError: Without the admin token (or while none is configured).
    """
    if not is_admin(request):
        raise UnauthorizedError("Operator token required.")


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


async def require_account(request: Request) -> str:
    """
    FastAPI dependency: the account behind ``Authorization: Bearer``, a session token or an API key.

    :param request: The incoming request.
    :return: The wallet address (base58), or ``key:<id>`` for an API key.
    :raises ServiceUnavailableError: If accounts are not configured.
    :raises UnauthorizedError: If the token or key is missing, forged, expired or revoked.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token.startswith(keys.KEY_PREFIX):
        return require_wallet(request)
    if not get_credits(request).sign_in_enabled or request.app.db_pool is None:
        raise ServiceUnavailableError("API keys are not available yet.")
    try:
        return await keys.authenticate(request.app.db_pool, token)
    except keys.ApiKeyError as exc:
        raise UnauthorizedError(str(exc), error_code="INVALID_API_KEY") from exc
