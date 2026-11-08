import json
import random
from typing import Any

import asyncpg

from veridex.config import settings
from veridex.migrations import migrate


async def create_pool() -> asyncpg.Pool:
    """Create and return an asyncpg connection pool."""
    # A query that hangs (a lock, a stuck connection) fails after 10 s instead of holding a request forever.
    return await asyncpg.create_pool(settings.postgres_uri, command_timeout=10)


async def init_db(pool: asyncpg.Pool) -> None:
    """Bring the schema up to date (see :mod:`veridex.migrations`)."""
    await migrate(pool)


class PostgresFactStore:
    """The store-level fact cache (:mod:`veridex.fact_cache`) in the ``fact_cache`` table."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """
        Use the given pool.

        :param pool: The connection pool.
        """
        self.pool = pool

    async def get(self, kind: str, key: str) -> tuple[bool, Any]:
        """
        Look a fact up; expired rows count as missing.

        :param kind: The fact kind.
        :param key: The fact key.
        :return: ``(found, value)``.
        """
        row = await self.pool.fetchrow(
            "SELECT value FROM fact_cache WHERE kind = $1 AND key = $2 AND expires_at > NOW()", kind, key
        )
        return (False, None) if row is None else (True, json.loads(row["value"]) if row["value"] else None)

    async def put(self, kind: str, key: str, value: Any, ttl_s: float) -> None:
        """
        Store a fact, and now and then purge expired rows.

        :param kind: The fact kind.
        :param key: The fact key.
        :param value: A JSON-serialisable value (``None`` records a miss).
        :param ttl_s: Seconds until it expires.
        """
        await self.pool.execute(
            "INSERT INTO fact_cache (kind, key, value, expires_at) "
            "VALUES ($1, $2, $3::jsonb, NOW() + make_interval(secs => $4)) "
            "ON CONFLICT (kind, key) DO UPDATE SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at",
            kind,
            key,
            json.dumps(value) if value is not None else None,
            float(ttl_s),
        )
        if random.random() < 0.01:  # noqa: S311 - housekeeping, not security
            await self.pool.execute("DELETE FROM fact_cache WHERE expires_at < NOW()")


class PostgresBudget:
    """A per-period usage counter (e.g. TinEye searches per month) in the ``usage_counters`` table."""

    def __init__(self, pool: asyncpg.Pool, name: str) -> None:
        """
        Count usage under ``name``.

        :param pool: The connection pool.
        :param name: The counter name, e.g. ``"tineye"``.
        """
        self.pool, self.name = pool, name

    async def reserve(self, month: str, limit: int) -> bool:
        """
        Atomically count one use for ``month`` if that stays within ``limit``.

        :param month: The period, e.g. ``"2026-11"``.
        :param limit: The maximum per period.
        :return: ``True`` if the use may go ahead.
        """
        row = await self.pool.fetchrow(
            "INSERT INTO usage_counters (name, period, count) VALUES ($1, $2, 1) "
            "ON CONFLICT (name, period) DO UPDATE SET count = usage_counters.count + 1 "
            "WHERE usage_counters.count < $3 RETURNING count",
            self.name,
            month,
            limit,
        )
        return row is not None and int(row["count"]) <= limit
