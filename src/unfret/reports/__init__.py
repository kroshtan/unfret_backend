"""
Reports of wrong results: "this is wrong" from a shopper, ``POST /v1/reports`` from an API user.

**The check behind a report.** Every analysis result for an account, quick or deep, carries a ``report_token``: an
HMAC over the account, the score, a hash of exactly what was analysed (a quick check's tier included) and of the
explanation, and an expiry. Nothing is stored when a check runs. A quick check without an account gets none. Only
when the reporter consents does the client send the analysed request and the explanation back with the token, which
proves they are what that account was shown, so a report can't claim a score the model never gave.

**Before the queue.** A report is about a page (:func:`~unfret.reports.scrub.page_identity`). If that page is
already in the queue, the report is closed as ``already_reported`` and counts as corroboration (raising the item's
value); if it was labelled within ``report_known_days``, it is closed as ``already_known``. Only a new page enters
the queue, so every queued item is a different page and the first reporter is the only one who can be rewarded.
Limits: ``reports_per_month`` per account and one per page per account.

**A full queue.** Every item has a :func:`value`: how far the reporter says the score is off, whether the page and
its shop are new to the labelled data, and how many others confirmed it. With ``report_queue_cap`` items open, a new
report takes the place of the item worth least if it is worth more (that item goes as if it had expired), and is
refused (``QUEUE_FULL``) otherwise. So grinding fills the queue with the reports that teach the model most, and a
burst of low-value ones can't push out the good ones.

**A second opinion.** A queued item gets one model's proposed label (:mod:`unfret.reports.second_opinion`), shown to
the operator to confirm or overrule; it never becomes a label, a training sample or a reward by itself.

**Handling.** A human labels the item legit, junk or scam (or rejects it), one by one or by confirming confident
proposals in bulk (:func:`confirm_proposals`). The label's zone is legit ≥ 70, junk and scam ≤ 40; a score at least
``report_reward_gap`` points outside it pays the first reporter ``report_reward_checks`` checks, one at least
``report_training_gap`` points outside it ``report_small_reward_checks``. The labelled page (scrubbed facts, text,
score, label) is kept as training data, and the report itself, the only link to the account, is deleted. Items
nobody handled are deleted after ``report_expiry_days``. The item
records the tier that gave the score (``payload["tier"]``); the labelled page is the same training data either way
(``make reports`` writes it to the corpus, whose pages both the deep and the quick scorer are trained on).
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
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

Reason = Literal["real_brand", "scam", "junk", "other"]
Label = Literal["legit", "junk", "scam"]
Outcome = Literal["queued", "already_reported", "already_known"]
Agreement = Literal["reporter", "score", "neither", "unsure", "none"]
Order = Literal["value", "confidence"]

LEGIT_MIN = 70
UNRELIABLE_MAX = 40
# What the reporter says the page is, as a label (``other``: something else, no label).
REASON_LABEL: dict[str, Label] = {"real_brand": "legit", "scam": "scam", "junk": "junk"}
# The value of an item (:func:`value`): bonuses for a page and a shop new to the labelled data, and per confirmation.
NEW_PAGE_VALUE = 15
NEW_DOMAIN_VALUE = 15
CORROBORATION_VALUE = 10
MAX_VALUED_CORROBORATIONS = 3


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
    :return: 32 hex characters. A quick check's differs from a deep check's of the same page (a deep check's is as
        it always was), so a token can't pass a quick score off as a deep one.
    """
    key = result_cache.key_for(
        url=body.url,
        page_content=body.page_content or "",
        page_facts_json=body.page_facts.model_dump_json() if body.page_facts else "",
        checkout_content=body.checkout_content or "",
        tier="quick" if body.tier == "quick" else "full",
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
    report_id: int | None = None  # the new item, when queued


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

    A report of the test shop (:mod:`unfret.test_shop`) is closed as ``already_known`` without being stored or
    counted: its tricks are deliberate, and it must never become training data.

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
    secret = settings.session_secret.get_secret_value()
    tag, month, limit = account_tag(secret, account), _month(now), settings.reports_per_month
    if is_test_shop(body.url):
        used = await pool.fetchval("SELECT count FROM report_quota WHERE account_tag = $1 AND month = $2", tag, month)
        logger.info("report_received", outcome="already_known", reason=reason)
        return Submitted("already_known", int(used or 0), limit)
    facts, text, url = page_of(body)
    page_key, domain = page_identity(body, facts, url, text)
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
            await conn.execute(
                "UPDATE reports SET corroborations = corroborations + 1, "
                "value = value + CASE WHEN corroborations < $2 THEN $3 ELSE 0 END WHERE id = $1",
                open_id,
                MAX_VALUED_CORROBORATIONS,
                CORROBORATION_VALUE,
            )
            outcome = "already_reported"
        elif await conn.fetchval(
            "SELECT TRUE FROM labelled_pages WHERE page_key = $1 AND labelled_at > $2 LIMIT 1",
            page_key,
            now - timedelta(days=settings.report_known_days),
        ):
            outcome = "already_known"
        else:
            worth = value(
                claim.score,
                reason,
                new_page=not await conn.fetchval(
                    "SELECT TRUE FROM labelled_pages WHERE page_key = $1 LIMIT 1", page_key
                ),
                new_domain=not await conn.fetchval(
                    "SELECT TRUE FROM labelled_pages WHERE domain = $1 UNION ALL "
                    "SELECT TRUE FROM reports WHERE domain = $1 LIMIT 1",
                    domain,
                ),
            )
            await _make_room(conn, settings, worth)
            payload = {
                "url": url,
                "facts": facts.model_dump(mode="json"),
                "text": text,
                "flags": body.flags,
                "explanation": explanation[:2000],
                "verdict": verdict,
                "tier": body.tier,  # which check gave the score: quick (rules only) or deep
            }
            report_id = await conn.fetchval(
                "INSERT INTO reports (page_key, domain, account, reason, score, payload, created_at, value) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) RETURNING id",
                page_key,
                domain,
                account,
                reason,
                claim.score,
                json.dumps(payload, ensure_ascii=False),
                now,
                worth,
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
    return Submitted(outcome, used + 1, limit, report_id if outcome == "queued" else None)


def claimed_gap(score: int, reason: Reason) -> int:
    """
    How far the score is off if the reporter is right: the :func:`gap` to the label their reason stands for.

    :param score: The model's score.
    :param reason: What the reporter says the page is.
    :return: Points; 0 for ``other`` (no label) or a score already in that label's zone.
    """
    label = REASON_LABEL.get(reason)
    return gap(score, label) if label else 0


def value(score: int, reason: Reason, *, new_page: bool, new_domain: bool, corroborations: int = 0) -> int:
    """
    What a queued report is worth as a training sample, to choose which item a full queue gives up.

    The larger the disagreement the reporter claims, the more the model learns if they are right (and a
    claim that large is quick to judge); a page, and a shop, that the labelled data doesn't have yet adds more than
    another page of a known one; each confirmation by another account makes the report likelier to be right.

    :param score: The model's score.
    :param reason: What the reporter says the page is.
    :param new_page: Whether the page was never labelled.
    :param new_domain: Whether no page of its shop (domain) was labelled or is queued.
    :param corroborations: Confirmations so far.
    :return: The value (0-130).
    """
    return (
        claimed_gap(score, reason)
        + NEW_PAGE_VALUE * new_page
        + NEW_DOMAIN_VALUE * new_domain
        + CORROBORATION_VALUE * min(corroborations, MAX_VALUED_CORROBORATIONS)
    )


async def _make_room(conn: asyncpg.Connection, settings: Settings, worth: int) -> None:
    """
    With the queue full, give up the item worth least for a new one worth more, or refuse the new one.

    :param conn: The connection, in the report's transaction.
    :param settings: The service settings.
    :param worth: The new report's value.
    :raises ReportError: ``QUEUE_FULL`` when every open item is worth at least as much.
    """
    if await conn.fetchval("SELECT COUNT(*) FROM reports") < settings.report_queue_cap:
        return
    least = await conn.fetchrow(
        "SELECT id, value FROM reports ORDER BY value ASC, created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
    )
    if least is None or least["value"] >= worth:
        raise ReportError("QUEUE_FULL", "Our review queue is full right now; please try again in a few days.")
    await conn.execute("DELETE FROM reports WHERE id = $1", least["id"])  # its account link goes with it
    logger.info("report_replaced", value=least["value"], by_value=worth)


# ── Handling ──────────────────────────────────────────────────────────────────────────────────────────────────────


def gap(score: int, label: Label) -> int:
    """
    How far the score was on the wrong side of the label's zone (legit ≥ 70, junk and scam ≤ 40).

    :param score: The model's score.
    :param label: The human label.
    :return: Points outside the zone; 0 when the score was inside it.
    """
    return max(0, LEGIT_MIN - score) if label == "legit" else max(0, score - UNRELIABLE_MAX)


def agreement(score: int, reason: Reason, opinion: Label | Literal["unsure"] | None, training_gap: int) -> Agreement:
    """
    What a second opinion sides with: the reporter, the original score, neither, or nothing yet.

    :param score: The model's score.
    :param reason: What the reporter says the page is.
    :param opinion: The proposed label (``unsure``, or ``None`` without an opinion).
    :param training_gap: How far off a score must be to count as wrong (``report_training_gap``).
    :return: ``score`` (the result stands: the score is within ``training_gap`` of the proposal's zone),
        ``reporter`` (the score was wrong, on the side the reporter says), ``neither`` (wrong, but not as the
        reporter says), ``unsure`` or ``none``.
    """
    if opinion is None:
        return "none"
    if opinion == "unsure":
        return "unsure"
    if gap(score, opinion) < training_gap:
        return "score"
    claimed = REASON_LABEL.get(reason)
    same_side = claimed is not None and (claimed == "legit") == (opinion == "legit")
    return "reporter" if same_side else "neither"


def reward_checks(settings: Settings, off: int) -> int:
    """
    The checks a label pays the first reporter, by how far the score was off.

    :param settings: The service settings.
    :param off: The :func:`gap` between the score and the label.
    :return: ``report_reward_checks``, ``report_small_reward_checks`` or 0.
    """
    if off >= settings.report_reward_gap:
        return settings.report_reward_checks
    if off >= settings.report_training_gap:
        return settings.report_small_reward_checks
    return 0


async def queue(
    pool: asyncpg.Pool,
    settings: Settings,
    limit: int = 50,
    *,
    order: Order = "value",
    agreement_is: Agreement | None = None,
    min_confidence: float | None = None,
) -> list[dict[str, Any]]:
    """
    The open items with their second opinions, without the reporters' accounts.

    :param pool: The database pool.
    :param settings: The service settings.
    :param limit: How many.
    :param order: ``value`` (worth most first, then oldest) or ``confidence`` (the most confident proposals first).
    :param agreement_is: Only items whose second opinion sides like this (:func:`agreement`).
    :param min_confidence: Only items with a proposed label (not unsure) at least this confident.
    :return: The items, each with ``second_opinion`` (``state``, ``label``, ``confidence``, ``reason``, ``model``,
        ``agreement``, and the ``gap`` and ``reward_checks`` that confirming the proposal would mean).
    """
    rows = await pool.fetch(
        "SELECT id, page_key, domain, reason, score, corroborations, value, created_at, opinion_state, llm_label, "
        "llm_confidence, llm_reason, llm_model FROM reports ORDER BY value DESC, created_at ASC, id ASC"
    )
    items = []
    for row in rows:
        item = dict(row)
        label = item.pop("llm_label")
        sides = agreement(item["score"], item["reason"], label, settings.report_training_gap)
        confidence = item.pop("llm_confidence")
        off = gap(item["score"], label) if label in REASON_LABEL.values() else None
        item["second_opinion"] = {
            "state": item.pop("opinion_state"),
            "label": label,
            "confidence": None if confidence is None else round(confidence, 3),
            "reason": item.pop("llm_reason"),
            "model": item.pop("llm_model"),
            "agreement": sides,
            "gap": off,
            "reward_checks": None if off is None else reward_checks(settings, off),
        }
        if agreement_is is not None and sides != agreement_is:
            continue
        if min_confidence is not None and (label in {None, "unsure"} or (confidence or 0) < min_confidence):
            continue
        items.append(item)
    if order == "confidence":
        items.sort(key=lambda i: -(i["second_opinion"]["confidence"] or -1))  # stable: by value within a tie
    items = items[:limit]
    payloads = {
        r["id"]: r["payload"]
        for r in await pool.fetch("SELECT id, payload FROM reports WHERE id = ANY($1)", [i["id"] for i in items])
    }
    for item in items:
        item["payload"] = payloads.get(item["id"], "{}")
    return items


@dataclass(frozen=True)
class Handled:
    """What labelling an item did."""

    label: Label | None
    gap: int
    reward_usd_micros: int
    training: bool
    reward_checks: int = 0


async def _label(
    conn: asyncpg.Connection, settings: Settings, row: asyncpg.Record, label: Label | None, *, bulk: bool
) -> Handled:
    """
    Apply a person's label to a locked item: keep the page as training data, pay the first reporter, unlink.

    :param conn: The connection, in a transaction holding the row.
    :param settings: The service settings.
    :param row: The item.
    :param label: The label, or ``None`` to reject.
    :param bulk: Whether the label is a second opinion the operator confirmed in bulk.
    :return: What happened.
    """
    await conn.execute("DELETE FROM reports WHERE id = $1", row["id"])  # the account goes with it
    if label is None:
        logger.info("report_rejected")
        return Handled(None, 0, 0, training=False)
    off = gap(row["score"], label)
    await conn.execute(
        "INSERT INTO labelled_pages (page_key, domain, label, score, reason, payload, llm_label, llm_confidence, "
        "confirmed_in_bulk) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        row["page_key"],
        row["domain"],
        label,
        row["score"],
        row["reason"],
        row["payload"],
        row["llm_label"],
        row["llm_confidence"],
        bulk,
    )
    checks = reward_checks(settings, off)
    reward = checks * settings.analysis_price_usd_micros
    if reward:
        await ledger.credit(conn, row["account"], reward)
    logger.info("report_labelled", label=label, gap=off, reward_checks=checks, bulk=bulk)
    return Handled(label, off, reward, training=off >= settings.report_training_gap, reward_checks=checks)


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
        return await _label(conn, settings, row, label, bulk=False)


ConfirmOutcome = Literal["labelled", "gone", "changed"]


async def confirm_proposals(
    pool: asyncpg.Pool, settings: Settings, confirmed: list[tuple[int, Label]], min_confidence: float
) -> list[tuple[int, ConfirmOutcome, Handled | None]]:
    """
    Label items with the second opinions the operator previewed and confirmed.

    Each item must still carry exactly the proposal confirmed: a done second opinion with that label and at least
    ``min_confidence``. The label is then the operator's, as if typed one by one (``confirmed_in_bulk`` notes how).

    :param pool: The database pool.
    :param settings: The service settings.
    :param confirmed: ``(id, label)`` pairs as previewed.
    :param min_confidence: The confidence the preview was filtered by.
    :return: Per item: ``labelled`` (with what it did), ``gone`` (handled or expired) or ``changed`` (the item
        doesn't carry that proposal: left for one-by-one review).
    """
    results: list[tuple[int, ConfirmOutcome, Handled | None]] = []
    for report_id, label in confirmed:
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM reports WHERE id = $1 FOR UPDATE", report_id)
            if row is None:
                results.append((report_id, "gone", None))
            elif (
                row["opinion_state"] != "done"
                or row["llm_label"] != label
                or (row["llm_confidence"] or 0) < min_confidence
            ):
                results.append((report_id, "changed", None))
            else:
                results.append((report_id, "labelled", await _label(conn, settings, row, label, bulk=True)))
    return results


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
    await pool.execute("DELETE FROM report_opinion_days WHERE day < $1", now.date() - timedelta(days=7))
    expired = int(status.split()[-1])
    if expired:
        logger.info("reports_expired", count=expired)
    return expired
