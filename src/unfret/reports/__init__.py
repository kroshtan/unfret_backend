"""
Reports of wrong results: "this is wrong" from a shopper, ``POST /v1/reports`` from an API user.

**The check behind a report.** Every analysis result for an account carries a ``report_token``: an HMAC over the
account, the score, a hash of exactly what was analysed and of the explanation, and an expiry. Nothing is stored
when a check runs. Only when the reporter consents does the client send the analysed request and the explanation
back with the token, which proves they are what that account was shown, so a report can't claim a score the model
never gave.

**Before the queue.** A report is about a page (:func:`~unfret.reports.scrub.page_identity`). If that page is
already in the queue, the report is closed as ``already_reported`` and counts as corroboration (raising the item's
priority); if it was labelled within ``report_known_days``, it is closed as ``already_known``. Only a new page enters
the queue, so every queued item is a different page and the first reporter is the only one who can be rewarded.
Limits: ``reports_per_month`` per account, one per page per account, and ``report_queue_cap`` open items in total.

**Handling.** A human labels the item legit, junk or scam (or rejects it). The label's zone is legit ≥ 70, junk and
scam ≤ 40; a score at least ``report_reward_gap`` points outside it pays the reporter ``report_reward_checks``
checks. The labelled page (scrubbed facts, text, score, label) is kept as training data, and the report itself,
the only link to the account, is deleted. Items nobody handled are deleted after ``report_expiry_days``.
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import asyncpg
import structlog

from unfret import result_cache
from unfret.config import Settings
from unfret.credits import ledger
from unfret.reports.scrub import page_identity, page_of
from unfret.schemas.requests import AnalyzeRequest

logger = structlog.get_logger("unfret")

Reason = Literal["real_brand", "scam", "junk", "other"]
Label = Literal["legit", "junk", "scam"]
Outcome = Literal["queued", "already_reported", "already_known"]

LEGIT_MIN = 70
UNRELIABLE_MAX = 40
_UNCERTAIN_MIDDLE = (LEGIT_MIN + UNRELIABLE_MAX) // 2


class ReportError(Exception):
    """A report that can't be accepted; ``code`` says why."""

    def __init__(self, code: str, detail: str, metadata: dict[str, Any] | None = None) -> None:
        """
        Describe the refusal.

        :param code: ``BAD_REPORT_TOKEN``, ``REPORT_LIMIT``, ``ALREADY_REPORTED_BY_YOU`` or ``QUEUE_FULL``.
        :param detail: A friendly message.
        :param metadata: Extra fields for the response.
        """
        super().__init__(detail)
        self.code, self.detail, self.metadata = code, detail, metadata


# ── Report tokens ─────────────────────────────────────────────────────────────────────────────────────────────────


def input_hash(body: AnalyzeRequest) -> str:
    """
    A hash of exactly what was analysed.

    :param body: The analysed request.
    :return: 32 hex characters.
    """
    key = result_cache.key_for(
        url=body.url,
        page_content=body.page_content or "",
        page_facts_json=body.page_facts.model_dump_json() if body.page_facts else "",
        checkout_content=body.checkout_content or "",
        flags=body.flags,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _mac(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), ("report:" + body).encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(
    secret: str,
    account: str,
    body: AnalyzeRequest,
    *,
    score: int,
    explanation: str,
    ttl_s: int,
    now: float | None = None,
) -> str | None:
    """
    The token that lets ``account`` report this result later.

    :param secret: The session secret.
    :param account: Who was shown the result.
    :param body: The analysed request.
    :param score: The score shown.
    :param explanation: The explanation shown.
    :param ttl_s: How long a report may be made.
    :param now: Current time (injectable for tests).
    :return: The token, or ``None`` for results that can't be reported (no score) or while reports are off.
    """
    if not secret or not 0 <= score <= 100:
        return None
    expires = int((time.time() if now is None else now) + ttl_s)
    payload = f"{account}|{score}|{input_hash(body)}|{_text_hash(explanation)}|{expires}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _mac(secret, payload)


@dataclass(frozen=True)
class Claim:
    """What a valid report token vouches for."""

    account: str
    score: int


def check_token(
    secret: str, token: str, *, account: str, body: AnalyzeRequest, explanation: str, now: float | None = None
) -> Claim:
    """
    Verify a report token against the reporter and what they sent back.

    :param secret: The session secret.
    :param token: The token from the result.
    :param account: The reporting account.
    :param body: The analysed request, as sent back.
    :param explanation: The explanation, as sent back.
    :param now: Current time (injectable for tests).
    :return: The vouched-for score.
    :raises ReportError: ``BAD_REPORT_TOKEN`` for a forged, expired or someone else's token, or changed data.
    """
    encoded, _, mac = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        owner, score, digest, text, expires = payload.split("|")
        valid = hmac.compare_digest(mac, _mac(secret, payload))
        fresh = int(expires) >= (time.time() if now is None else now)
    except ValueError:
        valid = fresh = False
    if not valid or owner != account:
        raise ReportError("BAD_REPORT_TOKEN", "This result can't be reported from this account.")
    if not fresh:
        raise ReportError("BAD_REPORT_TOKEN", "This check is too old to report; check the page again.")
    if digest != input_hash(body) or text != _text_hash(explanation):
        raise ReportError("BAD_REPORT_TOKEN", "The page data doesn't match the check it claims to be.")
    return Claim(owner, int(score))


# ── Submitting ────────────────────────────────────────────────────────────────────────────────────────────────────


def account_tag(secret: str, account: str) -> bytes:
    """
    A keyed hash of an account, for the per-account limits (the plain account is stored only with an open item).

    :param secret: The session secret.
    :param account: The account.
    :return: 16 bytes.
    """
    return hashlib.blake2b(
        account.encode(), key=secret.encode()[:64], digest_size=16, person=b"unfret-report"
    ).digest()


def _month(now: datetime) -> str:
    return now.strftime("%Y-%m")


@dataclass(frozen=True)
class Submitted:
    """What happened to a report."""

    outcome: Outcome
    used: int
    limit: int


async def submit(
    pool: asyncpg.Pool,
    settings: Settings,
    *,
    account: str,
    claim: Claim,
    body: AnalyzeRequest,
    reason: Reason,
    explanation: str,
    verdict: dict[str, Any] | None,
    now: datetime | None = None,
) -> Submitted:
    """
    Take a verified report: close it at once when the page is queued or known already, else queue it.

    :param pool: The database pool.
    :param settings: The service settings.
    :param account: The reporter.
    :param claim: The verified token.
    :param body: The analysed request.
    :param reason: What the reporter says the page is.
    :param explanation: The explanation they were shown.
    :param verdict: The verdict they were shown, if any.
    :param now: Current time (injectable for tests).
    :return: The outcome and the reporter's monthly count.
    :raises ReportError: ``REPORT_LIMIT``, ``ALREADY_REPORTED_BY_YOU`` or ``QUEUE_FULL``.
    """
    now = now or datetime.now(UTC)
    facts, text, url = page_of(body)
    page_key, domain = page_identity(body, facts, url, text)
    secret = settings.session_secret.get_secret_value()
    tag, month, limit = account_tag(secret, account), _month(now), settings.reports_per_month
    async with pool.acquire() as conn, conn.transaction():
        # Reports of one page take turns, so two of them can't both open an item.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", page_key)
        used = int(
            await conn.fetchval("SELECT count FROM report_quota WHERE account_tag = $1 AND month = $2", tag, month)
            or 0
        )
        if used >= limit:
            raise ReportError(
                "REPORT_LIMIT", "You've used this month's reports; thank you!", {"used": used, "limit": limit}
            )
        outcome: Outcome
        open_id = await conn.fetchval("SELECT id FROM reports WHERE page_key = $1", page_key)
        if open_id is not None:
            voted = await conn.fetchval(
                "INSERT INTO report_votes (report_id, account_tag) VALUES ($1, $2) ON CONFLICT DO NOTHING "
                "RETURNING TRUE",
                open_id,
                tag,
            )
            if not voted:
                raise ReportError("ALREADY_REPORTED_BY_YOU", "You've already reported this page.")
            await conn.execute("UPDATE reports SET corroborations = corroborations + 1 WHERE id = $1", open_id)
            outcome = "already_reported"
        elif await conn.fetchval(
            "SELECT TRUE FROM labelled_pages WHERE page_key = $1 AND labelled_at > $2 LIMIT 1",
            page_key,
            now - timedelta(days=settings.report_known_days),
        ):
            outcome = "already_known"
        else:
            if await conn.fetchval("SELECT COUNT(*) FROM reports") >= settings.report_queue_cap:
                raise ReportError("QUEUE_FULL", "Our review queue is full right now; please try again in a few days.")
            payload = {
                "url": url,
                "facts": facts.model_dump(mode="json"),
                "text": text,
                "flags": body.flags,
                "explanation": explanation[:2000],
                "verdict": verdict,
            }
            report_id = await conn.fetchval(
                "INSERT INTO reports (page_key, domain, account, reason, score, payload, created_at) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) RETURNING id",
                page_key,
                domain,
                account,
                reason,
                claim.score,
                json.dumps(payload, ensure_ascii=False),
                now,
            )
            await conn.execute("INSERT INTO report_votes (report_id, account_tag) VALUES ($1, $2)", report_id, tag)
            outcome = "queued"
        await conn.execute(
            "INSERT INTO report_quota (account_tag, month, count) VALUES ($1, $2, 1) "
            "ON CONFLICT (account_tag, month) DO UPDATE SET count = report_quota.count + 1",
            tag,
            month,
        )
    logger.info("report_received", outcome=outcome, reason=reason)
    return Submitted(outcome, used + 1, limit)


# ── Handling ──────────────────────────────────────────────────────────────────────────────────────────────────────


def gap(score: int, label: Label) -> int:
    """
    How far the score was on the wrong side of the label's zone (legit ≥ 70, junk and scam ≤ 40).

    :param score: The model's score.
    :param label: The human label.
    :return: Points outside the zone; 0 when the score was inside it.
    """
    return max(0, LEGIT_MIN - score) if label == "legit" else max(0, score - UNRELIABLE_MAX)


async def queue(pool: asyncpg.Pool, limit: int = 50) -> list[dict[str, Any]]:
    """
    The open items, most corroborated first, then those the model was least sure about, then the oldest.

    :param pool: The database pool.
    :param limit: How many.
    :return: Items without the reporter's account.
    """
    rows = await pool.fetch(
        "SELECT id, page_key, domain, reason, score, corroborations, payload, created_at FROM reports "
        "ORDER BY corroborations DESC, ABS(score - $1) ASC, created_at ASC LIMIT $2",
        _UNCERTAIN_MIDDLE,
        limit,
    )
    return [dict(r) for r in rows]


@dataclass(frozen=True)
class Handled:
    """What labelling an item did."""

    label: Label | None
    gap: int
    reward_usd_micros: int
    training: bool


async def handle(pool: asyncpg.Pool, settings: Settings, report_id: int, label: Label | None) -> Handled | None:
    """
    Label an item (or reject it with ``None``): pay the reward if earned, keep the page as training data, unlink.

    :param pool: The database pool.
    :param settings: The service settings.
    :param report_id: The item.
    :param label: The human label, or ``None`` to reject (not a product page, unusable, can't tell).
    :return: What happened, or ``None`` if the item is gone (handled or expired meanwhile).
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM reports WHERE id = $1 FOR UPDATE", report_id)
        if row is None:
            return None
        await conn.execute("DELETE FROM reports WHERE id = $1", report_id)  # the account goes with it
        if label is None:
            logger.info("report_rejected")
            return Handled(None, 0, 0, training=False)
        off = gap(row["score"], label)
        await conn.execute(
            "INSERT INTO labelled_pages (page_key, domain, label, score, reason, payload) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            row["page_key"],
            row["domain"],
            label,
            row["score"],
            row["reason"],
            row["payload"],
        )
        reward = 0
        if off >= settings.report_reward_gap:
            reward = settings.report_reward_checks * settings.analysis_price_usd_micros
            await ledger.credit(conn, row["account"], reward)
    logger.info("report_labelled", label=label, gap=off, reward_usd_micros=reward)
    return Handled(label, off, reward, training=off >= settings.report_training_gap)


async def expire(pool: asyncpg.Pool, settings: Settings, now: datetime | None = None) -> int:
    """
    Delete items nobody handled in time (and with them the account link) and past months' report counts.

    :param pool: The database pool.
    :param settings: The service settings.
    :param now: Current time (injectable for tests).
    :return: How many items expired.
    """
    now = now or datetime.now(UTC)
    status = await pool.execute(
        "DELETE FROM reports WHERE created_at < $1", now - timedelta(days=settings.report_expiry_days)
    )
    await pool.execute("DELETE FROM report_quota WHERE month < $1", _month(now))
    expired = int(status.split()[-1])
    if expired:
        logger.info("reports_expired", count=expired)
    return expired
