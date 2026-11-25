"""
The labels in "Wrong result?" reports: levels, wrong and missing findings, contributors, settling, the leaderboard.

The level a shopper thinks is right and the findings that are wrong or missing; the report's page itself is
:mod:`unfret.reports`.

**The result behind a report.** Every analysis result carries a ``label_token``: an HMAC over a hash of exactly what
was analysed, the tier, the score and level shown, the reason types behind it, and an expiry. Nothing is stored when
a check runs. A report sends the token back with the analysed request; the token proves the result and the page are
the ones Unfret gave and read, so nobody can report a result Unfret never gave.

**Contributors.** A report may carry a contributor key: 32 random bytes made on the device, sent only with reports
and profile calls, never with a check. Unfret keeps a keyed hash of it (no account, no e-mail), points (labels that
agreed with the settled answer), a reputation, and optionally a pseudonymous handle shown on the public leaderboard
(opt-in). Without a key a report is anonymous: its label weighs little (``crowd_anonymous_weight``) and counts only
where another agrees.

**Anti-poisoning** (every report, whichever endpoint it came through).

- Every report needs a token from a real result, which needs a real check (rate-limited and proof-of-work-gated where
  the check is), and an anonymous token is good for one report.
- One label per page per contributor (a new one replaces the old); ``crowd_key_daily_labels`` per key per day; at
  most ``crowd_anonymous_daily_cap`` anonymous reports a day in all.
- A new key is on probation (``crowd_new_key_weight``) until ``crowd_probation_settled`` of its labels agreed with a
  settled answer; its reputation then rises with agreement and falls faster with disagreement; a key below
  ``crowd_mute_reputation`` still gets an answer but its labels weigh nothing (it isn't told).
- A page's answer settles (:func:`settle`) only with enough weight (``crowd_settle_weight``), a two-thirds majority,
  two independent labels, and at least one contributor past probation, or when a reviewer labels the page
  (:func:`settle_by_person`). Anonymous labels alone never settle a page.
- A burst (more than ``crowd_burst_labels`` labels on one page in a day) that pushes the level down is held for a
  person instead of settling: the typical poisoning is a seller calling their own page fine.
- Reports never change a result. Settled answers are training data and the review queue's evidence, nothing more.

**Privacy.** Never who sent a report: no IP address, no account, no time (only the day). The link between a
contributor and a page is dropped once the page settles, or after ``crowd_link_days``; the contributor keeps only
counts. ``POST /v1/contributors/forget`` deletes a contributor.
"""

import base64
import hashlib
import hmac
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg
import structlog

from unfret.config import Settings

logger = structlog.get_logger("unfret")

Status = Literal["recorded", "updated"]
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # 32 bytes, base64url without padding
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")
RESERVED_HANDLES = re.compile(r"unfret|admin|moderator|official|scam|fraud", re.IGNORECASE)
MAX_TYPES = 40
TOKEN_VERSION = "v3"  # noqa: S105 - a format version; v3: binds what was analysed (the report sends the page)


class LabelError(Exception):
    """A label or profile call that can't be accepted; ``code`` says why."""

    def __init__(self, code: str, detail: str) -> None:
        """
        Describe the refusal.

        :param code: ``BAD_LABEL_TOKEN``, ``BAD_LABEL``, ``BAD_CONTRIBUTOR_KEY``, ``BAD_HANDLE``, ``HANDLE_TAKEN``,
            ``ALREADY_LABELLED``, ``LABEL_LIMIT`` or ``LABELS_PAUSED``.
        :param detail: A friendly message.
        """
        super().__init__(detail)
        self.code, self.detail = code, detail


# ── Tokens ────────────────────────────────────────────────────────────────────────────────────────────────────────


def _mac(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), ("label:" + body).encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(
    secret: str,
    input_hash: str,
    *,
    tier: str,
    score: int,
    level: int | None,
    types: list[str],
    ttl_s: int,
    now: float | None = None,
) -> str | None:
    """
    The token that lets whoever saw this result report it as wrong.

    :param secret: The session secret.
    :param input_hash: :func:`unfret.reports.input_hash` of exactly what was analysed: the report must send that back.
    :param tier: quick or deep.
    :param score: The score shown.
    :param level: The level shown (0–4), or ``None`` for a result without one.
    :param types: The reason types behind it (``risk.because`` and the cards).
    :param ttl_s: How long it may be used.
    :param now: Current time (injectable for tests).
    :return: The token, or ``None`` without a secret or for a result with no score.
    """
    if not secret or not 0 <= score <= 100:
        return None
    expires = int((time.time() if now is None else now) + ttl_s)
    shown = ",".join(dict.fromkeys(t for t in types if re.fullmatch(r"[a-z0-9_]{1,40}", t)))
    payload = f"{TOKEN_VERSION}|{input_hash}|{tier}|{score}|{'' if level is None else level}|{shown}|{expires}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _mac(secret, payload)


@dataclass(frozen=True)
class Shown:
    """What a result said, as its token (or a report token) vouches for it."""

    input_hash: str
    tier: str
    score: int
    level: int | None
    types: tuple[str, ...]
    token_hash: bytes


def check_token(secret: str, token: str, now: float | None = None) -> Shown:
    """
    Verify a label token.

    :param secret: The session secret.
    :param token: The token from the result.
    :param now: Current time (injectable for tests).
    :return: What it vouches for.
    :raises LabelError: ``BAD_LABEL_TOKEN`` for a forged, expired or older-format one.
    """
    encoded, _, mac = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        version, digest, tier, score, level, shown, expires = payload.split("|")
        valid = bool(secret) and version == TOKEN_VERSION and hmac.compare_digest(mac, _mac(secret, payload))
        fresh = int(expires) >= (time.time() if now is None else now)
    except ValueError:
        valid = fresh = False
    if not valid:
        raise LabelError("BAD_LABEL_TOKEN", "This result can't be reported; check the page again.")
    if not fresh:
        raise LabelError("BAD_LABEL_TOKEN", "This check is too old to report; check the page again.")
    types = tuple(t for t in shown.split(",") if t)
    return Shown(digest, tier, int(score), int(level) if level else None, types, token_hash(token))


def token_hash(token: str) -> bytes:
    """
    What is kept of a spent token, so it can't report twice.

    :param token: A label or report token.
    :return: 16 bytes.
    """
    return hashlib.sha256(token.encode()).digest()[:16]


# ── Contributors ──────────────────────────────────────────────────────────────────────────────────────────────────


def key_hash(secret: str, key: str) -> bytes:
    """
    The stored form of a contributor key.

    :param secret: The session secret.
    :param key: The key, as the device sends it.
    :return: 16 bytes.
    :raises LabelError: ``BAD_CONTRIBUTOR_KEY`` when it isn't 32 bytes in base64url.
    """
    if not KEY_RE.fullmatch(key):
        raise LabelError(
            "BAD_CONTRIBUTOR_KEY", "A contributor key is 32 random bytes in base64url, made on the device."
        )
    return hashlib.blake2b(key.encode(), key=secret.encode()[:64], digest_size=16, person=b"unfret-contrib").digest()


def weight(settings: Settings, row: asyncpg.Record | dict[str, Any] | None) -> float:
    """
    How much a label counts.

    :param settings: The settings.
    :param row: The contributor (``reputation``, ``settled``), or ``None`` for an anonymous label.
    :return: The weight (0 for a muted key).
    """
    if row is None:
        return settings.crowd_anonymous_weight
    reputation = float(row["reputation"])
    if reputation < settings.crowd_mute_reputation:
        return 0.0
    base = settings.crowd_new_key_weight if int(row["settled"]) < settings.crowd_probation_settled else 1.0
    return round(base * min(reputation, settings.crowd_max_reputation), 4)


def _today() -> Any:
    return datetime.now(UTC).date()


async def _contributor(conn: asyncpg.Connection, digest: bytes) -> asyncpg.Record:
    await conn.execute("INSERT INTO contributors (key_hash) VALUES ($1) ON CONFLICT (key_hash) DO NOTHING", digest)
    return await conn.fetchrow("SELECT * FROM contributors WHERE key_hash = $1 FOR UPDATE", digest)


async def record(
    conn: asyncpg.Connection,
    settings: Settings,
    shown: Shown,
    *,
    page_key: str,
    level: int | None,
    wrong: list[str],
    missing: list[str],
    contributor_key: str | None,
    comment: str | None = None,
) -> tuple[Status, float]:
    """
    Record one report's label in the caller's transaction (see the module docstring for the limits).

    :param conn: The connection, in a transaction.
    :param settings: The settings.
    :param shown: What the result said.
    :param page_key: The page (:func:`unfret.reports.scrub.page_identity`).
    :param level: The page's overall level (0–4) as the reporter sees it, or ``None``.
    :param wrong: Reason types of the result the reporter says are wrong.
    :param missing: Reason types the reporter says the result should have found.
    :param contributor_key: The device's contributor key, or ``None`` (anonymous).
    :param comment: The shopper's own words, scrubbed already.
    :return: recorded or updated (this contributor had reported the page before), and the label's weight.
    :raises LabelError: See :class:`LabelError`.
    """
    secret = settings.session_secret.get_secret_value()
    digest = key_hash(secret, contributor_key) if contributor_key else None
    today = _today()
    if digest is None:
        anonymous = await conn.fetchval(
            "SELECT COUNT(*) FROM report_labels WHERE anonymous AND created_day = $1",
            today,
        )
        if anonymous >= settings.crowd_anonymous_daily_cap:
            raise LabelError("LABELS_PAUSED", "Unfret takes no more anonymous reports today; thank you.")
        used = await conn.fetchval(
            "INSERT INTO report_tokens_used (token_hash, expires_on) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            shown.token_hash,
            datetime.fromtimestamp(time.time() + settings.crowd_label_token_ttl_s, UTC).date(),
        )
        if used is None:
            raise LabelError("ALREADY_LABELLED", "This result was reported already.")
        w = weight(settings, None)
        await _insert(conn, shown, page_key, level, wrong, missing, None, w, comment)
        return "recorded", w
    row = await _contributor(conn, digest)
    count = int(row["day_count"]) if row["last_label_day"] == today else 0
    if count >= settings.crowd_key_daily_labels:
        raise LabelError("LABEL_LIMIT", "That's the most reports for one day; thank you, more tomorrow.")
    await conn.execute(
        "UPDATE contributors SET last_label_day = $2, day_count = $3, labels = labels + 1 WHERE id = $1",
        row["id"],
        today,
        count + 1,
    )
    replaced = await conn.fetchval(
        "DELETE FROM report_labels WHERE page_key = $1 AND contributor_id = $2 AND NOT settled RETURNING 1",
        page_key,
        row["id"],
    )
    w = weight(settings, row)
    await _insert(conn, shown, page_key, level, wrong, missing, row["id"], w, comment)
    return ("updated" if replaced else "recorded"), w


async def _insert(  # noqa: PLR0917 - one row's columns
    conn: asyncpg.Connection,
    shown: Shown,
    page_key: str,
    level: int | None,
    wrong: list[str],
    missing: list[str],
    contributor: int | None,
    w: float,
    comment: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO report_labels (page_key, contributor_id, anonymous, tier, shown_level, shown_types, level, "
        "wrong, missing, weight, created_day, comment) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
        page_key,
        contributor,
        contributor is None,
        shown.tier,
        shown.level,
        list(shown.types)[:MAX_TYPES],
        level,
        wrong,
        missing,
        w,
        _today(),
        comment,
    )


def valid_handle(handle: str) -> str:
    """
    A leaderboard handle: 3–24 letters, digits, ``-`` or ``_``, not posing as Unfret or naming anyone a scam.

    :param handle: The handle asked for.
    :return: The handle.
    :raises LabelError: ``BAD_HANDLE``.
    """
    if not HANDLE_RE.fullmatch(handle) or RESERVED_HANDLES.search(handle):
        raise LabelError("BAD_HANDLE", "A handle is 3-24 letters, digits, - or _ (and can't contain some words).")
    return handle


async def profile(
    pool: asyncpg.Pool,
    settings: Settings,
    key: str,
    *,
    handle: str | None = None,
    public: bool | None = None,
) -> dict[str, Any]:
    """
    Make or update a contributor's profile, and read it back.

    :param pool: The database pool.
    :param settings: The settings.
    :param key: The contributor key.
    :param handle: A new handle (``""`` removes it), or ``None`` to keep it.
    :param public: Show on the leaderboard, or ``None`` to keep the setting. Needs a handle.
    :return: The profile: handle, public, labels, settled, agreed, reputation, rank (public only).
    :raises LabelError: ``BAD_HANDLE``, ``HANDLE_TAKEN``.
    """
    digest = key_hash(settings.session_secret.get_secret_value(), key)
    async with pool.acquire() as conn, conn.transaction():
        row = await _contributor(conn, digest)
        new_handle = row["handle"] if handle is None else (valid_handle(handle) if handle else None)
        new_public = bool(row["public"]) if public is None else public
        if new_public and not new_handle:
            raise LabelError("BAD_HANDLE", "Pick a handle to show on the leaderboard.")
        try:
            row = await conn.fetchrow(
                "UPDATE contributors SET handle = $2, public = $3 WHERE id = $1 RETURNING *",
                row["id"],
                new_handle,
                new_public,
            )
        except asyncpg.UniqueViolationError as exc:
            raise LabelError("HANDLE_TAKEN", "That handle is taken.") from exc
        rank = (
            await conn.fetchval(
                "SELECT COUNT(*) + 1 FROM contributors WHERE public AND agreed > $1 AND settled >= $2",
                row["agreed"],
                settings.crowd_probation_settled,
            )
            if row["public"] and row["settled"] >= settings.crowd_probation_settled
            else None
        )
    return {
        "handle": row["handle"],
        "public": row["public"],
        "labels": row["labels"],
        "settled": row["settled"],
        "agreed": row["agreed"],
        "points": row["agreed"],
        "trusted": row["settled"] >= settings.crowd_probation_settled,
        "rank": rank,
    }


async def forget(pool: asyncpg.Pool, settings: Settings, key: str) -> bool:
    """
    Delete a contributor (their labels stay, unlinked, as anonymous ones that no longer count).

    :param pool: The database pool.
    :param settings: The settings.
    :param key: The contributor key.
    :return: Whether there was one.
    """
    digest = key_hash(settings.session_secret.get_secret_value(), key)
    async with pool.acquire() as conn, conn.transaction():
        cid = await conn.fetchval("SELECT id FROM contributors WHERE key_hash = $1", digest)
        if cid is None:
            return False
        await conn.execute("UPDATE report_labels SET weight = 0 WHERE contributor_id = $1 AND NOT settled", cid)
        await conn.execute("DELETE FROM contributors WHERE id = $1", cid)
    return True


async def leaderboard(pool: asyncpg.Pool, settings: Settings, limit: int = 50) -> list[dict[str, Any]]:
    """
    The public leaderboard: contributors who opted in and are past probation, by agreed labels.

    :param pool: The database pool.
    :param settings: The settings.
    :param limit: How many (at most 100).
    :return: Rank, handle, agreed and settled labels.
    """
    rows = await pool.fetch(
        "SELECT handle, agreed, settled FROM contributors WHERE public AND handle IS NOT NULL AND settled >= $1 "
        "ORDER BY agreed DESC, settled ASC, handle LIMIT $2",
        settings.crowd_probation_settled,
        min(max(limit, 1), 100),
    )
    return [{"rank": i, **dict(r)} for i, r in enumerate(rows, start=1)]


# ── Settling ──────────────────────────────────────────────────────────────────────────────────────────────────────


@dataclass
class Vote:
    """One label, as settling sees it."""

    contributor: int | None
    level: int | None
    wrong: tuple[str, ...]
    weight: float
    trusted: bool
    day: Any


def decide(votes: list[Vote], settings: Settings, shown_level: int | None) -> tuple[int | None, bool]:
    """
    A page's settled level from its labels, or none yet (see the module docstring).

    :param votes: The page's open labels with a level.
    :param settings: The settings.
    :param shown_level: The level the results showed (most recent label's).
    :return: ``(level or None, held)``: held when a burst pushes the level down.
    """
    by_level: dict[int, list[Vote]] = {}
    for v in votes:
        if v.level is not None:
            by_level.setdefault(v.level, []).append(v)
    weights: dict[int, float] = {}
    for lv, vs in by_level.items():
        # Anonymous labels count only where another label agrees.
        weights[lv] = sum(v.weight for v in vs if v.contributor is not None or len(vs) >= 2)
    total = sum(weights.values())
    if not total:
        return None, False
    level = max(weights, key=lambda k: weights[k])
    winners = by_level[level]
    settles = (
        weights[level] >= settings.crowd_settle_weight
        and weights[level] >= 2 * total / 3
        and len(winners) >= 2
        and any(v.contributor is not None and v.trusted for v in winners)
    )
    if not settles:
        return None, False
    burst = max(sum(1 for v in votes if v.day == d) for d in {v.day for v in votes})
    milder = shown_level is not None and level < shown_level  # higher is worse
    return level, burst > settings.crowd_burst_labels and milder


async def _votes(
    conn: asyncpg.Connection, settings: Settings, page_key: str
) -> tuple[list[asyncpg.Record], list[Vote]]:
    rows = await conn.fetch(
        "SELECT l.*, c.settled AS c_settled FROM report_labels l LEFT JOIN contributors c ON c.id = "
        "l.contributor_id WHERE l.page_key = $1 AND NOT l.settled ORDER BY l.id FOR UPDATE OF l",
        page_key,
    )
    votes = [
        Vote(
            r["contributor_id"],
            r["level"],
            tuple(r["wrong"]),
            float(r["weight"]),
            (r["c_settled"] or 0) >= settings.crowd_probation_settled,
            r["created_day"],
        )
        for r in rows
    ]
    return rows, votes


async def _close(  # noqa: PLR0917 - one answer's parts
    conn: asyncpg.Connection,
    settings: Settings,
    page_key: str,
    rows: list[asyncpg.Record],
    votes: list[Vote],
    level: int,
    *,
    held: bool,
    by_person: bool,
) -> None:
    """
    Settle a page's open labels on ``level``: the answer, the reputations (not for a held answer), unlink.

    :param conn: The connection, in a transaction holding the rows.
    :param settings: The settings.
    :param page_key: The page.
    :param rows: Its open labels.
    :param votes: The same, as votes.
    :param level: The settled level.
    :param held: A burst that lowers the level: kept for a person, no reputations move.
    :param by_person: The level is a reviewer's, not the labels' agreement.
    """
    labellers = [v for v in votes if v.level is not None]
    # The findings at least two thirds of the labellers said were wrong.
    said = Counter(t for v in labellers for t in v.wrong)
    wrong = sorted(t for t, n in said.items() if labellers and 3 * n >= 2 * len(labellers))
    shown = rows[-1]["shown_level"] if rows else None
    await conn.execute(
        "INSERT INTO report_answers (page_key, level, shown_level, tier, wrong, labels, weight, held, by_person, "
        "settled_on) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        page_key,
        level,
        shown,
        rows[-1]["tier"] if rows else "deep",
        wrong,
        len(labellers),
        sum(v.weight for v in labellers if v.level == level),
        held,
        by_person,
        _today(),
    )
    if not held:
        for v in labellers:
            if v.contributor is None:
                continue
            agree = v.level == level
            await conn.execute(
                "UPDATE contributors SET settled = settled + 1, agreed = agreed + $2::int, "
                "reputation = LEAST($3, GREATEST(0, reputation + $4)) WHERE id = $1",
                v.contributor,
                1 if agree else 0,
                settings.crowd_max_reputation,
                settings.crowd_reputation_up if agree else -settings.crowd_reputation_down,
            )
    # Settled: the contributor link goes, the label stays as data.
    await conn.execute(
        "UPDATE report_labels SET settled = TRUE, contributor_id = NULL WHERE page_key = $1 AND NOT settled", page_key
    )


async def settle_by_person(conn: asyncpg.Connection, settings: Settings, page_key: str, level: int) -> None:
    """
    A reviewer labelled the page: its open labels settle on that level, and reputations move.

    :param conn: The connection, in a transaction.
    :param settings: The settings.
    :param page_key: The page.
    :param level: The reviewer's level.
    """
    rows, votes = await _votes(conn, settings, page_key)
    if rows:
        await _close(conn, settings, page_key, rows, votes, level, held=False, by_person=True)


async def settle(pool: asyncpg.Pool, settings: Settings) -> int:
    """
    Settle the pages whose labels agree enough, update reputations, drop old links (housekeeping).

    :param pool: The database pool.
    :param settings: The settings.
    :return: Pages settled.
    """
    settled = 0
    pages = await pool.fetch("SELECT DISTINCT page_key FROM report_labels WHERE NOT settled AND level IS NOT NULL")
    for page in pages:
        async with pool.acquire() as conn, conn.transaction():
            rows, votes = await _votes(conn, settings, page["page_key"])
            if not rows:
                continue
            level, held = decide(votes, settings, rows[-1]["shown_level"])
            if level is None:
                continue
            await _close(conn, settings, page["page_key"], rows, votes, level, held=held, by_person=False)
            settled += 1
    return settled


async def purge(pool: asyncpg.Pool, settings: Settings) -> None:
    """
    Settle what can be, unlink old labels from their contributors, delete spent tokens and stale labels.

    :param pool: The database pool.
    :param settings: The settings.
    """
    await settle(pool, settings)
    await pool.execute(
        "UPDATE report_labels SET contributor_id = NULL WHERE contributor_id IS NOT NULL "
        "AND created_day < CURRENT_DATE - $1::int",
        settings.crowd_link_days,
    )
    await pool.execute("DELETE FROM report_tokens_used WHERE expires_on < CURRENT_DATE")
    await pool.execute(
        "DELETE FROM report_labels WHERE NOT settled AND created_day < CURRENT_DATE - $1::int",
        settings.crowd_open_days,
    )


async def summary(pool: asyncpg.Pool) -> dict[str, Any]:
    """
    For the operator: settled answers against the level shown, held pages, and which findings people disagree with.

    :param pool: The database pool.
    :return: The numbers.
    """
    answers = await pool.fetch(
        "SELECT shown_level, level, held, by_person, COUNT(*) AS n FROM report_answers "
        "GROUP BY 1, 2, 3, 4 ORDER BY 1, 2"
    )
    wrong = await pool.fetch(
        "SELECT t AS type, COUNT(*) AS wrong FROM report_labels, UNNEST(wrong) AS t GROUP BY t ORDER BY 2 DESC"
    )
    missing = await pool.fetch(
        "SELECT t AS type, COUNT(*) AS missing FROM report_labels, UNNEST(missing) AS t GROUP BY t ORDER BY 2 DESC"
    )
    shown = await pool.fetch(
        "SELECT t AS type, COUNT(*) AS shown FROM report_labels, UNNEST(shown_types) AS t GROUP BY t"
    )
    shown_by = {r["type"]: r["shown"] for r in shown}
    return {
        "labels": await pool.fetchval("SELECT COUNT(*) FROM report_labels"),
        "contributors": await pool.fetchval("SELECT COUNT(*) FROM contributors"),
        "answers": [dict(r) for r in answers],
        "findings": [{"type": r["type"], "wrong": r["wrong"], "shown": shown_by.get(r["type"], 0)} for r in wrong],
        "missing": [dict(r) for r in missing],
    }
