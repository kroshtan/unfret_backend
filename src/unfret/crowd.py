"""
Labels from the product: a shopper disagrees with a finding, or gives the page's overall risk level.

**The result behind a label.** Every analysis result carries a ``label_token``: an HMAC over a keyed hash of the page
(:func:`page_key`, never the address), the tier, the level shown and the reason types behind it, and an expiry.
Nothing is stored when a check runs. A label (``POST /v1/labels``) sends the token back with the level and the reason
types the shopper disagrees with; the token proves what the result said, so nobody can label a result Unfret never
gave.

**Contributors.** A label may carry a contributor key: 32 random bytes made on the device, sent only with labels
and profile calls, never with a check. Unfret keeps a keyed hash of it (no account, no e-mail), the counts and a
reputation, and optionally a pseudonymous handle shown on the public leaderboard (opt-in). Without a key a label is
anonymous: it weighs little (``crowd_anonymous_weight``) and counts only where another label agrees.

**Anti-poisoning.**

- Every label needs a token from a real result, which needs a real check (rate-limited and proof-of-work-gated where
  the check is), and an anonymous token is good for one label.
- One label per page per contributor (a new one replaces the old); ``crowd_key_daily_labels`` per key per day; at
  most ``crowd_anonymous_daily_cap`` anonymous labels a day in all.
- A new key is on probation (``crowd_new_key_weight``) until ``crowd_probation_settled`` of its labels agreed with a
  settled answer; its reputation then rises with agreement and falls faster with disagreement; a key below
  ``crowd_mute_reputation`` still gets "recorded" but its labels weigh nothing (it isn't told).
- A page's answer settles (:func:`settle`) only with enough weight (``crowd_settle_weight``), a two-thirds majority,
  two independent labels, and at least one contributor past probation. Anonymous labels alone never settle a page.
- A burst (more than ``crowd_burst_labels`` labels on one page in a day) that pushes the level down is held for a
  person instead of settling: the typical poisoning is a seller calling their own page fine.
- Labels never change a result. Settled answers are training data and a review queue, nothing more.

**Privacy.** No IP address, no account, no time (only the day), no address of the page. The link between a
contributor and a page's hash is dropped once the page settles, or after ``crowd_link_days``; the contributor keeps
only counts. ``POST /v1/contributors/forget`` deletes a contributor.
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import structlog

from unfret.config import Settings

logger = structlog.get_logger("unfret")

Status = Literal["recorded", "updated"]
TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|msclkid|ref|ref_|tag|mc_[ce]id|_ga|igshid)$", re.IGNORECASE)
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # 32 bytes, base64url without padding
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{3,24}$")
RESERVED_HANDLES = re.compile(r"unfret|admin|moderator|official|scam|fraud", re.IGNORECASE)
MAX_TYPES = 40


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


def normalised_url(url: str) -> str:
    """
    The page's address as two shoppers on the same page would both have it: no fragment, no tracking parameters.

    :param url: The checked URL.
    :return: The normalised URL.
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k))
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(query), ""))


def page_key(secret: str, url: str) -> str:
    """
    A keyed hash of the page: labels of the same page meet without Unfret keeping its address.

    :param secret: The session secret.
    :param url: The checked URL.
    :return: 32 hex characters.
    """
    mac = hmac.new(secret.encode(), ("page:" + normalised_url(url)).encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def _mac(secret: str, body: str) -> str:
    return hmac.new(secret.encode(), ("label:" + body).encode(), hashlib.sha256).hexdigest()[:32]


def issue_token(
    secret: str, url: str | None, *, tier: str, level: int, types: list[str], ttl_s: int, now: float | None = None
) -> str | None:
    """
    The token that lets whoever saw this result label it.

    :param secret: The session secret.
    :param url: The checked URL (no token without one: labels of the same page must meet).
    :param tier: quick or deep.
    :param level: The level shown (0–4).
    :param types: The reason types behind it (``risk.because`` and the cards).
    :param ttl_s: How long it may be used.
    :param now: Current time (injectable for tests).
    :return: The token, or ``None``.
    """
    if not secret or not url:
        return None
    expires = int((time.time() if now is None else now) + ttl_s)
    shown = ",".join(dict.fromkeys(t for t in types if re.fullmatch(r"[a-z0-9_]{1,40}", t)))
    payload = f"{page_key(secret, url)}|{tier}|{level}|{shown}|{expires}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _mac(secret, payload)


@dataclass(frozen=True)
class Shown:
    """What a valid label token vouches for."""

    page: str
    tier: str
    level: int
    types: tuple[str, ...]
    token_hash: bytes


def check_token(secret: str, token: str, now: float | None = None) -> Shown:
    """
    Verify a label token.

    :param secret: The session secret.
    :param token: The token from the result.
    :param now: Current time (injectable for tests).
    :return: What it vouches for.
    :raises LabelError: ``BAD_LABEL_TOKEN`` for a forged or expired one.
    """
    encoded, _, mac = token.partition(".")
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        page, tier, level, shown, expires = payload.split("|")
        valid = bool(secret) and hmac.compare_digest(mac, _mac(secret, payload))
        fresh = int(expires) >= (time.time() if now is None else now)
    except ValueError:
        valid = fresh = False
    if not valid:
        raise LabelError("BAD_LABEL_TOKEN", "This result can't be labelled.")
    if not fresh:
        raise LabelError("BAD_LABEL_TOKEN", "This check is too old to label; check the page again.")
    types = tuple(t for t in shown.split(",") if t)
    return Shown(page, tier, int(level), types, hashlib.sha256(token.encode()).digest()[:16])


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


async def submit(
    pool: asyncpg.Pool,
    settings: Settings,
    *,
    token: str,
    level: int | None,
    disagree: list[str],
    contributor_key: str | None,
) -> Status:
    """
    Record one label.

    :param pool: The database pool.
    :param settings: The settings.
    :param token: The result's label token.
    :param level: The page's overall level (0–4) as the labeller sees it, or ``None``.
    :param disagree: Reason types of the result the labeller says are wrong.
    :param contributor_key: The device's contributor key, or ``None`` (anonymous).
    :return: recorded, or updated (this contributor had labelled the page before).
    :raises LabelError: See :class:`LabelError`.
    """
    secret = settings.session_secret.get_secret_value()
    shown = check_token(secret, token)
    wrong = list(dict.fromkeys(disagree))
    if level is None and not wrong:
        raise LabelError("BAD_LABEL", "Send a level, or the findings you disagree with.")
    if level is not None and not 0 <= level <= 4:
        raise LabelError("BAD_LABEL", "The level is 0 (no concern) or 1 to 4.")
    if not set(wrong) <= set(shown.types):
        raise LabelError("BAD_LABEL", "You can only disagree with findings this result showed.")
    digest = key_hash(secret, contributor_key) if contributor_key else None
    today = _today()
    async with pool.acquire() as conn, conn.transaction():
        if digest is None:
            anonymous = await conn.fetchval(
                "SELECT COUNT(*) FROM crowd_labels WHERE contributor_id IS NULL AND anonymous AND created_day = $1",
                today,
            )
            if anonymous >= settings.crowd_anonymous_daily_cap:
                raise LabelError("LABELS_PAUSED", "Unfret takes no more anonymous labels today; thank you.")
            used = await conn.fetchval(
                "INSERT INTO crowd_tokens_used (token_hash, expires_on) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING RETURNING 1",
                shown.token_hash,
                datetime.fromtimestamp(time.time() + settings.crowd_label_token_ttl_s, UTC).date(),
            )
            if used is None:
                raise LabelError("ALREADY_LABELLED", "This result was labelled already.")
            await _insert(
                conn, shown, level=level, disagree=wrong, contributor=None, w=weight(settings, None), anonymous=True
            )
            return "recorded"
        row = await _contributor(conn, digest)
        count = int(row["day_count"]) if row["last_label_day"] == today else 0
        if count >= settings.crowd_key_daily_labels:
            raise LabelError("LABEL_LIMIT", "That's the most labels for one day; thank you, more tomorrow.")
        await conn.execute(
            "UPDATE contributors SET last_label_day = $2, day_count = $3, labels = labels + 1 WHERE id = $1",
            row["id"],
            today,
            count + 1,
        )
        replaced = await conn.fetchval(
            "DELETE FROM crowd_labels WHERE page_key = $1 AND contributor_id = $2 AND NOT settled RETURNING 1",
            shown.page,
            row["id"],
        )
        await _insert(
            conn, shown, level=level, disagree=wrong, contributor=row["id"], w=weight(settings, row), anonymous=False
        )
    return "updated" if replaced else "recorded"


async def _insert(
    conn: asyncpg.Connection,
    shown: Shown,
    *,
    level: int | None,
    disagree: list[str],
    contributor: int | None,
    w: float,
    anonymous: bool,
) -> None:
    await conn.execute(
        "INSERT INTO crowd_labels (page_key, contributor_id, anonymous, tier, shown_level, shown_types, level, "
        "disagree, weight, created_day) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        shown.page,
        contributor,
        anonymous,
        shown.tier,
        shown.level,
        list(shown.types)[:MAX_TYPES],
        level,
        disagree,
        w,
        _today(),
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
        await conn.execute("UPDATE crowd_labels SET weight = 0 WHERE contributor_id = $1 AND NOT settled", cid)
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
    disagree: tuple[str, ...]
    weight: float
    trusted: bool
    day: Any


def decide(votes: list[Vote], settings: Settings, shown_level: int) -> tuple[int | None, bool]:
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
    milder = (level == 0 and shown_level != 0) or (shown_level != 0 and level > shown_level)
    return level, burst > settings.crowd_burst_labels and milder


async def settle(pool: asyncpg.Pool, settings: Settings) -> int:
    """
    Settle the pages whose labels agree enough, update reputations, drop old links (housekeeping).

    :param pool: The database pool.
    :param settings: The settings.
    :return: Pages settled.
    """
    settled = 0
    pages = await pool.fetch("SELECT DISTINCT page_key FROM crowd_labels WHERE NOT settled AND level IS NOT NULL")
    for page in pages:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "SELECT l.*, c.settled AS c_settled FROM crowd_labels l LEFT JOIN contributors c ON c.id = "
                "l.contributor_id WHERE l.page_key = $1 AND NOT l.settled ORDER BY l.id FOR UPDATE OF l",
                page["page_key"],
            )
            votes = [
                Vote(
                    r["contributor_id"],
                    r["level"],
                    tuple(r["disagree"]),
                    float(r["weight"]),
                    (r["c_settled"] or 0) >= settings.crowd_probation_settled,
                    r["created_day"],
                )
                for r in rows
            ]
            shown = int(rows[-1]["shown_level"])
            level, held = decide(votes, settings, shown)
            if level is None:
                continue
            labellers = [v for v in votes if v.level is not None]
            # The findings at least two thirds of the labellers said were wrong.
            said = Counter(t for v in labellers for t in v.disagree)
            wrong = sorted(t for t, n in said.items() if 3 * n >= 2 * len(labellers))
            await conn.execute(
                "INSERT INTO crowd_answers (page_key, level, shown_level, tier, disagree, labels, weight, held, "
                "settled_on) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                page["page_key"],
                level,
                shown,
                rows[-1]["tier"],
                wrong,
                len(labellers),
                sum(v.weight for v in labellers if v.level == level),
                held,
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
                "UPDATE crowd_labels SET settled = TRUE, contributor_id = NULL WHERE page_key = $1 AND NOT settled",
                page["page_key"],
            )
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
        "UPDATE crowd_labels SET contributor_id = NULL WHERE contributor_id IS NOT NULL "
        "AND created_day < CURRENT_DATE - $1::int",
        settings.crowd_link_days,
    )
    await pool.execute("DELETE FROM crowd_tokens_used WHERE expires_on < CURRENT_DATE")
    await pool.execute(
        "DELETE FROM crowd_labels WHERE NOT settled AND created_day < CURRENT_DATE - $1::int",
        settings.crowd_open_days,
    )


async def summary(pool: asyncpg.Pool) -> dict[str, Any]:
    """
    For the operator: settled answers against the level shown, held pages, and which findings people disagree with.

    :param pool: The database pool.
    :return: The numbers.
    """
    answers = await pool.fetch(
        "SELECT shown_level, level, held, COUNT(*) AS n FROM crowd_answers GROUP BY 1, 2, 3 ORDER BY 1, 2"
    )
    disagree = await pool.fetch(
        "SELECT t AS type, COUNT(*) AS disagree FROM crowd_labels, UNNEST(disagree) AS t GROUP BY t ORDER BY 2 DESC"
    )
    shown = await pool.fetch(
        "SELECT t AS type, COUNT(*) AS shown FROM crowd_labels, UNNEST(shown_types) AS t GROUP BY t"
    )
    shown_by = {r["type"]: r["shown"] for r in shown}
    return {
        "labels": await pool.fetchval("SELECT COUNT(*) FROM crowd_labels"),
        "contributors": await pool.fetchval("SELECT COUNT(*) FROM contributors"),
        "answers": [dict(r) for r in answers],
        "findings": [
            {"type": r["type"], "disagree": r["disagree"], "shown": shown_by.get(r["type"], 0)} for r in disagree
        ],
    }
