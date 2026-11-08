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
    (
        4,
        "wallet credits replace accounts",
        """
        -- Phase 4: no accounts, emails or analysis log any more. The only user state is a wallet's balance.
        DROP TABLE IF EXISTS analyze_log;
        DROP TABLE IF EXISTS accounts;
        CREATE TABLE wallets (
            pubkey            TEXT PRIMARY KEY,
            balance_lamports  BIGINT NOT NULL CHECK (balance_lamports >= 0)
        );
        -- Operational: the newest hot-wallet transaction the credits scanner has processed.
        CREATE TABLE scan_cursor (
            name        TEXT PRIMARY KEY,
            signature   TEXT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
    ),
    (
        5,
        "dollar balances and a trial flag",
        """
        -- Prices and balances move from lamports to micro-dollars (1 USD = 1,000,000). Existing balances keep their
        -- number of checks: a check cost 1,500,000 lamports and costs 150,000 micro-dollars now, so divide by 10.
        ALTER TABLE wallets RENAME COLUMN balance_lamports TO balance_usd_micros;
        UPDATE wallets SET balance_usd_micros = balance_usd_micros / 10;
        -- The trial is recorded explicitly: a row no longer means "trial spent", so a purchase (or a 1-lamport
        -- transfer naming someone) can't use up another wallet's trial.
        ALTER TABLE wallets ADD COLUMN trial_granted BOOLEAN NOT NULL DEFAULT FALSE;
        UPDATE wallets SET trial_granted = TRUE;
        """,
    ),
    (
        6,
        "price observations",
        """
        -- Anonymous price observations (veridex.price_store): a hashed product key, the day and the prices only.
        -- No URL, caller or time of day; one row per product per day; rows older than 90 days are deleted.
        CREATE TABLE price_observations (
            product_key  BYTEA NOT NULL CHECK (octet_length(product_key) = 32),
            day          DATE NOT NULL,
            price        NUMERIC(14, 2) NOT NULL CHECK (price >= 0),
            currency     TEXT NOT NULL CHECK (length(currency) <= 3),
            was_price    NUMERIC(14, 2) CHECK (was_price >= 0),
            PRIMARY KEY (product_key, day)
        );
        CREATE INDEX price_observations_day_idx ON price_observations (day);
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
