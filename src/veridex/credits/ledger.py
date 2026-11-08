"""
The credits ledger: ``pubkey → balance_lamports``, and nothing else about anyone.

No purchase history (the chain is the ledger), no analysis log, no URLs. A row's existence also records that the
wallet has had its free trial (or bought before signing in), so a wallet gets at most one.
"""

import asyncpg

type Executor = asyncpg.Pool | asyncpg.Connection


async def balance(db: Executor, pubkey: str) -> int | None:
    """
    Read a wallet's balance.

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :return: Lamports, or ``None`` if the wallet has no row.
    """
    value = await db.fetchval("SELECT balance_lamports FROM wallets WHERE pubkey = $1", pubkey)
    return None if value is None else int(value)


async def create(db: Executor, pubkey: str, lamports: int) -> bool:
    """
    Create a wallet's row with an opening balance, unless it already exists.

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :param lamports: The opening balance (e.g. the free trial).
    :return: ``True`` if the row was created.
    """
    created = await db.fetchval(
        "INSERT INTO wallets (pubkey, balance_lamports) VALUES ($1, $2) ON CONFLICT (pubkey) DO NOTHING "
        "RETURNING TRUE",
        pubkey,
        lamports,
    )
    return bool(created)


async def credit(db: Executor, pubkey: str, lamports: int) -> int:
    """
    Add to a wallet's balance, creating the row if needed.

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :param lamports: The amount to add (positive).
    :return: The new balance.
    :raises ValueError: If ``lamports`` is not positive.
    """
    if lamports <= 0:
        raise ValueError("credit must be positive")
    return int(
        await db.fetchval(
            "INSERT INTO wallets (pubkey, balance_lamports) VALUES ($1, $2) ON CONFLICT (pubkey) "
            "DO UPDATE SET balance_lamports = wallets.balance_lamports + EXCLUDED.balance_lamports "
            "RETURNING balance_lamports",
            pubkey,
            lamports,
        )
    )


async def debit(db: Executor, pubkey: str, lamports: int) -> int | None:
    """
    Take ``lamports`` from a wallet atomically, only if it has enough (concurrent debits never overspend).

    :param db: A pool or connection.
    :param pubkey: The wallet (base58).
    :param lamports: The amount to take.
    :return: The new balance, or ``None`` if the balance was insufficient (or the wallet unknown).
    """
    value = await db.fetchval(
        "UPDATE wallets SET balance_lamports = balance_lamports - $2 "
        "WHERE pubkey = $1 AND balance_lamports >= $2 RETURNING balance_lamports",
        pubkey,
        lamports,
    )
    return None if value is None else int(value)


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
