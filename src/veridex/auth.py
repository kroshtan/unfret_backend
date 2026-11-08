import asyncio
from dataclasses import dataclass
from typing import Literal

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from veridex.config import settings
from veridex.schemas.errors import ForbiddenError

SubscriptionStatus = Literal["free", "premium", "admin", "blocked"]

# bcrypt only hashes the first 72 bytes and rejects longer input.
BCRYPT_MAX_PASSWORD_BYTES = 72

security = HTTPBasic()

# Checked against when the username does not exist, so unknown users take as long
# to reject as wrong passwords and usernames cannot be enumerated by timing.
_DUMMY_HASH = bcrypt.hashpw(b"veridex-dummy-password", bcrypt.gensalt())


@dataclass(frozen=True)
class Account:
    """The authenticated caller."""

    username: str
    subscription_status: SubscriptionStatus

    @property
    def daily_limit(self) -> int | None:
        """Maximum analyses per UTC day, or ``None`` for unlimited (admins)."""
        if self.subscription_status == "admin":
            return None
        if self.subscription_status == "premium":
            return settings.premium_daily_limit
        return settings.free_daily_limit


async def hash_password(password: str) -> str:
    """
    Hash a password with bcrypt off the event loop.

    :param password: The plain-text password.
    :return: The bcrypt hash as a string.
    """
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode(), bcrypt.gensalt())
    return hashed.decode()


async def verify_credentials(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> Account:
    """
    Verify HTTP Basic Auth credentials against the accounts table.

    :param request: The incoming request (used to access the DB pool).
    :param credentials: The HTTP Basic Auth credentials.
    :return: The authenticated account.
    :raises HTTPException: 401 if credentials are missing or invalid.
    :raises ForbiddenError: If the account is blocked.
    """
    username = credentials.username.lower()
    row = await request.app.db_pool.fetchrow(
        "SELECT hashed_password, subscription_status FROM accounts WHERE username = $1",
        username,
    )
    password = credentials.password.encode()
    stored_hash = row["hashed_password"].encode() if row else _DUMMY_HASH
    # Signup rejects longer passwords, so an over-long one can never match (and bcrypt would raise).
    password_ok = len(password) <= BCRYPT_MAX_PASSWORD_BYTES and await asyncio.to_thread(
        bcrypt.checkpw, password, stored_hash
    )
    if row is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
    if row["subscription_status"] == "blocked":
        raise ForbiddenError("Account is blocked.")
    return Account(username=username, subscription_status=row["subscription_status"])


async def require_admin(account: Account = Depends(verify_credentials)) -> Account:
    """
    Extend :func:`verify_credentials` to also require the admin tier.

    :param account: The authenticated account.
    :return: The authenticated admin account.
    :raises ForbiddenError: If the account is not an admin.
    """
    if account.subscription_status != "admin":
        raise ForbiddenError("Admin access required.")
    return account
