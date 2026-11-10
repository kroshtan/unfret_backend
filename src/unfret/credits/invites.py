"""
Invites: codes a buyer gets with a pack, to give to someone who hasn't tried Unfret.

**Issuing.** Every credited pack comes with one invite per ``invite_per_checks`` checks (25 → 1, 50 → 2, 100 → 4),
each worth ``invite_checks`` checks (3 × the price of a check). They are made in the same database transaction that
credits the purchase: the crypto scanner's when a quote's first payment arrives (:func:`issue_for_quote`), the fiat
webhook's when an order is paid (:func:`issue_for_checkout`). Only purchases a buyer's page can collect from get
invites: a quote or card checkout started with a claim token (below). A transfer made by hand, without a quote, gets
none, and neither does a payment that brings in less than :data:`MIN_PAID_PCT` percent of the pack's value.

**Storage.** An invite is ``ufi_`` + 160 random bits. The ``invites`` table holds only its SHA-256 (nothing to
brute-force at that entropy), the day it was issued and the day it was redeemed: no buyer, no redeemer, no IP. Nothing
links the buyer to whoever redeems it.

**Shown once.** The buyer's page gets a random *claim token* with the purchase (``purchase-tx`` → ``claim``, the QR
code → ``X-Unfret-Claim``, ``fiat-checkout`` → ``claim``); only its hash is stored, with the quote or the checkout.
Unlike the quote id and the reference, the token never goes on chain or to the payment provider. When the purchase
is credited, the plaintext codes are kept in the quote's (or checkout's) row, and the first status request that
presents the token takes them: one ``UPDATE … RETURNING`` reads and wipes them atomically, so concurrent requests
can't both get them, and from then on only the hashes exist. Codes nobody collected go with their row: a quote a day
after it expires, a checkout's after :data:`UNCLAIMED_DAYS`. Until then the row links the codes to the account the
purchase credited, which is why it is wiped at the first read.

**Redeeming.** ``POST /v1/invites/redeem`` with the account's own sign-in: one code, once, for an account that has no
row in the ledger yet. Every credit (a purchase, a refund, a reward, a grant, an invite) creates that row, so "no row"
means a wallet we've never credited or a key that was never funded, and the redemption's own row (marked
``invite_redeemed``) refuses a second one. It is a gift, not a sale: no VAT record, no sales row, only the
``credits_granted`` log line with ``kind="invite"`` (amounts only).
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import asyncpg
import structlog

from unfret.config import Settings

logger = structlog.get_logger("unfret")

type Executor = asyncpg.Pool | asyncpg.Connection

PREFIX = "ufi_"
_CODE_RE = re.compile(r"^ufi_[a-z2-7]{32}$")
_CLAIM_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# A payment must bring in at least this share of the pack's value for its invites (SOL moves a little between quote
# and payment; a dust payment naming someone's quote must not).
MIN_PAID_PCT = 90
# Uncollected codes of a card checkout are wiped after this many days.
UNCLAIMED_DAYS = 7

Failure = Literal["invalid", "unknown", "used", "not_eligible"]


class InviteError(ValueError):
    """A code that can't be redeemed (malformed, unknown, used) or an account that may not redeem one."""

    def __init__(self, kind: Failure, message: str) -> None:
        """
        Name the failure.

        :param kind: What went wrong.
        :param message: For the caller.
        """
        super().__init__(message)
        self.kind = kind


def count_for(settings: Settings, checks: int) -> int:
    """
    How many invites a pack of ``checks`` checks comes with.

    :param settings: The service settings.
    :param checks: The pack size.
    :return: ``checks // invite_per_checks`` (0 while invites are off).
    """
    return checks // settings.invite_per_checks if settings.invite_per_checks else 0


def value_usd_micros(settings: Settings) -> int:
    """
    What one invite credits: ``invite_checks`` checks at the price of a check.

    :param settings: The service settings.
    :return: Micro-dollars.
    """
    return settings.invite_checks * settings.analysis_price_usd_micros


def new_code() -> str:
    """
    A fresh invite code.

    :return: ``ufi_`` + 32 base32 characters (160 random bits).
    """
    return PREFIX + base64.b32encode(secrets.token_bytes(20)).decode().lower()


def normalise(value: str) -> str | None:
    """
    An invite code as typed (spaces, capitals), or ``None`` if it isn't one.

    :param value: What the caller sent.
    :return: The code.
    """
    code = value.strip().lower()
    return code if _CODE_RE.fullmatch(code) else None


def digest(value: str) -> bytes:
    """
    The SHA-256 of a code or a claim token, as stored.

    :param value: The code or token.
    :return: 32 bytes.
    """
    return hashlib.sha256(value.encode()).digest()


def new_claim() -> str:
    """
    A claim token: what lets the buyer's page collect a purchase's invites, once.

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


async def _issue(conn: asyncpg.Connection, count: int, today: date | None) -> list[str]:
    codes = [new_code() for _ in range(count)]
    await conn.executemany(
        "INSERT INTO invites (code_hash, issued_on) VALUES ($1, $2)",
        [(digest(code), today or datetime.now(UTC).date()) for code in codes],
    )
    return codes


async def issue_for_quote(
    conn: asyncpg.Connection, settings: Settings, quote_id: str, paid_net_usd_micros: int, *, today: date | None = None
) -> int:
    """
    Issue a crypto purchase's invites (in the scanner's crediting transaction, at its first payment).

    :param conn: The connection (inside the crediting transaction).
    :param settings: The service settings.
    :param quote_id: The quote paid.
    :param paid_net_usd_micros: The net credits the payment brought in.
    :param today: The issue day (injectable for tests).
    :return: How many invites were issued (0 without a claim token, or for a short payment).
    """
    row = await conn.fetchrow(
        "SELECT checks, net_usd_micros FROM vat_quotes WHERE id = $1 AND claim_hash IS NOT NULL "
        "AND invite_codes IS NULL FOR UPDATE",
        quote_id,
    )
    if row is None or paid_net_usd_micros * 100 < row["net_usd_micros"] * MIN_PAID_PCT:
        return 0
    count = count_for(settings, int(row["checks"]))
    if count:
        codes = await _issue(conn, count, today)
        await conn.execute("UPDATE vat_quotes SET invite_codes = $2 WHERE id = $1", quote_id, codes)
    return count


async def issue_for_checkout(
    conn: asyncpg.Connection,
    settings: Settings,
    provider: str,
    checkout_id: str,
    checks: int,
    *,
    today: date | None = None,
) -> int:
    """
    Issue a card purchase's invites (in the webhook's crediting transaction) and mark its checkout paid.

    :param conn: The connection (inside the crediting transaction).
    :param settings: The service settings.
    :param provider: The provider's name.
    :param checkout_id: The order's checkout.
    :param checks: The pack the order paid for exactly (0 when its amount matched no pack: no invites).
    :param today: The issue day (injectable for tests).
    :return: How many invites were issued (0 for a checkout without a claim token or one already paid).
    """
    row = await conn.fetchrow(
        "UPDATE fiat_checkouts SET paid_at = NOW() WHERE provider = $1 AND checkout_id = $2 AND paid_at IS NULL "
        "RETURNING claim_hash IS NOT NULL AS claimable",
        provider,
        checkout_id,
    )
    count = count_for(settings, checks) if row is not None and row["claimable"] else 0
    if count:
        codes = await _issue(conn, count, today)
        await conn.execute(
            "UPDATE fiat_checkouts SET invite_codes = $3 WHERE provider = $1 AND checkout_id = $2",
            provider,
            checkout_id,
            codes,
        )
    return count


async def take_from_quote(db: asyncpg.Pool, quote_id: str, claim: str | None) -> list[str]:
    """
    Hand a crypto purchase's invite codes to its buyer: returned once, wiped in the same statement.

    :param db: The database pool.
    :param quote_id: The quote.
    :param claim: The claim token the caller presented.
    :return: The codes, or nothing (wrong or missing token, not paid yet, none issued, or already taken).
    """
    hashed = claim_digest(claim)
    if hashed is None:
        return []
    codes = await db.fetchval(
        "UPDATE vat_quotes q SET invite_codes = NULL "
        "FROM (SELECT id, invite_codes FROM vat_quotes WHERE id = $1 AND claim_hash = $2 FOR UPDATE) old "
        "WHERE q.id = old.id AND old.invite_codes IS NOT NULL RETURNING old.invite_codes",
        quote_id,
        hashed,
    )
    return list(codes or [])


@dataclass(frozen=True)
class CheckoutStatus:
    """A card checkout as its buyer's page sees it."""

    paid: bool
    checks: int | None
    invites: list[str]


async def checkout_status(
    db: asyncpg.Pool, provider: str, checkout_id: str, claim: str | None
) -> CheckoutStatus | None:
    """
    Whether a card checkout is paid, and its invite codes the first time they're asked for (then wiped).

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
        "UPDATE fiat_checkouts c SET invite_codes = NULL FROM (SELECT provider, checkout_id, invite_codes "
        "FROM fiat_checkouts WHERE provider = $1 AND checkout_id = $2 AND claim_hash = $3 FOR UPDATE) old "
        "WHERE c.provider = old.provider AND c.checkout_id = old.checkout_id "
        "RETURNING c.paid_at IS NOT NULL AS paid, c.checks, old.invite_codes",
        provider,
        checkout_id,
        hashed,
    )
    if row is None:
        return None
    return CheckoutStatus(bool(row["paid"]), row["checks"], list(row["invite_codes"] or []))


@dataclass(frozen=True)
class Redeemed:
    """What a redemption added."""

    usd_micros: int
    balance_usd_micros: int


async def redeem(db: asyncpg.Pool, settings: Settings, account: str, value: str) -> Redeemed:
    """
    Redeem an invite for an account that has never had any credit: once per code, once per account.

    :param db: The database pool.
    :param settings: The service settings.
    :param account: The redeeming account (a wallet, or ``key:<id>``), from its own sign-in.
    :param value: The code, as typed.
    :return: The credit and the new balance.
    :raises InviteError: For a malformed, unknown or used code, or an account that had credit before.
    """
    code = normalise(value)
    if code is None:
        raise InviteError("invalid", "That isn't an invite code (it starts with ufi_).")
    amount = value_usd_micros(settings)
    async with db.acquire() as conn, conn.transaction():
        redeemed_on = await conn.fetchrow(
            "SELECT redeemed_on FROM invites WHERE code_hash = $1 FOR UPDATE", digest(code)
        )
        if redeemed_on is None:
            raise InviteError("unknown", "Unknown invite code.")
        if redeemed_on["redeemed_on"] is not None:
            raise InviteError("used", "This invite has already been used.")
        # The account's ledger row is created here, or the account had credit before: the conflict refuses it.
        opened = await conn.fetchval(
            "INSERT INTO wallets (pubkey, balance_usd_micros, invite_redeemed) VALUES ($1, $2, TRUE) "
            "ON CONFLICT (pubkey) DO NOTHING RETURNING balance_usd_micros",
            account,
            amount,
        )
        if opened is None:
            raise InviteError(
                "not_eligible", "Invites are for new accounts: this one has had checks before, or used an invite."
            )
        await conn.execute("UPDATE invites SET redeemed_on = CURRENT_DATE WHERE code_hash = $1", digest(code))
    logger.info("credits_granted", kind="invite", usd_micros=amount)
    return Redeemed(amount, int(opened))


async def summary(db: asyncpg.Pool, settings: Settings) -> dict[str, int]:
    """
    Invites issued, redeemed and outstanding, for the operator (a gift's cost, not revenue).

    :param db: The database pool.
    :param settings: The service settings.
    :return: Counts, and what the outstanding ones would credit at today's price.
    """
    row = await db.fetchrow("SELECT count(*) AS issued, count(redeemed_on) AS redeemed FROM invites")
    issued, redeemed = int(row["issued"]), int(row["redeemed"])
    return {
        "issued": issued,
        "redeemed": redeemed,
        "outstanding": issued - redeemed,
        "outstanding_usd_micros": (issued - redeemed) * value_usd_micros(settings),
    }
