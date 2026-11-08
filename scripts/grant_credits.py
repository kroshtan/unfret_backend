#!/usr/bin/env python3
"""
Grant (or, with a negative amount, take back) credits for a wallet, directly in the database.

Usage:
    uv run scripts/grant_credits.py <pubkey> <lamports>
    uv run scripts/grant_credits.py <pubkey> --checks <n>
"""

import argparse
import asyncio
import sys

import asyncpg
from solders.pubkey import Pubkey

from veridex.config import settings


async def grant(pubkey: str, lamports: int) -> int:
    """Add ``lamports`` (may be negative) to ``pubkey``'s balance and return the new balance."""
    conn = await asyncpg.connect(settings.postgres_uri)
    try:
        balance = await conn.fetchval(
            "INSERT INTO wallets (pubkey, balance_lamports) VALUES ($1, GREATEST($2, 0)) ON CONFLICT (pubkey) "
            "DO UPDATE SET balance_lamports = GREATEST(wallets.balance_lamports + $2, 0) RETURNING balance_lamports",
            pubkey,
            lamports,
        )
        return int(balance)
    finally:
        await conn.close()


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("pubkey")
    parser.add_argument("lamports", type=int, nargs="?")
    parser.add_argument("--checks", type=int, help="grant this many analyses instead of a lamport amount")
    args = parser.parse_args()
    if (args.lamports is None) == (args.checks is None):
        sys.exit("Give either <lamports> or --checks <n>.")
    try:
        pubkey = str(Pubkey.from_string(args.pubkey))
    except ValueError:
        sys.exit(f"Not a Solana address: {args.pubkey!r}")
    amount = args.lamports if args.checks is None else args.checks * settings.analysis_price_lamports
    print(f"{pubkey}: balance now {asyncio.run(grant(pubkey, amount))} lamports")


if __name__ == "__main__":
    main()
