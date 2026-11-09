"""
The known-listing fast path for ``/v1/check``: the last answer for a listing, served again for a day.

An answer is kept only when it is trustworthy for everyone: Unfret fetched the page itself (never from
caller-supplied content, which could plant an "allow" for a listing every later caller would get), the page was
assessed, and the analysis wasn't degraded. It is served for at most a day, and only while the shop's fingerprint
(its account IDs, apps and template, from :mod:`veridex.shop_store`) is the one the answer was made with: any change
falls through to a full analysis. Answers are stored apart from everything else and never read by a skill, so they
never become evidence.

What is stored: a hash of the listing (shop, path and product query parameters), the tier, the day, the shop
fingerprint's digest and the answer. No URL, caller, payer or time of day.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import asyncpg
import structlog

from veridex import shop_store
from veridex.net import hostname
from veridex.price_store import registrable_domain
from veridex.x402.verdict import Policy, Tier, Verdict, action_for

logger = structlog.get_logger("veridex")

FRESH_DAYS = 1
# Query parameters that track a visit rather than pick a product.
_TRACKING = ("utm_", "ref", "fbclid", "gclid", "msclkid", "mc_", "_ga", "srsltid", "tag", "psc", "th")


def listing_key(url: str) -> bytes | None:
    """
    The listing a check URL points at: shop, path and the query parameters that pick a product.

    :param url: The checked URL.
    :return: 32 bytes, or ``None`` without a host.
    """
    host = hostname(url)
    if not host or "." not in host:
        return None
    parts = urlsplit(url if "://" in url else f"https://{url}")
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(_TRACKING))
    canonical = f"{registrable_domain(host)}|{parts.path.rstrip('/').lower()}?{urlencode(query)}"
    return hashlib.sha256(canonical.encode()).digest()


@dataclass(frozen=True)
class Stored:
    """A kept answer."""

    day: date
    shop_fingerprint: bytes | None
    verdict: dict[str, Any]


class VerdictStore(Protocol):
    """Where kept answers live."""

    async def get(self, key: bytes, tier: Tier) -> Stored | None:
        """
        The kept answer for a listing and tier.

        :param key: From :func:`listing_key`.
        :param tier: The tier.
        :return: The answer, or ``None``.
        """

    async def put(self, key: bytes, tier: Tier, stored: Stored) -> None:
        """
        Keep an answer, replacing the previous one.

        :param key: From :func:`listing_key`.
        :param tier: The tier.
        :param stored: The answer.
        """

    async def purge(self, today: date) -> None:
        """
        Delete answers too old to serve.

        :param today: Today.
        """


class MemoryVerdictStore:
    """In-process store, for tests."""

    def __init__(self) -> None:
        """Start empty."""
        self.rows: dict[tuple[bytes, str], Stored] = {}

    async def get(self, key: bytes, tier: Tier) -> Stored | None:
        """
        Look up.

        :param key: The listing.
        :param tier: The tier.
        :return: The answer.
        """
        return self.rows.get((key, tier))

    async def put(self, key: bytes, tier: Tier, stored: Stored) -> None:
        """
        Keep.

        :param key: The listing.
        :param tier: The tier.
        :param stored: The answer.
        """
        self.rows[(key, tier)] = stored

    async def purge(self, today: date) -> None:
        """
        Drop old answers.

        :param today: Today.
        """
        self.rows = {k: v for k, v in self.rows.items() if v.day >= today - timedelta(days=FRESH_DAYS)}


class PostgresVerdictStore:
    """Answers in the ``listing_verdicts`` table (migration 12)."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def get(self, key: bytes, tier: Tier) -> Stored | None:
        """
        Look up.

        :param key: The listing.
        :param tier: The tier.
        :return: The answer.
        """
        row = await self.pool.fetchrow(
            "SELECT day, shop_fingerprint, verdict FROM listing_verdicts WHERE listing_key = $1 AND tier = $2",
            key,
            tier,
        )
        if row is None:
            return None
        fingerprint = bytes(row["shop_fingerprint"]) if row["shop_fingerprint"] is not None else None
        return Stored(row["day"], fingerprint, json.loads(row["verdict"]))

    async def put(self, key: bytes, tier: Tier, stored: Stored) -> None:
        """
        Keep, replacing.

        :param key: The listing.
        :param tier: The tier.
        :param stored: The answer.
        """
        await self.pool.execute(
            "INSERT INTO listing_verdicts (listing_key, tier, day, shop_fingerprint, verdict) VALUES ($1, $2, $3, $4,"
            " $5::jsonb) ON CONFLICT (listing_key, tier) DO UPDATE SET day = EXCLUDED.day,"
            " shop_fingerprint = EXCLUDED.shop_fingerprint, verdict = EXCLUDED.verdict",
            key,
            tier,
            stored.day,
            stored.shop_fingerprint,
            json.dumps(stored.verdict),
        )

    async def purge(self, today: date) -> None:
        """
        Delete old answers.

        :param today: Today.
        """
        await self.pool.execute("DELETE FROM listing_verdicts WHERE day < $1", today - timedelta(days=FRESH_DAYS))


_store: VerdictStore = MemoryVerdictStore()
_purged_on: date | None = None


def use_store(store: VerdictStore) -> None:
    """
    Use a different store (the app swaps in :class:`PostgresVerdictStore` at startup).

    :param store: The store.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


def keepable(result: dict[str, Any], verdict: Verdict, *, fetched_by_us: bool) -> bool:
    """
    Whether an answer may be served to later callers.

    :param result: The final analysis state.
    :param verdict: The answer.
    :param fetched_by_us: Whether Unfret fetched the page (not caller-supplied content).
    :return: ``True`` only for an assessed, undegraded analysis of a page Unfret fetched itself.
    """
    assessment = result.get("assessment") or {}
    return fetched_by_us and verdict.assessed and bool(assessment) and not assessment.get("degraded")


async def remember(url: str, result: dict[str, Any], verdict: Verdict, *, fetched_by_us: bool) -> None:
    """
    Keep an answer for the fast path when it is :func:`keepable`. Never raises.

    :param url: The checked URL.
    :param result: The final analysis state.
    :param verdict: The answer.
    :param fetched_by_us: Whether Unfret fetched the page.
    """
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    key = listing_key(url)
    if key is None or not keepable(result, verdict, fetched_by_us=fetched_by_us):
        return
    try:
        page = await shop_store.page_links(result)
        day = shop_store.today()
        stored = Stored(day, shop_store.fingerprint_digest(page) if page else None, verdict.model_dump(mode="json"))
        await _store.put(key, verdict.tier, stored)
        if _purged_on != day:
            _purged_on = day
            await _store.purge(day)
    except Exception as exc:  # noqa: BLE001 - the fast path is optional
        logger.warning("verdict_store_failed", error=type(exc).__name__)


async def recall(url: str, tier: Tier, policy: Policy) -> Verdict | None:
    """
    A fresh kept answer for the listing, with the caller's policy applied, or ``None`` to analyse it.

    :param url: The checked URL.
    :param tier: The tier asked for.
    :param policy: The caller's policy.
    :return: The answer (``source`` ``"fingerprint"``), or ``None``.
    """
    key = listing_key(url)
    if key is None:
        return None
    try:
        stored = await _store.get(key, tier)
        if stored is None or stored.day < shop_store.today() - timedelta(days=FRESH_DAYS):
            return None
        if stored.shop_fingerprint is not None:
            current = await shop_store.store().fingerprint(registrable_domain(hostname(url)))
            if current != stored.shop_fingerprint:
                return None  # the shop changed since: analyse again
        verdict = Verdict.model_validate(stored.verdict)
    except Exception as exc:  # noqa: BLE001 - fall through to a full analysis
        logger.warning("verdict_recall_failed", error=type(exc).__name__)
        return None
    risk = verdict.risk_score if verdict.risk_score is not None else 1.0
    return verdict.model_copy(
        update={"action": action_for(risk, policy), "policy_applied": policy, "cached": True, "source": "fingerprint"}
    )
