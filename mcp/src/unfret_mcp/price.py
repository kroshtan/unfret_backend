"""
The agent's own SOL/USD price, read on chain from Pyth, so the spend guard can cap SOL payments in dollars.

Unfret quotes SOL at its own Pyth price; the guard doesn't take Unfret's word for what a wSOL charge is worth, it
values it independently. Without a fresh, fully verified, tight price, SOL payments are refused (USDC still works).
"""

import base64
import struct
import time
from decimal import Decimal

from solders.pubkey import Pubkey

from unfret_mcp.solana import Rpc

PYTH_RECEIVER_PROGRAM = Pubkey.from_string("rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ")
SOL_USD_ACCOUNT = Pubkey.from_string("7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE")  # sponsored push feed, shard 0
SOL_USD_FEED_ID = bytes.fromhex("ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d")
_HEADER = 8 + 32  # Anchor discriminator, write authority
_FULL = 1


class PriceError(RuntimeError):
    """No trustworthy SOL/USD price right now."""


class SolUsd:
    """Reads Pyth's SOL/USD ``PriceUpdateV2`` account and checks it before use."""

    def __init__(self, rpc: Rpc, *, max_age_s: float = 120, max_conf_bps: int = 200) -> None:
        """
        Configure the reader.

        :param rpc: A mainnet RPC (Pyth's devnet copy updates too rarely).
        :param max_age_s: The oldest acceptable price.
        :param max_conf_bps: The widest acceptable confidence interval, in basis points of the price.
        """
        self.rpc = rpc
        self.max_age_s = max_age_s
        self.max_conf_bps = max_conf_bps

    async def usd_per_sol(self) -> Decimal:
        """
        The current price.

        :return: US dollars per SOL.
        :raises PriceError: If the account can't be read or fails a check.
        """
        try:
            info = await self.rpc.call("getAccountInfo", [str(SOL_USD_ACCOUNT), {"encoding": "base64"}])
        except Exception as exc:
            raise PriceError(f"SOL/USD unreadable: {exc}") from exc
        value = (info or {}).get("value")
        if not value or value.get("owner") != str(PYTH_RECEIVER_PROGRAM):
            raise PriceError("SOL/USD account missing or not Pyth's")
        return self.check(base64.b64decode(value["data"][0]), time.time())

    def check(self, data: bytes, now: float) -> Decimal:
        """
        Validate raw ``PriceUpdateV2`` data.

        :param data: The account data.
        :param now: The current unix time.
        :return: US dollars per SOL.
        :raises PriceError: If it is short, not fully verified, not SOL/USD, stale, too uncertain or not positive.
        """
        if len(data) < _HEADER + 2 + 32 + 28 or data[_HEADER] != _FULL:
            raise PriceError("SOL/USD price not fully verified")
        offset = _HEADER + 1
        if data[offset : offset + 32] != SOL_USD_FEED_ID:
            raise PriceError("not the SOL/USD feed")
        price, conf, exponent, publish_time = struct.unpack_from("<qQiq", data, offset + 32)
        if price <= 0:
            raise PriceError("SOL/USD price not positive")
        if now - publish_time > self.max_age_s:
            raise PriceError("SOL/USD price stale")
        if conf * 10_000 > price * self.max_conf_bps:
            raise PriceError("SOL/USD price too uncertain")
        return Decimal(price).scaleb(exponent)
