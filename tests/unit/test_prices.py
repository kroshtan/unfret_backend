"""The price book: canonical dollar prices, USDC exact, SOL converted with a validated Pyth SOL/USD price."""

import base64
import struct
from decimal import Decimal
from typing import Any

import pytest
from solders.pubkey import Pubkey

from tests.unit.x402_helpers import CHECK_USD, StaticOracle
from unfret.config import Settings
from unfret.prices import (
    PYTH_RECEIVER_PROGRAM,
    SOL_USD_FEED_ID,
    PriceBook,
    PriceUnavailableError,
    PythSolUsd,
    SolUsd,
    format_usd,
    lamports_to_usd,
    lamports_to_usdc,
    usd_to_lamports,
    usdc_to_lamports,
    validate_sol_usd,
)
from unfret.x402.solana import SolanaRpc

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


def test_conversions_round_in_unfrets_disfavour_never_the_payers() -> None:
    price = Decimal("89.6073631")
    # 0.0015 SOL at that price = 134_411.04 USDC units: charged as 134_412 (rounded up).
    assert lamports_to_usdc(1_500_000, price) == 134_412
    # 134_412 units = 1_500_010.4 lamports: credited as 1_500_010 (rounded down).
    assert usdc_to_lamports(134_412, price) == 1_500_010
    assert usdc_to_lamports(1_000_000, price) == 11_159_797  # 1 USDC


async def test_dollar_prices_are_canonical_usdc_exact_and_sol_converted() -> None:
    book = PriceBook(Settings(price_per_check_usd=CHECK_USD), StaticOracle("100"))
    assert book.usd("analysis") == 150_000 and book.usd("x402_quick") == 10_000 and book.usd("x402_deep") == 150_000
    assert await book.price("analysis", "USDC") == 150_000  # $0.15 exactly
    assert await book.price("analysis", "SOL") == 1_500_000  # $0.15 at $100/SOL
    assert await book.price("x402_quick", "SOL") == 100_000
    assert await book.value(1_500_000, "SOL") == 150_000
    assert await book.value(150_000, "USDC") == 150_000
    assert book.converted("SOL") and not book.converted("USDC")


async def test_sol_conversions_round_in_unfrets_disfavour() -> None:
    book = PriceBook(Settings(price_per_check_usd=CHECK_USD), StaticOracle("89.6073631"))
    # $0.15 / $89.6073631 = 1_673_969.4 lamports: charged as 1_673_970 (rounded up).
    assert await book.price("analysis", "SOL") == 1_673_970
    # 1_673_970 lamports = $0.15000005…: credited as 150_000 micro-dollars (rounded down).
    assert await book.value(1_673_970, "SOL") == 150_000
    assert usd_to_lamports(150_000, Decimal(100)) == 1_500_000
    assert lamports_to_usd(1_499_999, Decimal(100)) == 149_999


async def test_sol_is_refused_while_the_oracle_is_unavailable_and_usdc_still_works() -> None:
    book = PriceBook(Settings(price_per_check_usd=CHECK_USD), StaticOracle(None))
    assert await book.price("x402_deep", "USDC") == 150_000  # USDC never needs the oracle
    with pytest.raises(PriceUnavailableError):
        await book.price("x402_deep", "SOL")
    with pytest.raises(PriceUnavailableError):
        await book.value(1_500_000, "SOL")
    with pytest.raises(PriceUnavailableError):
        await PriceBook(Settings(), None).price("analysis", "SOL")


def test_format_usd() -> None:
    assert format_usd(150_000) == "$0.15"
    assert format_usd(10_000) == "$0.01"
    assert format_usd(5_000_000) == "$5.00"
    assert format_usd(1_234) == "$0.001234"


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
    assert (await oracle.read(NOW)).usd_per_sol == Decimal("89.60736310")
    await oracle.read(NOW + 2)
    assert rpc.calls == 1  # cached
    with pytest.raises(PriceUnavailableError, match="stale"):
        await oracle.read(NOW + 11)  # re-read, and now 61 s old
