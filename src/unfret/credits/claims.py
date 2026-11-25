"""
Claim tokens: what ties a card checkout to the browser that started it.

The buyer's page gets a random *claim token* with a card checkout (``fiat-checkout`` → ``claim``); only its SHA-256 is
stored with the checkout (``fiat_checkouts.claim_hash``), and the token never goes to the payment provider. The page
polls the checkout's status with it: the first poll after payment marks the purchase as picked up by that browser
(``collected_at``), after which it can't be reissued to another browser (:func:`unfret.credits.fiat.reissue_pass`).
"""

import hashlib
import re
import secrets
from dataclasses import dataclass

import asyncpg

_CLAIM_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def digest(value: str) -> bytes:
    """
    The SHA-256 of a claim token, as stored.

    :param value: The token.
    :return: 32 bytes.
    """
    return hashlib.sha256(value.encode()).digest()


def new_claim() -> str:
    """
    A fresh claim token.

    :return: 256 random bits, URL-safe base64 (43 characters).
    """
    return secrets.token_urlsafe(32)


def claim_digest(claim: str | None) -> bytes | None:
    """
    The stored form of a claim token the caller presented.

    :param claim: The token, or ``None``.
    :return: Its hash, or ``None`` for a missing or malformed token.
    """
    return digest(claim) if claim and _CLAIM_RE.fullmatch(claim) else None


async def mark_checkout_paid(conn: asyncpg.Connection, provider: str, checkout_id: str) -> None:
    """
    Mark a card checkout paid (in the webhook's crediting transaction).

    :param conn: The connection.
    :param provider: The provider's name.
    :param checkout_id: The order's checkout.
    """
    await conn.execute(
        "UPDATE fiat_checkouts SET paid_at = NOW() WHERE provider = $1 AND checkout_id = $2 AND paid_at IS NULL",
        provider,
        checkout_id,
    )


@dataclass(frozen=True)
class CheckoutStatus:
    """A card checkout as its buyer's page sees it."""

    paid: bool
    checks: int | None
    reissued: bool = False  # picked up in another browser: the pass this browser made no longer works


async def checkout_status(
    db: asyncpg.Pool, provider: str, checkout_id: str, claim: str | None
) -> CheckoutStatus | None:
    """
    Whether a card checkout is paid; asked with the claim token once it is, the checkout counts as picked up.

    :param db: The database pool.
    :param provider: The provider's name.
    :param checkout_id: The checkout.
    :param claim: The claim token the caller presented (the checkout's id alone shows nothing).
    :return: The status, or ``None`` for an unknown checkout or a wrong or missing token.
    """
    hashed = claim_digest(claim)
    if hashed is None:
        return None
    row = await db.fetchrow(
        "UPDATE fiat_checkouts SET "
        "collected_at = CASE WHEN paid_at IS NOT NULL THEN COALESCE(collected_at, NOW()) END "
        "WHERE provider = $1 AND checkout_id = $2 AND claim_hash = $3 "
        "RETURNING paid_at IS NOT NULL AS paid, checks, reissued_at IS NOT NULL AS reissued",
        provider,
        checkout_id,
        hashed,
    )
    if row is None:
        return None
    return CheckoutStatus(bool(row["paid"]), row["checks"], reissued=bool(row["reissued"]))
