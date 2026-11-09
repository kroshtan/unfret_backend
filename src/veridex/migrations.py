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
    (
        7,
        "shop fingerprints",
        """
        -- Web shops seen by analyses (veridex.shop_store): facts about the shop only, days not times; no URL,
        -- caller, score or verdict. Deleted 400 days after last seen.
        CREATE TABLE shops (
            domain      TEXT PRIMARY KEY CHECK (domain = LOWER(domain) AND length(domain) <= 253),
            first_seen  DATE NOT NULL,
            last_seen   DATE NOT NULL,
            platform    TEXT NOT NULL CHECK (length(platform) <= 20),
            apps        TEXT[] NOT NULL DEFAULT '{}',
            skeleton    BIGINT,
            registered  DATE,
            registrar   TEXT CHECK (length(registrar) <= 200),
            first_cert  DATE
        );
        CREATE INDEX shops_last_seen_idx ON shops (last_seen);
        -- Account identifiers per shop. Contacts are personal data: only a keyed hash (64 hex characters) may be
        -- stored, enforced here; they are deleted 180 days after last seen.
        CREATE TABLE shop_links (
            kind        TEXT NOT NULL CHECK (length(kind) <= 20),
            value       TEXT NOT NULL CHECK (length(value) <= 120),
            domain      TEXT NOT NULL REFERENCES shops (domain) ON DELETE CASCADE,
            first_seen  DATE NOT NULL,
            last_seen   DATE NOT NULL,
            PRIMARY KEY (kind, value, domain),
            CHECK (kind NOT IN ('email', 'phone') OR value ~ '^[0-9a-f]{64}$')
        );
        CREATE INDEX shop_links_domain_idx ON shop_links (domain);
        CREATE INDEX shop_links_last_seen_idx ON shop_links (last_seen);
        """,
    ),
    (
        8,
        "api keys and fiat payments",
        """
        -- API keys (veridex.credits.keys): a public id and the SHA-256 of the key and of its recovery code, nothing
        -- else. The balance lives in wallets like any account, under 'key:<id>' (wallet addresses are base58, so
        -- the two never collide).
        CREATE TABLE api_keys (
            id             TEXT PRIMARY KEY CHECK (id ~ '^[a-z2-7]{10}$'),
            key_hash       BYTEA NOT NULL CHECK (octet_length(key_hash) = 32),
            recovery_hash  BYTEA NOT NULL CHECK (octet_length(recovery_hash) = 32),
            created_on     DATE NOT NULL DEFAULT CURRENT_DATE,
            rotated_on     DATE
        );
        -- A fiat refund or chargeback may take back credits already spent: the balance may then go negative, which
        -- blocks paid use (debits need balance >= price) until a top-up.
        ALTER TABLE wallets DROP CONSTRAINT IF EXISTS wallets_balance_lamports_check;
        -- Fiat orders (veridex.credits.fiat): to credit each exactly once, debit the right account on a refund or
        -- chargeback, and for support. Never the buyer's email, name or address. Deleted after 180 days.
        CREATE TABLE fiat_payments (
            provider             TEXT NOT NULL CHECK (length(provider) <= 20),
            order_id             TEXT NOT NULL CHECK (length(order_id) <= 100),
            account              TEXT NOT NULL CHECK (length(account) <= 64),
            amount_cents         BIGINT NOT NULL CHECK (amount_cents > 0),
            credited_usd_micros  BIGINT NOT NULL CHECK (credited_usd_micros > 0),
            refunded_cents       BIGINT NOT NULL DEFAULT 0 CHECK (refunded_cents BETWEEN 0 AND amount_cents),
            disputed             BOOLEAN NOT NULL DEFAULT FALSE,
            debited_usd_micros   BIGINT NOT NULL DEFAULT 0
                CHECK (debited_usd_micros BETWEEN 0 AND credited_usd_micros),
            created_on           DATE NOT NULL DEFAULT CURRENT_DATE,
            PRIMARY KEY (provider, order_id)
        );
        CREATE INDEX fiat_payments_created_on_idx ON fiat_payments (created_on);
        -- Which account a checkout is for, in case the order's webhook doesn't echo the checkout metadata.
        CREATE TABLE fiat_checkouts (
            provider     TEXT NOT NULL CHECK (length(provider) <= 20),
            checkout_id  TEXT NOT NULL CHECK (length(checkout_id) <= 100),
            account      TEXT NOT NULL CHECK (length(account) <= 64),
            created_on   DATE NOT NULL DEFAULT CURRENT_DATE,
            PRIMARY KEY (provider, checkout_id)
        );
        """,
    ),
    (
        9,
        "reports and labelled pages",
        """
        -- Reports of wrong results (veridex.reports), with the reporter's consent to share that check's page data.
        -- One row per page in the review queue; later reports of the same page only raise corroborations. The
        -- account is kept only to pay a reward: the row is deleted once handled (its data moves to labelled_pages
        -- without the account) or after 30 days unhandled.
        CREATE TABLE reports (
            id              BIGSERIAL PRIMARY KEY,
            page_key        TEXT NOT NULL UNIQUE CHECK (length(page_key) <= 400),
            domain          TEXT NOT NULL CHECK (length(domain) <= 253),
            account         TEXT NOT NULL CHECK (length(account) <= 64),
            reason          TEXT NOT NULL CHECK (reason IN ('real_brand', 'scam', 'junk', 'other')),
            score           SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
            corroborations  INTEGER NOT NULL DEFAULT 0,
            payload         JSONB NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX reports_created_at_idx ON reports (created_at);
        -- Who already reported an open item (keyed hashes of accounts), so nobody reports a page twice; deleted
        -- with the item.
        CREATE TABLE report_votes (
            report_id    BIGINT NOT NULL REFERENCES reports (id) ON DELETE CASCADE,
            account_tag  BYTEA NOT NULL CHECK (octet_length(account_tag) = 16),
            PRIMARY KEY (report_id, account_tag)
        );
        -- Reports per account per month (keyed hashes), for the monthly limit; old months are deleted.
        CREATE TABLE report_quota (
            account_tag  BYTEA NOT NULL CHECK (octet_length(account_tag) = 16),
            month        TEXT NOT NULL CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
            count        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_tag, month)
        );
        -- Human-labelled pages: training data that belongs to the page, not to whoever reported it.
        CREATE TABLE labelled_pages (
            id           BIGSERIAL PRIMARY KEY,
            page_key     TEXT NOT NULL CHECK (length(page_key) <= 400),
            domain       TEXT NOT NULL CHECK (length(domain) <= 253),
            label        TEXT NOT NULL CHECK (label IN ('legit', 'junk', 'scam')),
            score        SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 100),
            reason       TEXT NOT NULL,
            payload      JSONB NOT NULL,
            labelled_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX labelled_pages_page_key_idx ON labelled_pages (page_key, labelled_at);
        """,
    ),
    (
        10,
        "crypto vat quotes and sales records",
        """
        -- Priced crypto purchases (veridex.credits.vat): the pack, the buyer's stated country and the VAT, and who
        -- to credit. The purchase transaction's memo names the id. Deleted a day after they expire.
        CREATE TABLE vat_quotes (
            id                TEXT PRIMARY KEY CHECK (id ~ '^[a-z2-7]{20}$'),
            checks            INTEGER NOT NULL CHECK (checks > 0),
            country           TEXT NOT NULL CHECK (country ~ '^[A-Z]{2}$'),
            country_mismatch  BOOLEAN NOT NULL,
            vat_rate_bps      INTEGER NOT NULL CHECK (vat_rate_bps BETWEEN 0 AND 10000),
            net_usd_micros    BIGINT NOT NULL CHECK (net_usd_micros > 0),
            vat_usd_micros    BIGINT NOT NULL CHECK (vat_usd_micros >= 0),
            beneficiary       TEXT CHECK (length(beneficiary) <= 64),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX vat_quotes_created_at_idx ON vat_quotes (created_at);
        -- One row per crypto credit purchase, for the bookkeeping: kept ten years. No IP address and no wallet
        -- (the transaction on chain shows who paid); country 'unknown' for a transfer without a quote.
        CREATE TABLE crypto_sales (
            id                BIGSERIAL PRIMARY KEY,
            sold_on           DATE NOT NULL,
            country           TEXT NOT NULL CHECK (country ~ '^[A-Z]{2}$' OR country = 'unknown'),
            country_mismatch  BOOLEAN NOT NULL,
            net_usd_micros    BIGINT NOT NULL CHECK (net_usd_micros >= 0),
            vat_rate_bps      INTEGER NOT NULL CHECK (vat_rate_bps BETWEEN 0 AND 10000),
            vat_usd_micros    BIGINT NOT NULL CHECK (vat_usd_micros >= 0),
            asset             TEXT NOT NULL CHECK (asset IN ('SOL', 'USDC')),
            amount            BIGINT NOT NULL CHECK (amount > 0),
            signature         TEXT NOT NULL CHECK (length(signature) <= 100)
        );
        CREATE INDEX crypto_sales_sold_on_idx ON crypto_sales (sold_on);
        """,
    ),
    (
        11,
        "image index",
        """
        -- Unfret's own image index (veridex.image_store): one main photo per listing, as fingerprints plus the
        -- photo's own address for thumbnails. The listing is a hash (shop + public product ID or page path), never
        -- the page address; days not times. Photos seen in analyses are deleted 400 days after last seen.
        CREATE TABLE image_prints (
            id           BIGSERIAL PRIMARY KEY,
            source       TEXT NOT NULL CHECK (source IN ('seen', 'abo')),
            domain       TEXT NOT NULL CHECK (length(domain) <= 253),
            listing_key  BYTEA NOT NULL CHECK (octet_length(listing_key) = 32),
            phash        BIGINT NOT NULL,
            dhash        BIGINT NOT NULL,
            embedding    BYTEA NOT NULL CHECK (octet_length(embedding) = 384),
            colour       BYTEA NOT NULL CHECK (octet_length(colour) = 48),
            image_url    TEXT NOT NULL CHECK (length(image_url) <= 1000),
            label        TEXT CHECK (length(label) <= 120),
            first_seen   DATE NOT NULL,
            last_seen    DATE NOT NULL,
            UNIQUE (domain, listing_key)
        );
        CREATE INDEX image_prints_seen_idx ON image_prints (last_seen) WHERE source = 'seen';
        """,
    ),
    (
        12,
        "known-listing fast path",
        """
        -- The digest of each shop's latest fingerprint, to tell when a kept answer is out of date.
        ALTER TABLE shops ADD COLUMN fingerprint BYTEA CHECK (octet_length(fingerprint) = 32);
        -- Answers of full /v1/check analyses of pages Unfret fetched itself (veridex.verdict_store), served again for
        -- a day while the shop is unchanged. Keyed by a listing hash; no URL, caller or payer. Never read by a skill.
        CREATE TABLE listing_verdicts (
            listing_key       BYTEA NOT NULL CHECK (octet_length(listing_key) = 32),
            tier              TEXT NOT NULL CHECK (tier IN ('quick', 'deep')),
            day               DATE NOT NULL,
            shop_fingerprint  BYTEA CHECK (octet_length(shop_fingerprint) = 32),
            verdict           JSONB NOT NULL,
            PRIMARY KEY (listing_key, tier)
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
