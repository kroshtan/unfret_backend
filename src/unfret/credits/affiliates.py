"""
Affiliates and bonus codes: who brought a buyer, what the buyer gets for it, and what the affiliate earns.

**Codes.** One table (``bonus_codes``) holds every code a buyer types at checkout or follows in a link
(``unfret.net/?ref=CODE``): an affiliate's own code (no percentage: it attributes the pass and gives the first-pack
bonus), or an operator's bonus code giving ``bonus_pct`` more checks with a pack, optionally counted for an affiliate.
Codes give checks, never a lower price: the price is the same for everyone. A code may expire (``expires_on``, the last
day it works) and have a number of uses (``max_uses``, counted when a purchase with it is paid). One code per purchase.
A look-up that fails answers the same whatever the reason (unknown, ended, used up), so it never tells which codes
exist.

**Attribution.** A pass (``key:<id>``; wallets are never attributed) belongs to the affiliate whose code it first came
with (``affiliate_passes``): at its creation (``POST /v1/keys`` with ``ref``), from a page that holds it
(``POST /v1/affiliates/attribute``: the browser extension's pass, relayed by ``/welcome``), or at a paid purchase
started with an affiliate's code. Only a pass that has never bought anything can be attributed, and never twice: the
first attribution wins, an old customer can't be claimed, and nothing is stored but the pass, the affiliate and the
time (no IP, no URL, no code).

**The buyer's benefit.** The first pack bought on an attributed pass comes with ``affiliate_bonus_pct`` more checks
(whole checks), once per pass (``bonus_used``, set in the crediting transaction). An operator's bonus code gives its
``bonus_pct`` instead. Bonus checks are credited through the ledger as a credit that isn't a purchase, and are part of
a card order's credits, so a refund takes their share back.

**Commission.** ``affiliate_commission_pct`` of the net revenue of every purchase on an attributed pass for
``affiliate_months`` months after its attribution, while the affiliate is active, written in the transaction that
credits the purchase (``affiliate_earnings``, unique per order or transaction: never counted twice). Net revenue: for a
card order, Polar's ``net_amount`` (before VAT/sales tax) minus Polar's fee (``platform_fee_amount``;
estimated from ``card_fee_pct`` and ``card_fee_flat_usd`` when Polar doesn't report it in the order's currency); for
crypto, what arrived valued in dollars minus its VAT (no payment fee on our side). A refund reverses the refunded share
of the commission, a chargeback all of it, in the month it happens (``affiliate_reversals``); a crypto refund is made
by hand, and so is its reversal (:func:`reverse_earning`).

**Payouts** are made by hand (``make affiliates``): :func:`report` per affiliate and month, :func:`mark_paid` records
what is due (:func:`balances`: commission counts after a 30-day hold, a payout needs $25 or €25 per currency unless the
affiliate was switched off, and a refund after a payout is made up for by the next ones). An affiliate sees their own
numbers per month on a stats page reached through a secret link (:func:`new_stats_token`): 256 random bits, kept only
as a SHA-256, revocable. It shows counts and amounts, never a pass, a buyer or what anyone checked.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import asyncpg
import structlog

from unfret.config import Settings
from unfret.credits import keys

logger = structlog.get_logger("unfret")

type Executor = asyncpg.Pool | asyncpg.Connection

_CODE_RE = re.compile(r"^[A-Z0-9]{3,24}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
STATS_PREFIX = "ufa_"
_STATS_RE = re.compile(r"^ufa_[a-z2-7]{52}$")
NOT_VALID = "This code isn't valid, or has ended."

Source = Literal["card", "crypto"]


class CodeError(ValueError):
    """An operator command that can't be done: a malformed or taken code, an unknown affiliate."""


def normalise_code(value: str | None) -> str | None:
    """
    A code as typed (spaces, any case), or ``None`` if it can't be one.

    :param value: What the caller sent.
    :return: The code, upper case.
    """
    code = (value or "").strip().upper()
    return code if _CODE_RE.fullmatch(code) else None


@dataclass(frozen=True)
class Code:
    """A bonus code (or an affiliate's own code) as stored."""

    code: str
    affiliate_id: int | None
    bonus_pct: int | None
    expires_on: date | None
    max_uses: int | None
    uses: int
    active: bool
    created_on: date

    @property
    def is_affiliate_code(self) -> bool:
        """An affiliate's own code: attribution and the first-pack bonus, no bonus of its own."""
        return self.bonus_pct is None


@dataclass(frozen=True)
class Offer:
    """What a code gives the buyer, as the checkout shows it."""

    code: str
    bonus_pct: int
    first_pack_only: bool  # an affiliate's code: the bonus is for a pass's first pack only

    def label(self) -> str:
        """
        The offer in a few words.

        :return: E.g. ``+20% checks`` or ``+20% checks on your first pack``.
        """
        return f"+{self.bonus_pct}% checks" + (" on your first pack" if self.first_pack_only else "")


def _code(row: asyncpg.Record) -> Code:
    return Code(**{k: row[k] for k in Code.__dataclass_fields__})


async def get_code(db: Executor, code: str) -> Code | None:
    """
    A code as stored, whatever its state.

    :param db: A pool or connection.
    :param code: The code (normalised).
    :return: The code, or ``None``.
    """
    row = await db.fetchrow("SELECT * FROM bonus_codes WHERE code = $1", code)
    return None if row is None else _code(row)


async def usable(db: Executor, value: str | None, *, today: date | None = None) -> Code | None:
    """
    A code a buyer may use now: on, not past its last day, with uses left, and its affiliate (if any) active.

    :param db: A pool or connection.
    :param value: The code as typed.
    :param today: The day (tests).
    :return: The code, or ``None`` for every reason it can't be used (the caller says the same either way).
    """
    code = normalise_code(value)
    if code is None:
        return None
    row = await db.fetchrow(
        "SELECT c.* FROM bonus_codes c LEFT JOIN affiliates a ON a.id = c.affiliate_id "
        "WHERE c.code = $1 AND c.active AND (c.affiliate_id IS NULL OR a.active) "
        "AND (c.expires_on IS NULL OR c.expires_on >= $2) AND (c.max_uses IS NULL OR c.uses < c.max_uses)",
        code,
        today or datetime.now(UTC).date(),
    )
    return None if row is None else _code(row)


def offer(settings: Settings, code: Code) -> Offer:
    """
    What a code gives.

    :param settings: The service settings.
    :param code: The code.
    :return: The offer.
    """
    if code.is_affiliate_code:
        return Offer(code.code, settings.affiliate_bonus_pct, first_pack_only=True)
    return Offer(code.code, code.bonus_pct or 0, first_pack_only=False)


# ── Attribution ──────────────────────────────────────────────────────────────────────────────────────────────────


async def never_bought(db: Executor, account: str) -> bool:
    """
    Whether an account has never bought checks (crypto or card): the only kind that can still be attributed.

    :param db: A pool or connection.
    :param account: The account.
    :return: ``True`` if it never did.
    """
    bought = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM wallets WHERE pubkey = $1 AND purchased) "
        "OR EXISTS (SELECT 1 FROM fiat_payments WHERE account = $1)",
        account,
    )
    return not bought


async def _attribute(conn: Executor, account: str, affiliate_id: int) -> bool:
    if not keys.is_key_account(account):
        return False
    inserted = await conn.fetchval(
        "INSERT INTO affiliate_passes (account, affiliate_id) SELECT $1, $2 "
        "WHERE EXISTS (SELECT 1 FROM affiliates WHERE id = $2 AND active) "
        "ON CONFLICT (account) DO NOTHING RETURNING TRUE",
        account,
        affiliate_id,
    )
    return bool(inserted)


Attributed = Literal["attributed", "already", "not_eligible", "invalid"]


async def attribute(db: asyncpg.Pool, account: str, value: str | None) -> Attributed:
    """
    Attribute a pass to the affiliate of a code it came with (a link's ``ref``), if it can still be.

    :param db: The database pool.
    :param account: The pass (``key:<id>``).
    :param value: The code as sent.
    :return: ``attributed``; ``already`` (it had been, to this or another affiliate); ``not_eligible`` (a wallet,
        or a pass that has bought before); ``invalid`` (no usable affiliate code).
    """
    code = await usable(db, value)
    if code is None or code.affiliate_id is None:
        return "invalid"
    if not keys.is_key_account(account):
        return "not_eligible"
    async with db.acquire() as conn, conn.transaction():
        # Serialise with a purchase being credited for this pass: lock its ledger row if it has one.
        await conn.execute("SELECT 1 FROM wallets WHERE pubkey = $1 FOR UPDATE", account)
        if await conn.fetchval("SELECT TRUE FROM affiliate_passes WHERE account = $1", account):
            return "already"
        if not await never_bought(conn, account):
            return "not_eligible"
        done = await _attribute(conn, account, code.affiliate_id)
    if done:
        logger.info("affiliate_attributed", via="pass")
    return "attributed" if done else "already"


async def attribute_new(conn: Executor, account: str, value: str | None) -> bool:
    """
    Attribute a pass made just now (in the transaction that makes it) to a link's affiliate code.

    :param conn: A pool or connection.
    :param account: The new pass.
    :param value: The code as sent (anything unusable is ignored).
    :return: Whether it was attributed.
    """
    code = await usable(conn, value)
    if code is None or code.affiliate_id is None:
        return False
    return await _attribute(conn, account, code.affiliate_id)


async def first_check(db: Executor, account: str) -> None:
    """
    Note the day an attributed pass made its first check (for the affiliate's numbers; nothing about the check).

    :param db: A pool or connection.
    :param account: The account that checked.
    """
    if keys.is_key_account(account):
        await db.execute(
            "UPDATE affiliate_passes SET first_check_on = CURRENT_DATE WHERE account = $1 AND first_check_on IS NULL",
            account,
        )


# ── A purchase being credited ────────────────────────────────────────────────────────────────────────────────────


async def _use(conn: asyncpg.Connection, code: str) -> Code | None:
    """Count one use of a code, atomically, if it has one left; checkouts racing for the last use get it once."""
    row = await conn.fetchrow(
        "UPDATE bonus_codes SET uses = uses + 1 WHERE code = $1 AND (max_uses IS NULL OR uses < max_uses) RETURNING *",
        code,
    )
    return None if row is None else _code(row)


async def on_purchase(
    conn: asyncpg.Connection,
    settings: Settings,
    account: str,
    *,
    code: str | None,
    checks: int | None,
    first_purchase: bool,
) -> int:
    """
    Apply a paid purchase's code and attribution (in the crediting transaction, before the purchase is credited).

    Counts a use of the code (only while it has uses left: several purchases started on its last use get it once; the
    others are paid like a purchase without it), attributes a pass that has never bought to the code's affiliate, and
    works out the bonus checks: the code's ``bonus_pct``, or for an affiliate's code or no code the first-pack bonus of
    an attributed pass (once per pass).

    :param conn: The connection (inside the crediting transaction).
    :param settings: The service settings.
    :param account: The account credited.
    :param code: The code the purchase was started with (``None``: none).
    :param checks: The pack paid for in full (``None``: not a whole pack: no bonus, the first pack is still to come).
    :param first_purchase: Whether the account had never bought before this purchase (:func:`never_bought`, asked
        before it is credited).
    :return: The bonus in micro-dollars (0 for none).
    """
    known = await get_code(conn, code) if code else None
    linked = known.affiliate_id if known is not None else None
    if linked is not None and first_purchase and await _attribute(conn, account, linked):
        logger.info("affiliate_attributed", via="purchase")
    used = await _use(conn, known.code) if known is not None else None
    if known is not None and used is None:
        logger.warning("bonus_code_used_up_at_payment")
    if checks is None:
        return 0
    first_pack = await conn.fetchval(
        "UPDATE affiliate_passes SET bonus_used = TRUE WHERE account = $1 AND NOT bonus_used RETURNING TRUE",
        account,
    )
    if used is not None and used.bonus_pct is not None:
        pct = used.bonus_pct
    else:
        pct = settings.affiliate_bonus_pct if first_pack else 0
    bonus_checks = checks * pct // 100
    if bonus_checks:
        logger.info("credits_bonus", checks=bonus_checks, kind="code" if used and used.bonus_pct else "affiliate")
    return bonus_checks * settings.analysis_price_usd_micros


def card_fee_estimate(settings: Settings, amount_cents: int) -> int:
    """
    The provider's fee on a card order when it doesn't report one: the configured worst case.

    :param settings: The service settings.
    :param amount_cents: The order's net amount (before tax), in its currency's cents.
    :return: Cents (the flat part is taken as the same number of cents in either currency).
    """
    pct = Decimal(amount_cents) * settings.card_fee_pct / 100
    return int(pct.to_integral_value(rounding="ROUND_CEILING")) + int(settings.card_fee_flat_usd * 100)


async def earn(
    conn: asyncpg.Connection,
    settings: Settings,
    account: str,
    *,
    source: Source,
    ref: str,
    currency: str,
    net_minor: int,
    fee_estimated: bool = False,
) -> int:
    """
    Record the commission on a purchase, if the account is attributed to an active affiliate within the window.

    :param conn: The connection (inside the crediting transaction).
    :param settings: The service settings.
    :param account: The account credited.
    :param source: ``card`` or ``crypto``.
    :param ref: The purchase: ``<provider>:<order id>`` or ``<signature>:<account>``.
    :param currency: ``usd`` or ``eur``.
    :param net_minor: The net revenue, in the currency's cents.
    :param fee_estimated: The payment fee was estimated, not reported.
    :return: The commission in cents (0 when none is due).
    """
    affiliate_id = await conn.fetchval(
        "SELECT p.affiliate_id FROM affiliate_passes p JOIN affiliates a ON a.id = p.affiliate_id "
        "WHERE p.account = $1 AND a.active AND p.attributed_at > NOW() - make_interval(months => $2)",
        account,
        settings.affiliate_months,
    )
    if affiliate_id is None:
        return 0
    net = max(net_minor, 0)
    commission = net * settings.affiliate_commission_pct // 100
    inserted = await conn.fetchval(
        "INSERT INTO affiliate_earnings (affiliate_id, source, ref, currency, net_minor, commission_minor, "
        "fee_estimated) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (source, ref) DO NOTHING RETURNING TRUE",
        affiliate_id,
        source,
        ref[:200],
        currency,
        net,
        commission,
        fee_estimated,
    )
    if not inserted:
        return 0
    logger.info("affiliate_earned", source=source, currency=currency, commission_minor=commission)
    return commission


async def reverse(conn: asyncpg.Connection, source: Source, ref: str, *, share: tuple[int, int] | None) -> int:
    """
    Take back (part of) a commission after a refund or chargeback, idempotently.

    :param conn: The connection (inside the refund's transaction).
    :param source: ``card`` or ``crypto``.
    :param ref: The purchase, as :func:`earn` recorded it.
    :param share: ``(refunded, amount)``: the refunded share of the order so far; ``None``: all of it (a chargeback).
    :return: What was taken back now, in cents.
    """
    row = await conn.fetchrow(
        "SELECT id, commission_minor, reversed_minor FROM affiliate_earnings WHERE source = $1 AND ref = $2 "
        "FOR UPDATE",
        source,
        ref[:200],
    )
    if row is None:
        return 0
    commission = int(row["commission_minor"])
    target = commission if share is None else commission * min(share[0], share[1]) // max(share[1], 1)
    delta = max(target - int(row["reversed_minor"]), 0)
    if delta:
        await conn.execute(
            "UPDATE affiliate_earnings SET reversed_minor = reversed_minor + $2 WHERE id = $1", row["id"], delta
        )
        await conn.execute(
            "INSERT INTO affiliate_reversals (earning_id, amount_minor) VALUES ($1, $2)", row["id"], delta
        )
        logger.info("affiliate_reversed", source=source, amount_minor=delta)
    return delta


async def reverse_earning(db: asyncpg.Pool, earning_id: int) -> int | None:
    """
    Reverse an earning in full by hand (a crypto purchase refunded by the operator).

    :param db: The database pool.
    :param earning_id: The earning (from the report).
    :return: What was taken back, or ``None`` for an unknown earning.
    """
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT source, ref FROM affiliate_earnings WHERE id = $1", earning_id)
        if row is None:
            return None
        return await reverse(conn, row["source"], row["ref"], share=None)


# ── Operator: affiliates and codes ───────────────────────────────────────────────────────────────────────────────


def new_stats_token() -> str:
    """
    A stats-page token.

    :return: ``ufa_`` + 52 base32 characters (256 random bits).
    """
    return STATS_PREFIX + base64.b32encode(secrets.token_bytes(32)).decode().rstrip("=").lower()


def stats_digest(token: str) -> bytes | None:
    """
    The stored form of a stats token as presented.

    :param token: The token.
    :return: Its SHA-256, or ``None`` if it isn't one.
    """
    token = token.strip()
    return hashlib.sha256(token.encode()).digest() if _STATS_RE.fullmatch(token) else None


async def _code_taken(conn: Executor, code: str) -> bool:
    return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM bonus_codes WHERE code = $1)", code))


@dataclass(frozen=True)
class NewAffiliate:
    """An affiliate just made: their code and the stats token, shown this once."""

    id: int
    code: str
    name: str
    stats_token: str


async def create_affiliate(db: asyncpg.Pool, code: str, name: str, payout_note: str = "") -> NewAffiliate:
    """
    Make an affiliate with their own code.

    :param db: The database pool.
    :param code: Their code, as typed.
    :param name: What the operator calls them.
    :param payout_note: How to pay them (IBAN, PayPal, wallet), for the operator.
    :return: The affiliate and their stats token (only its hash is kept).
    :raises CodeError: For a malformed or taken code, or an empty name.
    """
    normal = normalise_code(code)
    if normal is None:
        raise CodeError("A code is 3 to 24 letters or digits.")
    if not name.strip():
        raise CodeError("An affiliate needs a name.")
    token = new_stats_token()
    async with db.acquire() as conn, conn.transaction():
        if await _code_taken(conn, normal):
            raise CodeError(f"{normal} is taken.")
        affiliate_id = await conn.fetchval(
            "INSERT INTO affiliates (name, payout_note, stats_hash) VALUES ($1, $2, $3) RETURNING id",
            name.strip()[:100],
            payout_note.strip()[:500],
            stats_digest(token),
        )
        await conn.execute("INSERT INTO bonus_codes (code, affiliate_id) VALUES ($1, $2)", normal, affiliate_id)
    return NewAffiliate(int(affiliate_id), normal, name.strip()[:100], token)


async def affiliate_id_of(db: Executor, code: str) -> int | None:
    """
    The affiliate a code belongs to.

    :param db: A pool or connection.
    :param code: The code, as typed.
    :return: The affiliate's id, or ``None``.
    """
    normal = normalise_code(code)
    if normal is None:
        return None
    value = await db.fetchval("SELECT affiliate_id FROM bonus_codes WHERE code = $1", normal)
    return None if value is None else int(value)


async def update_affiliate(
    db: asyncpg.Pool, code: str, *, active: bool | None = None, payout_note: str | None = None
) -> bool:
    """
    Switch an affiliate on or off, or change their payout note.

    Off: their codes attribute nothing new and their passes earn no more commission (bonuses already promised to
    attributed passes still apply).

    :param db: The database pool.
    :param code: One of their codes.
    :param active: On or off (``None``: unchanged).
    :param payout_note: The new note (``None``: unchanged).
    :return: Whether there was such an affiliate.
    """
    affiliate_id = await affiliate_id_of(db, code)
    if affiliate_id is None:
        return False
    await db.execute(
        "UPDATE affiliates SET active = COALESCE($2, active), payout_note = COALESCE($3, payout_note) WHERE id = $1",
        affiliate_id,
        active,
        None if payout_note is None else payout_note.strip()[:500],
    )
    return True


async def delete_affiliate(db: asyncpg.Pool, code: str) -> bool | None:
    """
    Remove an affiliate made by mistake (or for a test): their codes and attributions go too.

    Only while nothing was earned or paid: an affiliate with money on the books is switched off instead.

    :param db: The database pool.
    :param code: One of their codes.
    :return: ``True`` when removed, ``False`` when they have earnings or payouts, ``None`` for an unknown affiliate.
    """
    affiliate_id = await affiliate_id_of(db, code)
    if affiliate_id is None:
        return None
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM affiliates WHERE id = $1 FOR UPDATE", affiliate_id)
        booked = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM affiliate_earnings WHERE affiliate_id = $1) "
            "OR EXISTS (SELECT 1 FROM affiliate_payouts WHERE affiliate_id = $1)",
            affiliate_id,
        )
        if booked:
            return False
        await conn.execute("DELETE FROM affiliate_passes WHERE affiliate_id = $1", affiliate_id)
        await conn.execute("DELETE FROM bonus_codes WHERE affiliate_id = $1", affiliate_id)
        await conn.execute("DELETE FROM affiliates WHERE id = $1", affiliate_id)
    return True


async def rotate_stats(db: asyncpg.Pool, code: str, *, revoke: bool = False) -> str | None:
    """
    Give an affiliate a new stats link (the old one stops working), or revoke it.

    :param db: The database pool.
    :param code: One of their codes.
    :param revoke: Revoke without a new one.
    :return: The new token (``""`` when revoked), or ``None`` for an unknown affiliate.
    """
    affiliate_id = await affiliate_id_of(db, code)
    if affiliate_id is None:
        return None
    token = "" if revoke else new_stats_token()
    await db.execute(
        "UPDATE affiliates SET stats_hash = $2 WHERE id = $1", affiliate_id, stats_digest(token) if token else None
    )
    return token


async def create_code(
    db: asyncpg.Pool,
    code: str,
    *,
    bonus_pct: int,
    expires_on: date | None = None,
    max_uses: int | None = None,
    affiliate: str | None = None,
) -> Code:
    """
    Make an operator's bonus code: more checks with a pack, optionally counted for an affiliate.

    :param db: The database pool.
    :param code: The code, as typed.
    :param bonus_pct: Percent more checks (1-200).
    :param expires_on: The last day it works.
    :param max_uses: How many purchases may use it.
    :param affiliate: One of the affiliate's codes, to count sales with this code for them.
    :return: The code.
    :raises CodeError: For a malformed or taken code, a bonus out of range, or an unknown affiliate.
    """
    normal = normalise_code(code)
    if normal is None:
        raise CodeError("A code is 3 to 24 letters or digits.")
    if not 1 <= bonus_pct <= 200:
        raise CodeError("Bonus checks are 1 to 200 percent.")
    affiliate_id = None
    if affiliate:
        affiliate_id = await affiliate_id_of(db, affiliate)
        if affiliate_id is None:
            raise CodeError(f"No affiliate with the code {affiliate}.")
    async with db.acquire() as conn, conn.transaction():
        if await _code_taken(conn, normal):
            raise CodeError(f"{normal} is taken.")
        row = await conn.fetchrow(
            "INSERT INTO bonus_codes (code, affiliate_id, bonus_pct, expires_on, max_uses) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING *",
            normal,
            affiliate_id,
            bonus_pct,
            expires_on,
            max_uses,
        )
    return _code(row)


async def set_code_active(db: asyncpg.Pool, code: str, active: bool) -> Code | None:
    """
    Switch a bonus code on or off.

    :param db: The database pool.
    :param code: The code.
    :param active: On or off.
    :return: The code, or ``None`` if there is none.
    """
    row = await db.fetchrow(
        "UPDATE bonus_codes SET active = $2 WHERE code = $1 RETURNING *", normalise_code(code) or code, active
    )
    return None if row is None else _code(row)


async def list_codes(db: asyncpg.Pool) -> list[dict[str, Any]]:
    """
    Every operator bonus code (affiliates' own codes are in :func:`list_affiliates`).

    :param db: The database pool.
    :return: The codes, with their affiliate's name.
    """
    rows = await db.fetch(
        "SELECT c.*, a.name AS affiliate FROM bonus_codes c LEFT JOIN affiliates a ON a.id = c.affiliate_id "
        "WHERE c.bonus_pct IS NOT NULL ORDER BY c.created_on, c.code"
    )
    return [{**asdict(_code(row)), "affiliate": row["affiliate"]} for row in rows]


async def list_affiliates(db: asyncpg.Pool, settings: Settings) -> list[dict[str, Any]]:
    """
    Every affiliate, their codes, passes and their balance per currency (:func:`balances`).

    :param db: The database pool.
    :param settings: The service settings (the hold and the minimum payout).
    :return: One entry per affiliate.
    """
    rows = await db.fetch(
        """
        SELECT a.id, a.name, a.payout_note, a.active, a.stats_hash IS NOT NULL AS stats_link, a.created_on,
               (SELECT array_agg(code ORDER BY code) FROM bonus_codes c WHERE c.affiliate_id = a.id) AS codes,
               (SELECT count(*) FROM affiliate_passes p WHERE p.affiliate_id = a.id) AS passes
        FROM affiliates a ORDER BY a.id
        """
    )
    balance = await balances(db, settings)
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "payout_note": row["payout_note"],
            "active": row["active"],
            "stats_link": row["stats_link"],
            "created_on": row["created_on"].isoformat(),
            "codes": list(row["codes"] or []),
            "passes": int(row["passes"]),
            "balance": balance.get(int(row["id"]), {}),
        }
        for row in rows
    ]


# ── Reports, payouts and the stats page ──────────────────────────────────────────────────────────────────────────


def month_bounds(month: str) -> tuple[date, date]:
    """
    The first day of a month and of the next.

    :param month: ``YYYY-MM``.
    :return: ``(start, end)``, end exclusive.
    :raises ValueError: If it isn't a month.
    """
    if not _MONTH_RE.fullmatch(month):
        raise ValueError("A month reads like 2026-11.")
    start = date(int(month[:4]), int(month[5:]), 1)
    return start, (start + timedelta(days=32)).replace(day=1)


async def balances(
    db: Executor, settings: Settings, affiliate_id: int | None = None, *, today: date | None = None
) -> dict[int, dict[str, dict[str, int]]]:
    """
    What each affiliate has to come, per currency, in cents.

    Commission counts as payable ``affiliate_hold_days`` (30) days after the purchase, Polar's refund window, so most
    refunds land before it is paid; until then it is ``held``. A refund or chargeback takes its share off the earning
    whenever it comes: before the payout it simply lowers what is paid, after it the balance goes below zero and the
    next payouts make up for it (``carried_over`` negative). ``due`` is what the next payout pays: the payable balance
    once it reaches ``affiliate_min_payout_cents`` ($25 or €25, per currency, never converted), or all of it for an
    affiliate who was switched off; the rest is carried over.

    :param db: A pool or connection.
    :param settings: The service settings.
    :param affiliate_id: One affiliate, or ``None`` for all.
    :param today: The day (tests).
    :return: ``{affiliate_id: {currency: {"payable", "held", "due", "carried_over"}}}``.
    """
    cutoff = (today or datetime.now(UTC).date()) - timedelta(days=settings.affiliate_hold_days)
    rows = await db.fetch(
        """
        WITH e AS (
            SELECT affiliate_id, currency,
                   sum(commission_minor - reversed_minor) FILTER (WHERE earned_on <= $1) AS matured,
                   sum(commission_minor - reversed_minor) FILTER (WHERE earned_on > $1) AS held
              FROM affiliate_earnings GROUP BY affiliate_id, currency
        ), p AS (
            SELECT affiliate_id, currency, sum(amount_minor) AS paid FROM affiliate_payouts
             GROUP BY affiliate_id, currency
        )
        SELECT affiliate_id, currency, COALESCE(e.matured, 0) AS matured, COALESCE(e.held, 0) AS held,
               COALESCE(p.paid, 0) AS paid, a.active
          FROM e FULL JOIN p USING (affiliate_id, currency) JOIN affiliates a ON a.id = affiliate_id
         WHERE ($2::int IS NULL OR affiliate_id = $2)
        """,
        cutoff,
        affiliate_id,
    )
    out: dict[int, dict[str, dict[str, int]]] = {}
    for row in rows:
        payable = int(row["matured"]) - int(row["paid"])
        enough = payable >= settings.affiliate_min_payout_cents or not row["active"]
        due = payable if payable > 0 and enough else 0
        out.setdefault(int(row["affiliate_id"]), {})[row["currency"]] = {
            "payable": payable,
            "held": int(row["held"]),
            "due": due,
            "carried_over": payable - due,
        }
    return out


async def monthly(db: Executor, affiliate_id: int | None, months: int = 13) -> list[dict[str, Any]]:
    """
    Per affiliate and month: passes, first checks, purchases, and per currency net, commission, reversed and paid.

    :param db: A pool or connection.
    :param affiliate_id: One affiliate, or ``None`` for all.
    :param months: How many months back from the current one.
    :return: Rows ``{affiliate_id, month, passes, first_checks, purchases, money: {currency: {...}}}``, newest first.
    """
    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)
    rows = await db.fetch(
        """
        WITH events AS (
            SELECT affiliate_id, to_char(attributed_at AT TIME ZONE 'UTC', 'YYYY-MM') AS month, 'passes' AS kind,
                   NULL::text AS currency, 1::bigint AS n FROM affiliate_passes WHERE attributed_at >= $1
            UNION ALL
            SELECT affiliate_id, to_char(first_check_on, 'YYYY-MM'), 'first_checks', NULL, 1 FROM affiliate_passes
             WHERE first_check_on >= $1
            UNION ALL
            SELECT affiliate_id, to_char(earned_on, 'YYYY-MM'), 'purchases', NULL, 1 FROM affiliate_earnings
             WHERE earned_on >= $1
            UNION ALL
            SELECT affiliate_id, to_char(earned_on, 'YYYY-MM'), 'net', currency, net_minor FROM affiliate_earnings
             WHERE earned_on >= $1
            UNION ALL
            SELECT affiliate_id, to_char(earned_on, 'YYYY-MM'), 'commission', currency, commission_minor
              FROM affiliate_earnings WHERE earned_on >= $1
            UNION ALL
            SELECT e.affiliate_id, to_char(r.reversed_on, 'YYYY-MM'), 'reversed', e.currency, r.amount_minor
              FROM affiliate_reversals r JOIN affiliate_earnings e ON e.id = r.earning_id WHERE r.reversed_on >= $1
            UNION ALL
            SELECT affiliate_id, month, 'paid', currency, amount_minor FROM affiliate_payouts WHERE month >= $2
        )
        SELECT affiliate_id, month, kind, currency, sum(n) AS n FROM events
         WHERE ($3::int IS NULL OR affiliate_id = $3)
         GROUP BY affiliate_id, month, kind, currency ORDER BY month DESC, affiliate_id
        """,
        start,
        start.strftime("%Y-%m"),
        affiliate_id,
    )
    table: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        entry = table.setdefault(
            (int(row["affiliate_id"]), row["month"]),
            {
                "affiliate_id": int(row["affiliate_id"]),
                "month": row["month"],
                "passes": 0,
                "first_checks": 0,
                "purchases": 0,
                "money": {},
            },
        )
        if row["currency"] is None:
            entry[row["kind"]] = int(row["n"])
        else:
            money = entry["money"].setdefault(row["currency"], {"net": 0, "commission": 0, "reversed": 0, "paid": 0})
            money[row["kind"]] = int(row["n"])
    return sorted(table.values(), key=lambda e: (e["month"], -e["affiliate_id"]), reverse=True)


async def report(db: asyncpg.Pool, settings: Settings, month: str | None = None) -> dict[str, Any]:
    """
    The operator's monthly report: every affiliate's months (earned, reversed, paid) and their balance now.

    :param db: The database pool.
    :param settings: The service settings.
    :param month: Only this month (``YYYY-MM``); ``None``: the last 13.
    :return: ``{"affiliates": {id: {name, codes, payout_note, active, balance}}, "months": [...], "rules": {...}}``.
    """
    rows = await monthly(db, None)
    if month:
        month_bounds(month)
        rows = [r for r in rows if r["month"] == month]
    return {
        "affiliates": {str(a["id"]): a for a in await list_affiliates(db, settings)},
        "months": rows,
        "rules": {"hold_days": settings.affiliate_hold_days, "min_payout_cents": settings.affiliate_min_payout_cents},
    }


async def mark_paid(
    db: asyncpg.Pool, settings: Settings, code: str, note: str = "", *, today: date | None = None
) -> dict[str, int] | None:
    """
    Record a payout made by hand: what is due now (:func:`balances`), per currency.

    :param db: The database pool.
    :param settings: The service settings.
    :param code: One of the affiliate's codes.
    :param note: How it was paid (a reference).
    :param today: The day (tests).
    :return: What was recorded per currency (empty when nothing is due), or ``None`` for an unknown affiliate.
    """
    affiliate_id = await affiliate_id_of(db, code)
    if affiliate_id is None:
        return None
    day = today or datetime.now(UTC).date()
    paid: dict[str, int] = {}
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM affiliates WHERE id = $1 FOR UPDATE", affiliate_id)
        due = (await balances(conn, settings, affiliate_id, today=day)).get(affiliate_id, {})
        for currency, balance in due.items():
            if balance["due"] > 0:
                await conn.execute(
                    "INSERT INTO affiliate_payouts (affiliate_id, month, currency, amount_minor, paid_on, note) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    affiliate_id,
                    day.strftime("%Y-%m"),
                    currency,
                    balance["due"],
                    day,
                    note.strip()[:500],
                )
                paid[currency] = balance["due"]
    return paid


async def stats(db: asyncpg.Pool, settings: Settings, token: str) -> dict[str, Any] | None:
    """
    An affiliate's own numbers, for their stats page: counts and amounts per month, and their balance now.

    :param db: The database pool.
    :param settings: The service settings.
    :param token: The stats token, as presented.
    :return: The numbers, or ``None`` for an unknown or revoked token.
    """
    digest = stats_digest(token)
    if digest is None:
        return None
    row = await db.fetchrow("SELECT id, name, active FROM affiliates WHERE stats_hash = $1", digest)
    if row is None:
        return None
    affiliate_id = int(row["id"])
    codes = await db.fetch(
        # Their own code first: the stats page builds their link from it.
        "SELECT code FROM bonus_codes WHERE affiliate_id = $1 ORDER BY bonus_pct IS NOT NULL, code",
        affiliate_id,
    )
    months = await monthly(db, affiliate_id)
    for entry in months:
        del entry["affiliate_id"]
    return {
        "name": row["name"],
        "active": row["active"],
        "codes": [c["code"] for c in codes],
        "months": months,
        "balance": (await balances(db, settings, affiliate_id)).get(affiliate_id, {}),
        "rules": {"hold_days": settings.affiliate_hold_days, "min_payout_cents": settings.affiliate_min_payout_cents},
    }


async def purge(db: asyncpg.Pool, settings: Settings, now: datetime | None = None) -> None:
    """
    Forget attributions past their use, and earnings, reversals and payouts past the bookkeeping retention.

    :param db: The database pool.
    :param settings: The service settings.
    :param now: Current time (tests).
    """
    now = now or datetime.now(UTC)
    await db.execute(
        "DELETE FROM affiliate_passes WHERE attributed_at < $1",
        now - timedelta(days=settings.affiliate_pass_retention_days),
    )
    cutoff = now.date() - timedelta(days=settings.affiliate_records_retention_days)
    await db.execute(
        "DELETE FROM affiliate_reversals WHERE earning_id IN (SELECT id FROM affiliate_earnings WHERE earned_on < $1)",
        cutoff,
    )
    await db.execute("DELETE FROM affiliate_earnings WHERE earned_on < $1", cutoff)
    await db.execute("DELETE FROM affiliate_payouts WHERE paid_on < $1", cutoff)
