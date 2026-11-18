"""
Invites ("gift codes"): codes a buyer gets with a pack, to give to a friend.

**Issuing.** Every credited pack comes with one invite per ``invite_per_checks`` checks (25 → 1, 100 → 4), each
worth ``invite_checks`` checks (5 × the price of a check). They are made in the same database transaction that
credits the purchase: the crypto scanner's when a quote's first payment arrives (:func:`issue_for_quote`), the fiat
webhook's when an order is paid (:func:`issue_for_checkout`). Only purchases a buyer's page can collect from get
invites: a quote or card checkout started with a claim token (below). A transfer made by hand, without a quote, gets
none, and neither does a payment that brings in less than :data:`MIN_PAID_PCT` percent of the pack's value.

**Storage.** An invite is ``ufi_`` + 160 random bits. The ``invites`` table holds only its SHA-256 (nothing to
brute-force at that entropy), the day it was issued and the day it was redeemed, and a keyed hash of the buying account
(below): no redeemer, no IP. Nothing links the buyer to whoever redeems it.

**Shown once.** The buyer's page gets a random *claim token* with the purchase (``purchase-tx`` → ``claim``, the QR
code → ``X-Unfret-Claim``, ``fiat-checkout`` → ``claim``); only its hash is stored, with the quote or the checkout.
Unlike the quote id and the reference, the token never goes on chain or to the payment provider. When the purchase
is credited, the plaintext codes are kept in the quote's (or checkout's) row, and the first status request that
presents the token takes them: one ``UPDATE … RETURNING`` reads and wipes them atomically, so concurrent requests
can't both get them, and from then on only the hashes exist. Codes nobody collected go with their row: a quote a day
after it expires, a checkout's after :data:`UNCLAIMED_DAYS`. Until then the row links the codes to the account the
purchase credited, which is why it is wiped at the first read.

**Redeeming.** ``POST /v1/invites/redeem`` with the account's own sign-in: each code once, by any account, new or
in use, except the one whose purchase issued it (a gift is for a friend). For that, each invite keeps a keyed hash of
the buying account (``buyer_hash``: HMAC-SHA256 with the session secret, so the table alone names nobody); nothing
is kept about who redeems it but the account's ``invite_redeemed`` flag and the month of its latest gift code
(``gift_month``, never which code). Invites issued before the hash was kept can be redeemed by anyone. It is a
gift, not a sale: no VAT record, no sales row, only the ``credits_granted`` log line with ``kind="invite"`` (amounts
only).

**One a month.** An account redeems at most :data:`REDEEM_PER_MONTH` gift code a calendar month (UTC). A second code
in the same month is refused (``monthly``) and stays unused: it works on this account next month, or on someone
else's now. The check and the credit are one transaction (the account's row is locked by the credit), so two codes
at once can't both pass.
"""

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import asyncpg
import structlog

from unfret.config import Settings
from unfret.credits import ledger

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

Failure = Literal["invalid", "unknown", "used", "own", "monthly"]
# Gift codes an account may redeem per calendar month (UTC): the month of its latest one is kept (wallets.gift_month).
REDEEM_PER_MONTH = 1
MONTHLY_MESSAGE = (
    "You've already used a gift code this month; this one still works next month, or give it to someone else."
)


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


def buyer_digest(settings: Settings, account: str) -> bytes:
    """
    The keyed hash an invite keeps of the account whose purchase issued it.

    :param settings: The service settings (the session secret is the key).
    :param account: The buying account.
    :return: 32 bytes.
    """
    key = settings.session_secret.get_secret_value().encode()
    return hmac.new(key, ("gift-buyer:" + account).encode(), hashlib.sha256).digest()


async def _issue(
    conn: asyncpg.Connection, settings: Settings, count: int, today: date | None, buyer: str | None
) -> list[str]:
    codes = [new_code() for _ in range(count)]
    buyer_hash = buyer_digest(settings, buyer) if buyer else None
    await conn.executemany(
        "INSERT INTO invites (code_hash, issued_on, buyer_hash) VALUES ($1, $2, $3)",
        [(digest(code), today or datetime.now(UTC).date(), buyer_hash) for code in codes],
    )
    return codes


async def issue_for_quote(
    conn: asyncpg.Connection,
    settings: Settings,
    quote_id: str,
    paid_net_usd_micros: int,
    *,
    today: date | None = None,
    buyer: str | None = None,
) -> int:
    """
    Issue a crypto purchase's invites (in the scanner's crediting transaction, at its first payment).

    :param conn: The connection (inside the crediting transaction).
    :param settings: The service settings.
    :param quote_id: The quote paid.
    :param paid_net_usd_micros: The net credits the payment brought in.
    :param today: The issue day (injectable for tests).
    :param buyer: The account the purchase credited (which can't redeem them).
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
        codes = await _issue(conn, settings, count, today, buyer)
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
    buyer: str | None = None,
) -> int:
    """
    Issue a card purchase's invites (in the webhook's crediting transaction) and mark its checkout paid.

    :param conn: The connection (inside the crediting transaction).
    :param settings: The service settings.
    :param provider: The provider's name.
    :param checkout_id: The order's checkout.
    :param checks: The pack the order paid for exactly (0 when its amount matched no pack: no invites).
    :param today: The issue day (injectable for tests).
    :param buyer: The account the order credited (which can't redeem them).
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
        codes = await _issue(conn, settings, count, today, buyer)
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
    reissued: bool = False  # picked up in another browser: the pass this browser made no longer works


async def checkout_status(
    db: asyncpg.Pool, provider: str, checkout_id: str, claim: str | None
) -> CheckoutStatus | None:
    """
    Whether a card checkout is paid, and its invite codes the first time they're asked for (then wiped).

    Asked with the claim token once it is paid, the checkout counts as picked up by the browser that bought it
    (``collected_at``): from then on it can't be reissued elsewhere (:func:`unfret.credits.fiat.reissue_pass`).

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
        "UPDATE fiat_checkouts c SET invite_codes = NULL, "
        "collected_at = CASE WHEN c.paid_at IS NOT NULL THEN COALESCE(c.collected_at, NOW()) END "
        "FROM (SELECT provider, checkout_id, invite_codes "
        "FROM fiat_checkouts WHERE provider = $1 AND checkout_id = $2 AND claim_hash = $3 FOR UPDATE) old "
        "WHERE c.provider = old.provider AND c.checkout_id = old.checkout_id "
        "RETURNING c.paid_at IS NOT NULL AS paid, c.checks, old.invite_codes, c.reissued_at IS NOT NULL AS reissued",
        provider,
        checkout_id,
        hashed,
    )
    if row is None:
        return None
    return CheckoutStatus(
        bool(row["paid"]), row["checks"], list(row["invite_codes"] or []), reissued=bool(row["reissued"])
    )


@dataclass(frozen=True)
class Redeemed:
    """What a redemption added."""

    usd_micros: int
    balance_usd_micros: int


def next_month(today: date) -> date:
    """
    The first day of the month after ``today``'s: when an account can redeem a gift code again.

    :param today: A day (UTC).
    :return: The 1st of the next month.
    """
    return date(today.year + today.month // 12, today.month % 12 + 1, 1)


async def redeem(
    db: asyncpg.Pool, settings: Settings, account: str, value: str, *, today: date | None = None
) -> Redeemed:
    """
    Redeem a gift code (an invite) for any account but the buyer's, once, one a month.

    :param db: The database pool.
    :param settings: The service settings.
    :param account: The redeeming account (a wallet, or ``key:<id>``), from its own sign-in.
    :param value: The code, as typed.
    :param today: The day in UTC (injectable for tests).
    :return: The credit and the new balance.
    :raises InviteError: For a malformed, unknown or used code, the buyer's own pass, or a second gift code in a
        month.
    """
    code = normalise(value)
    if code is None:
        raise InviteError("invalid", "That isn't a gift code.")
    amount = value_usd_micros(settings)
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT redeemed_on, buyer_hash FROM invites WHERE code_hash = $1 FOR UPDATE", digest(code)
        )
        if row is None:
            raise InviteError("unknown", "Unknown gift code.")
        if row["redeemed_on"] is not None:
            raise InviteError("used", "This gift code has already been used.")
        if row["buyer_hash"] is not None and hmac.compare_digest(
            bytes(row["buyer_hash"]), buyer_digest(settings, account)
        ):
            raise InviteError("own", "This gift code came with your own pack: give it to a friend.")
        day = today or datetime.now(UTC).date()
        month = day.replace(day=1)
        # The credit makes (or locks) the account's row; the month is then checked and set under that lock, and a
        # refusal rolls the credit back, so the code stays unused.
        balance = await ledger.credit(conn, account, amount)
        first = await conn.fetchval(
            "UPDATE wallets SET invite_redeemed = TRUE, gift_month = $2 "
            "WHERE pubkey = $1 AND (gift_month IS NULL OR gift_month < $2) RETURNING TRUE",
            account,
            month,
        )
        if not first:
            raise InviteError("monthly", MONTHLY_MESSAGE)
        await conn.execute("UPDATE invites SET redeemed_on = $2 WHERE code_hash = $1", digest(code), day)
    logger.info("credits_granted", kind="invite", usd_micros=amount)
    return Redeemed(amount, balance)


async def _accounts(db: asyncpg.Pool) -> dict[str, int]:
    # A buyer is an account that bought and kept it: ``purchased``, unless every card order it has was refunded in full
    # or disputed (the money went back). A crypto purchase can't be refunded, so it always counts; crypto sales aren't
    # linked to an account, so an account with a crypto purchase and only reversed card orders isn't counted.
    row = await db.fetchrow(
        """
        WITH buyers AS (
            SELECT w.pubkey, w.invite_redeemed,
                   w.purchased AND (
                       NOT EXISTS (SELECT 1 FROM fiat_payments f WHERE f.account = w.pubkey)
                       OR EXISTS (SELECT 1 FROM fiat_payments f WHERE f.account = w.pubkey
                                  AND NOT f.disputed AND f.refunded_cents < f.amount_cents)
                   ) AS bought
            FROM wallets w
        )
        SELECT count(*) FILTER (WHERE invite_redeemed) AS via_code,
               count(*) FILTER (WHERE invite_redeemed AND bought) AS via_code_bought,
               count(*) FILTER (WHERE NOT invite_redeemed AND bought) AS organic_bought
        FROM buyers
        """
    )
    return {key: int(row[key]) for key in ("via_code", "via_code_bought", "organic_bought")}


async def summary(db: asyncpg.Pool, settings: Settings) -> dict[str, int]:
    """
    Invites issued, redeemed and outstanding (a gift's cost, not revenue), and buyers by how they came.

    :param db: The database pool.
    :param settings: The service settings.
    :return: Counts, what the outstanding ones would credit at today's price, and ``via_code`` (accounts that redeemed
        a code), ``via_code_bought`` (of those, buyers) and ``organic_bought`` (buyers that never redeemed one).
    """
    row = await db.fetchrow("SELECT count(*) AS issued, count(redeemed_on) AS redeemed FROM invites")
    issued, redeemed = int(row["issued"]), int(row["redeemed"])
    return {
        "issued": issued,
        "redeemed": redeemed,
        "outstanding": issued - redeemed,
        "outstanding_usd_micros": (issued - redeemed) * value_usd_micros(settings),
        **await _accounts(db),
    }
