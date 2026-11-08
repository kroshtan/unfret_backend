"""The price book: canonical SOL prices, USDC converted with a validated Pyth SOL/USD price, the fixed fallback."""

import base64
import struct
from decimal import Decimal
from typing import Any

import pytest
from solders.pubkey import Pubkey

from veridex.config import Settings
from veridex.prices import (
    PYTH_RECEIVER_PROGRAM,
    SOL_USD_FEED_ID,
    PriceBook,
    PriceUnavailableError,
    PythSolUsd,
    SolUsd,
    lamports_to_usdc,
    usdc_to_lamports,
    validate_sol_usd,
)
from veridex.x402.solana import SolanaRpc

NOW = 1_800_000_000
OWNER = str(PYTH_RECEIVER_PROGRAM)


def price_update(
    *,
    price: int = 8_960_736_310,  # $89.6073631 at exponent -8
    conf: int = 6_849_210,
    exponent: int = -8,
    publish_time: int = NOW - 4,
    full: bool = True,
    feed_id: bytes = SOL_USD_FEED_ID,
) -> bytes:
    """A PriceUpdateV2 account as Pyth's receiver writes it (the layout read from mainnet's SOL/USD account)."""
    level = b"\x01" if full else b"\x00\x05"
    message = feed_id + struct.pack("<qQiqqqQ", price, conf, exponent, publish_time, publish_time - 1, price, conf)
    return b"\x22" * 8 + bytes(32) + level + message + struct.pack("<Q", 1)


def check(data: bytes, owner: str = OWNER, **limits: Any) -> SolUsd:
    return validate_sol_usd(data, owner, now=NOW, max_age_s=limits.get("max_age_s", 60), max_conf_bps=100)


def test_a_fresh_verified_sol_usd_price_is_accepted() -> None:
    reading = check(price_update())
    assert reading.usd_per_sol == Decimal("89.60736310")
    assert reading.publish_time == NOW - 4


@pytest.mark.parametrize(
    ("data", "owner", "reason"),
    [
        (price_update(publish_time=NOW - 61), OWNER, "stale"),
        (price_update(full=False), OWNER, "partially verified"),
        (price_update(feed_id=bytes(32)), OWNER, "not the SOL/USD feed"),
        (price_update(conf=100_000_000), OWNER, "confidence"),  # $1 on $89.6: >1%
        (price_update(price=0), OWNER, "not positive"),
        (price_update(), str(Pubkey.default()), "not owned"),
        (b"\x00" * 40, OWNER, "unreadable"),
    ],
)
def test_untrustworthy_prices_are_refused(data: bytes, owner: str, reason: str) -> None:
    with pytest.raises(PriceUnavailableError, match=reason):
        check(data, owner)


def test_conversions_round_in_veridexs_disfavour_never_the_payers() -> None:
    price = Decimal("89.6073631")
    # 0.0015 SOL at that price = 134_411.04 USDC units: charged as 134_412 (rounded up).
    assert lamports_to_usdc(1_500_000, price) == 134_412
    # 134_412 units = 1_500_010.4 lamports: credited as 1_500_010 (rounded down).
    assert usdc_to_lamports(134_412, price) == 1_500_010
    assert usdc_to_lamports(1_000_000, price) == 11_159_797  # 1 USDC


class StaticOracle(PythSolUsd):
    def __init__(self, usd_per_sol: str | None) -> None:
        self.value = usd_per_sol

    async def read(self, now: float | None = None) -> SolUsd:
        if self.value is None:
            raise PriceUnavailableError("oracle price is stale (99 s old)")
        return SolUsd(Decimal(self.value), Decimal("0.05"), NOW)


async def test_sol_mode_converts_the_canonical_sol_prices() -> None:
    book = PriceBook(Settings(), StaticOracle("100"))
    assert await book.price("analysis", "SOL") == 1_500_000
    assert await book.price("analysis", "USDC") == 150_000  # 0.0015 SOL at $100
    assert await book.price("x402_quick", "USDC") == 10_000
    assert await book.lamports_for(150_000, "USDC") == 1_500_000
    assert await book.converted("USDC") and not await book.converted("SOL")


async def test_usdc_is_refused_while_the_oracle_is_unavailable() -> None:
    book = PriceBook(Settings(), StaticOracle(None))
    assert await book.price("x402_deep", "SOL") == 1_500_000  # SOL never needs the oracle
    with pytest.raises(PriceUnavailableError):
        await book.price("x402_deep", "USDC")
    with pytest.raises(PriceUnavailableError):
        await book.lamports_for(150_000, "USDC")
    with pytest.raises(PriceUnavailableError):
        await PriceBook(Settings(), None).price("analysis", "USDC")


async def test_fixed_mode_is_an_explicit_fallback() -> None:
    book = PriceBook(Settings(price_mode="fixed"), None)
    assert await book.price("analysis", "USDC") == 150_000
    assert await book.price("x402_quick", "USDC") == 10_000
    assert await book.lamports_for(300_000, "USDC") == 3_000_000
    assert not await book.converted("USDC")
    assert Settings().price_mode == "sol"  # the default is canonical SOL + oracle


class AccountRpc(SolanaRpc):
    def __init__(self, data: bytes) -> None:
        super().__init__("http://rpc.invalid")
        self.data, self.calls = data, 0

    async def call(self, method: str, params: list[Any]) -> Any:
        self.calls += 1
        return {"value": {"data": [base64.b64encode(self.data).decode(), "base64"], "owner": OWNER}}


async def test_the_reader_caches_briefly_and_rechecks_age() -> None:
    rpc = AccountRpc(price_update(publish_time=NOW - 50))
    oracle = PythSolUsd(rpc, Pubkey.default(), max_age_s=60, max_conf_bps=100)
    assert (await oracle.read(NOW)).usd_per_sol > 0
    await oracle.read(NOW + 2)
    assert rpc.calls == 1  # cached
    with pytest.raises(PriceUnavailableError, match="stale"):
        await oracle.read(NOW + 11)  # re-read, and now 61 s old
