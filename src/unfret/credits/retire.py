"""
Retiring passes: the operator's fresh start (``python -m unfret.admin_cli retire-passes``, admin API).

Every pass made before a moment is deleted, with every row that names it: its balance and flags (``wallets``), its
free checks (a column of ``api_keys``), an affiliate's attribution, open card checkouts and crypto quotes for it, its
open reports and the keyed hashes behind the report limits, and the buyer hash on gift codes it bought. Wallet
(crypto) accounts go the same way: a wallet keeps no date, so every wallet account is retired, whatever the moment.
Rows of passes that no longer exist (a refund that landed after a pass was deleted, say) go too. What stays are
anonymous counts: how many passes and wallets were retired on a day (``usage_counters``), and the aggregate tables that
never named anyone (affiliate earnings).

A retired pass is then unknown: it answers ``401 INVALID_API_KEY`` like any key the server doesn't know, and the
clients drop it and start the free-pass flow (with the free deep checks if that browser never had them).

**The moment.** A pass keeps only the UTC day it was made (``created_on``), never a time of day, so a time on the
cut-off day can't separate the passes made before it from those made after. Passes from earlier days are retired.
For ``--before`` with a time of day, passes made on that day are retired too unless they still hold free checks: the
free-pass flow (0.13.0) is the only maker of passes with free checks, so these are the new passes made after the
deploy, while a pass from before it never had any. A bare date (midnight) retires the earlier days only.

**Safety.** Without ``confirm`` nothing changes and only counts come back, never a key, a key id or a wallet. With
it, everything happens in one transaction, and a second run finds nothing left to do. It refuses, changing nothing,
when ``keep`` names a pass that doesn't exist (a typo would retire the pass it meant to keep), when a wallet holds a
non-zero balance (x402 refunds, crypto purchases) unless ``include_wallet_balances``, and when an account bought
checks (a card order on record or the ``purchased`` flag) unless ``include_purchases``. ``before`` may not be in the
future.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import asyncpg
import structlog

from unfret.config import Settings
from unfret.credits import invites, keys
from unfret.reports import account_tag

logger = structlog.get_logger("unfret")

# usage_counters names for the anonymous record of what was retired (a count a UTC day, nothing about anyone).
PASSES_COUNTER = "passes_retired"
WALLETS_COUNTER = "wallets_retired"

# Every column that names an account (or a keyed hash of one) and how retiring handles it: a test checks that no
# table has such a column this module doesn't know about.
ACCOUNT_COLUMNS = {
    ("wallets", "pubkey"),
    ("fiat_payments", "account"),
    ("fiat_checkouts", "account"),
    ("reports", "account"),
    ("report_votes", "account_tag"),
    ("report_quota", "account_tag"),
    ("vat_quotes", "beneficiary"),
    ("affiliate_passes", "account"),
    ("invites", "buyer_hash"),
}


class RetireError(ValueError):
    """The request can't be carried out as asked (a moment in the future, a malformed key id)."""


@dataclass(frozen=True)
class Cutoff:
    """The moment, as a pass can be compared with it: its UTC day, and whether passes from that day count."""

    day: date
    same_day: bool

    @classmethod
    def of(cls, before: datetime, now: datetime | None = None) -> Cutoff:
        """
        The cut-off for ``before``.

        :param before: The moment (naive: UTC).
        :param now: The time (now by default).
        :return: The cut-off.
        :raises RetireError: If ``before`` is in the future.
        """
        moment = before if before.tzinfo else before.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC)
        if moment > (now or datetime.now(UTC)):
            raise RetireError("'before' is in the future: only passes that exist can be retired.")
        return cls(moment.date(), moment.time() != time(0, 0))


@dataclass
class Report:
    """What a run found (and, confirmed, did): counts only."""

    before: str
    cutoff_day: str
    same_day: bool
    confirm: bool
    passes: int = 0
    passes_earlier_days: int = 0
    passes_same_day: int = 0
    kept_same_day_free: int = 0
    kept_named: int = 0
    keep_named: int = 0
    keep_unknown: int = 0
    passes_later: int = 0
    with_balance: int = 0
    balance_usd_micros: int = 0
    balance_checks: int = 0
    free_checks: int = 0
    redeemed_code: int = 0
    purchases: int = 0
    card_checkouts: int = 0
    crypto_quotes: int = 0
    affiliate_links: int = 0
    open_reports: int = 0
    report_limits: int = 0
    gift_codes_bought: int = 0
    wallets: int = 0
    wallets_with_balance: int = 0
    wallet_balance_usd_micros: int = 0
    wallet_balance_checks: int = 0
    orphan_rows: int = 0
    refused: list[str] = field(default_factory=list)
    retired: bool = False

    def as_dict(self) -> dict[str, Any]:
        """The report as JSON."""
        return asdict(self)


def _checks(usd_micros: int, price_usd_micros: int) -> int:
    return usd_micros // price_usd_micros if price_usd_micros > 0 else 0


async def _orphans(conn: asyncpg.Connection) -> list[str]:
    # Key accounts some table still names although the pass is gone (fiat_payments is the bookkeeping's: refused).
    rows = await conn.fetch(
        """
        SELECT account FROM (
            SELECT pubkey AS account FROM wallets
            UNION SELECT account FROM affiliate_passes
            UNION SELECT account FROM fiat_checkouts
            UNION SELECT account FROM reports
            UNION SELECT beneficiary FROM vat_quotes WHERE beneficiary IS NOT NULL
        ) named
        WHERE account LIKE 'key:%' AND NOT EXISTS (SELECT 1 FROM api_keys k WHERE 'key:' || k.id = named.account)
        """
    )
    return [str(r["account"]) for r in rows]


async def _count(conn: asyncpg.Connection, sql: str, *args: Any) -> int:
    return int(await conn.fetchval(sql, *args) or 0)


async def _select(conn: asyncpg.Connection, cut: Cutoff, keep: list[str], report: Report) -> list[str]:
    # A bare date: the days before it. A time of day: that day too (its passes with free checks are kept).
    last = cut.day if cut.same_day else cut.day - timedelta(days=1)
    rows = await conn.fetch(
        "SELECT id, created_on, free_checks FROM api_keys WHERE created_on <= $1 ORDER BY id FOR UPDATE", last
    )
    report.passes_later = await _count(conn, "SELECT count(*) FROM api_keys WHERE created_on > $1", last)
    found = {str(r["id"]) for r in await conn.fetch("SELECT id FROM api_keys WHERE id = ANY($1::text[])", keep)}
    report.keep_named, report.keep_unknown = len(keep), len(set(keep) - found)
    selected: list[str] = []
    for row in rows:
        key_id, earlier = str(row["id"]), row["created_on"] < cut.day
        if not earlier and row["free_checks"] > 0:
            report.kept_same_day_free += 1
        elif key_id in keep:
            report.kept_named += 1
        else:
            selected.append(key_id)
            report.passes_earlier_days += int(earlier)
            report.passes_same_day += int(not earlier)
            report.free_checks += int(row["free_checks"])
    report.passes = len(selected)
    return selected


async def _wallets(conn: asyncpg.Connection, report: Report, price: int) -> list[str]:
    rows = await conn.fetch("SELECT pubkey, balance_usd_micros FROM wallets WHERE pubkey NOT LIKE 'key:%' FOR UPDATE")
    report.wallets = len(rows)
    report.wallets_with_balance = sum(1 for r in rows if r["balance_usd_micros"] != 0)
    report.wallet_balance_usd_micros = sum(max(int(r["balance_usd_micros"]), 0) for r in rows)
    report.wallet_balance_checks = _checks(report.wallet_balance_usd_micros, price)
    return [str(r["pubkey"]) for r in rows]


async def _count_attached(
    conn: asyncpg.Connection, passes: list[str], accounts: list[str], report: Report, price: int
) -> None:
    balances = await conn.fetch(
        "SELECT balance_usd_micros, invite_redeemed FROM wallets WHERE pubkey = ANY($1::text[])", passes
    )
    report.with_balance = sum(1 for r in balances if r["balance_usd_micros"] != 0)
    report.balance_usd_micros = sum(max(int(r["balance_usd_micros"]), 0) for r in balances)
    report.balance_checks = _checks(report.balance_usd_micros, price)
    report.redeemed_code = sum(1 for r in balances if r["invite_redeemed"])
    report.purchases = await _count(
        conn,
        "SELECT count(DISTINCT account) FROM (SELECT account FROM fiat_payments WHERE account = ANY($1::text[]) "
        "UNION SELECT pubkey FROM wallets WHERE pubkey = ANY($1::text[]) AND purchased) bought",
        accounts,
    )
    named = "SELECT count(*) FROM {} WHERE {} = ANY($1::text[])"
    report.card_checkouts = await _count(conn, named.format("fiat_checkouts", "account"), accounts)
    report.crypto_quotes = await _count(conn, named.format("vat_quotes", "beneficiary"), accounts)
    report.affiliate_links = await _count(conn, named.format("affiliate_passes", "account"), accounts)
    report.open_reports = await _count(conn, named.format("reports", "account"), accounts)


async def _count_hashed(conn: asyncpg.Connection, tags: list[bytes], buyers: list[bytes], report: Report) -> None:
    report.report_limits = await _count(
        conn,
        "SELECT (SELECT count(*) FROM report_quota WHERE account_tag = ANY($1::bytea[])) "
        "+ (SELECT count(*) FROM report_votes WHERE account_tag = ANY($1::bytea[]))",
        tags,
    )
    report.gift_codes_bought = await _count(
        conn, "SELECT count(*) FROM invites WHERE buyer_hash = ANY($1::bytea[])", buyers
    )


def _refusals(report: Report, *, include_wallet_balances: bool, include_purchases: bool) -> list[str]:
    reasons = []
    if report.keep_unknown:
        reasons.append(f"{report.keep_unknown} of the {report.keep_named} passes to keep don't exist: check --keep.")
    if report.wallets_with_balance and not include_wallet_balances:
        reasons.append(
            f"{report.wallets_with_balance} wallets hold a balance: pass --include-wallet-balances to retire them."
        )
    if report.purchases and not include_purchases:
        reasons.append(f"{report.purchases} accounts bought checks: pass --include-purchases to retire them.")
    return reasons


async def _delete(
    conn: asyncpg.Connection,
    selected: list[str],
    accounts: list[str],
    hashes: tuple[list[bytes], list[bytes]],
    report: Report,
    *,
    period: str,
) -> None:
    tags, buyers = hashes
    await conn.execute("DELETE FROM report_votes WHERE account_tag = ANY($1::bytea[])", tags)
    await conn.execute("DELETE FROM report_quota WHERE account_tag = ANY($1::bytea[])", tags)
    await conn.execute("DELETE FROM reports WHERE account = ANY($1::text[])", accounts)  # their votes cascade
    await conn.execute("UPDATE invites SET buyer_hash = NULL WHERE buyer_hash = ANY($1::bytea[])", buyers)
    for table, column in (
        ("vat_quotes", "beneficiary"),
        ("fiat_checkouts", "account"),
        ("fiat_payments", "account"),
        ("affiliate_passes", "account"),
        ("wallets", "pubkey"),
    ):
        await conn.execute(f"DELETE FROM {table} WHERE {column} = ANY($1::text[])", accounts)  # noqa: S608 - fixed
    await conn.execute("DELETE FROM api_keys WHERE id = ANY($1::text[])", selected)
    for name, n in ((PASSES_COUNTER, report.passes), (WALLETS_COUNTER, report.wallets)):
        if n:
            await conn.execute(
                "INSERT INTO usage_counters (name, period, count) VALUES ($1, $2, $3) ON CONFLICT (name, period) "
                "DO UPDATE SET count = usage_counters.count + EXCLUDED.count",
                name,
                period,
                n,
            )


async def retire_passes(
    pool: asyncpg.Pool,
    *,
    before: datetime,
    keep: list[str],
    confirm: bool,
    settings: Settings,
    include_wallet_balances: bool = False,
    include_purchases: bool = False,
    now: datetime | None = None,
) -> Report:
    """
    Count (and with ``confirm``, retire) every pass made before ``before`` and every wallet account.

    :param pool: The database pool.
    :param before: The moment (see the module's docstring for what a time of day does).
    :param keep: Key ids of passes to keep whatever their age (the store reviewers' pass).
    :param confirm: Retire them; otherwise only count.
    :param settings: The service settings (the session secret keys the account hashes; the price of a check).
    :param include_wallet_balances: Retire wallets that hold a non-zero balance too.
    :param include_purchases: Retire accounts that bought checks too.
    :param now: The time (now by default).
    :return: The counts; ``retired`` once done, ``refused`` with the reasons when it wouldn't.
    :raises RetireError: If ``before`` is in the future or a key id is malformed.
    """
    cut = Cutoff.of(before, now)
    secret, price = settings.session_secret.get_secret_value(), settings.analysis_price_usd_micros
    if any(not keys.is_key_id(k) for k in keep):
        raise RetireError("The passes to keep must be key ids (10 letters a-z and 2-7).")
    keep = sorted(set(keep))
    report = Report(before=before.isoformat(), cutoff_day=cut.day.isoformat(), same_day=cut.same_day, confirm=confirm)
    async with pool.acquire() as conn, conn.transaction():
        # One retirement at a time; the rows it deletes are locked as it reads them.
        await conn.execute("LOCK TABLE api_keys IN SHARE ROW EXCLUSIVE MODE")
        selected = await _select(conn, cut, keep, report)
        passes = [keys.account_of(k) for k in selected]
        orphans = await _orphans(conn)
        accounts = passes + await _wallets(conn, report, price) + orphans
        report.orphan_rows = await _count(
            conn,
            "SELECT (SELECT count(*) FROM wallets WHERE pubkey = ANY($1::text[])) "
            "+ (SELECT count(*) FROM affiliate_passes WHERE account = ANY($1::text[])) "
            "+ (SELECT count(*) FROM fiat_checkouts WHERE account = ANY($1::text[])) "
            "+ (SELECT count(*) FROM reports WHERE account = ANY($1::text[])) "
            "+ (SELECT count(*) FROM vat_quotes WHERE beneficiary = ANY($1::text[]))",
            orphans,
        )
        await _count_attached(conn, passes, accounts, report, price)
        hashes = [account_tag(secret, a) for a in accounts], [invites.buyer_digest(settings, a) for a in accounts]
        await _count_hashed(conn, *hashes, report)
        report.refused = _refusals(
            report, include_wallet_balances=include_wallet_balances, include_purchases=include_purchases
        )
        if not confirm or report.refused:
            return report
        await _delete(conn, selected, accounts, hashes, report, period=(now or datetime.now(UTC)).date().isoformat())
        report.retired = True
    logger.info("passes_retired", passes=report.passes, wallets=report.wallets, orphans=report.orphan_rows)
    return report
