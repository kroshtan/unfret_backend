"""
Versioned database migrations, applied in order at startup.

Each migration runs once, in its own transaction, recorded in ``schema_migrations``. A Postgres advisory lock makes
concurrent starts (several workers, a rolling deploy) apply them one at a time. Migration 1 reproduces the schema
that earlier versions created ad hoc, idempotently, so an existing database simply adopts version 1.

To change the schema, append a migration; never edit one that has shipped.
"""

import asyncpg
import structlog

logger = structlog.get_logger("veridex")

_LOCK_ID = 0x7E21DE  # advisory lock key for migrations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "accounts and analyze log",
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id                      SERIAL PRIMARY KEY,
            username                TEXT UNIQUE NOT NULL CHECK (username = LOWER(username)),
            hashed_password         TEXT NOT NULL,
            contact_email           TEXT UNIQUE NOT NULL,
            subscription_status     TEXT NOT NULL DEFAULT 'free',
            paddle_subscription_id  TEXT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS paddle_subscription_id TEXT;
        CREATE TABLE IF NOT EXISTS analyze_log (
            id          SERIAL PRIMARY KEY,
            username    TEXT NOT NULL REFERENCES accounts(username) ON DELETE CASCADE,
            url         TEXT NOT NULL,
            score       INTEGER NOT NULL,
            explanation TEXT NOT NULL,
            analyzed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS analyze_log_username_analyzed_at_idx ON analyze_log (username, analyzed_at);
        """,
    ),
    (
        2,
        "store-level fact cache",
        """
        -- Facts about shops and images (RDAP, CT, VIES, reverse image by hash). Keyed by fact, never by caller.
        CREATE TABLE fact_cache (
            kind        TEXT NOT NULL,
            key         TEXT NOT NULL,
            value       JSONB,
            expires_at  TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (kind, key)
        );
        CREATE INDEX fact_cache_expires_at_idx ON fact_cache (expires_at);
        """,
    ),
    (
        3,
        "usage counters",
        """
        -- Operational counters per period, e.g. TinEye searches per month for the spending cap.
        CREATE TABLE usage_counters (
            name    TEXT NOT NULL,
            period  TEXT NOT NULL,
            count   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (name, period)
        );
        """,
    ),
]


async def migrate(pool: asyncpg.Pool, migrations: list[tuple[int, str, str]] = MIGRATIONS) -> list[int]:
    """
    Apply every migration that hasn't been applied yet.

    :param pool: The connection pool.
    :param migrations: ``(version, name, sql)`` in ascending version order.
    :return: The versions applied by this call.
    """
    applied_now: list[int] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for version, name, sql in migrations:
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (version, name) VALUES ($1, $2)", version, name)
                logger.info("migration_applied", version=version, name=name)
                applied_now.append(version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
    return applied_now
