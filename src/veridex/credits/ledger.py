"""
The credits ledger: ``account → balance`` in micro-dollars, and nothing else about anyone.

An account is a wallet (its base58 address) or an API key (``key:<id>``, see :mod:`veridex.credits.keys`); both
live in the ``wallets`` table, whose ``pubkey`` column holds either. Every account starts at zero. No purchase
history (the chain is the ledger), no analysis log, no URLs. Balances are micro-dollars (1 USD = 1,000,000), so a
check keeps its value whatever SOL does. The ``pubkey`` parameters below take either kind of account.

A balance can be negative only after a fiat refund or chargeback took back credits already spent
(:func:`charge`); :func:`debit` never goes below zero, so a negative balance blocks paid use until a top-up.
"""

import asyncpg

type Executor = asyncpg.Pool | asyncpg.Connection


async def balance(db: Executor, pubkey: str) -> int | None:
    """
    Read a wallet's balance.

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :return: Micro-dollars, or ``None`` if the wallet has no row.
    """
    value = await db.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", pubkey)
    return None if value is None else int(value)


async def credit(db: Executor, pubkey: str, amount: int) -> int:
    """
    Add to a wallet's balance, creating the row if needed.

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :param amount: The micro-dollars to add (positive).
    :return: The new balance.
    :raises ValueError: If ``amount`` is not positive.
    """
    if amount <= 0:
        raise ValueError("credit must be positive")
    return int(
        await db.fetchval(
            "INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, $2) ON CONFLICT (pubkey) "
            "DO UPDATE SET balance_usd_micros = wallets.balance_usd_micros + EXCLUDED.balance_usd_micros "
            "RETURNING balance_usd_micros",
            pubkey,
            amount,
        )
    )


async def debit(db: Executor, pubkey: str, amount: int) -> int | None:
    """
    Take ``amount`` from a wallet atomically, only if it has enough (concurrent debits never overspend).

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :param amount: The micro-dollars to take.
    :return: The new balance, or ``None`` if the balance was insufficient (or the wallet unknown).
    """
    value = await db.fetchval(
        "UPDATE wallets SET balance_usd_micros = balance_usd_micros - $2 "
        "WHERE pubkey = $1 AND balance_usd_micros >= $2 RETURNING balance_usd_micros",
        pubkey,
        amount,
    )
    return None if value is None else int(value)


async def charge(db: Executor, pubkey: str, amount: int) -> int:
    """
    Take ``amount`` back from an account even if that makes its balance negative (a refund or chargeback).

    :param db: A pool or connection.
    :param pubkey: The account.
    :param amount: The micro-dollars to take back (positive).
    :return: The new balance.
    :raises ValueError: If ``amount`` is not positive.
    """
    if amount <= 0:
        raise ValueError("charge must be positive")
    return int(
        await db.fetchval(
            "INSERT INTO wallets (pubkey, balance_usd_micros) VALUES ($1, -$2::bigint) ON CONFLICT (pubkey) "
            "DO UPDATE SET balance_usd_micros = wallets.balance_usd_micros - $2::bigint RETURNING balance_usd_micros",
            pubkey,
            amount,
        )
    )


async def get_cursor(db: Executor, name: str) -> str | None:
    """
    Read a scanner's cursor: the newest transaction it has processed.

    :param db: A pool or connection.
    :param name: The cursor name (one per scanned address).
    :return: The signature, or ``None`` if the scanner never ran.
    """
    value = await db.fetchval("SELECT signature FROM scan_cursor WHERE name = $1", name)
    return None if value is None else str(value)


async def set_cursor(db: Executor, name: str, signature: str) -> None:
    """
    Move a scanner's cursor (in the same transaction as the credits it covers).

    :param db: A pool or connection.
    :param name: The cursor name.
    :param signature: The newest processed signature.
    """
    await db.execute(
        "INSERT INTO scan_cursor (name, signature, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (name) DO UPDATE SET signature = EXCLUDED.signature, updated_at = NOW()",
        name,
        signature,
    )
