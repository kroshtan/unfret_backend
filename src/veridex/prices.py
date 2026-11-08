"""
Every price Veridex charges, in one place: :meth:`PriceBook.price` and the conversions built on it.

The canonical price list is in US dollars, kept as integer micro-dollars (1 USD = 1,000,000), which is also USDC's
base unit: USDC prices are exact. SOL (wSOL) amounts are derived at quote and payment time from Pyth's SOL/USD price,
read on chain through our own RPC from the Pyth receiver's ``PriceUpdateV2`` account: the update must be fully
verified, at most ``pyth_max_age_s`` old and its confidence interval at most ``pyth_max_conf_bps`` of the price.
When it isn't, SOL is refused (:class:`PriceUnavailableError`) rather than mispriced, and USDC still works.

Credit balances are micro-dollars too (:mod:`veridex.credits.ledger`), so a check keeps its value whatever SOL does.
"""

import base64
import math
import struct
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from solders.pubkey import Pubkey

from veridex.config import Settings
from veridex.x402.solana import SolanaRpc

Product = Literal["analysis", "x402_quick", "x402_deep"]
Currency = Literal["SOL", "USDC"]

LAMPORTS_PER_SOL = 1_000_000_000
USDC_UNITS = 1_000_000  # USDC has 6 decimals
MICRO_USD = 1_000_000  # canonical prices and balances are integer micro-dollars (= USDC base units)

# Pyth's Solana receiver program; it owns the sponsored push-feed accounts (shard 0).
PYTH_RECEIVER_PROGRAM = Pubkey.from_string("rec5EKMGg6MxZYaMdyBfgwp4d5rB9T1VQH5pJv5LtFJ")
# SOL/USD price feed id (https://pyth.network/developers/price-feed-ids).
SOL_USD_FEED_ID = bytes.fromhex("ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d")
_DISCRIMINATOR = 8
_FULL_VERIFICATION = 1
_CACHE_S = 5.0


class PriceUnavailableError(RuntimeError):
    """A converted price can't be given right now (oracle stale, uncertain, unreadable or invalid)."""


@dataclass(frozen=True)
class SolUsd:
    """One validated Pyth SOL/USD reading."""

    usd_per_sol: Decimal
    confidence: Decimal
    publish_time: int


def parse_price_update(data: bytes) -> tuple[bool, bytes, int, int, int, int]:
    """
    Decode a Pyth ``PriceUpdateV2`` account.

    Layout (Borsh, after the 8-byte Anchor discriminator): write authority (32), verification level (``0`` Partial
    plus a ``u8`` signature count, or ``1`` Full), then the price message: feed id (32), price ``i64``, confidence
    ``u64``, exponent ``i32``, publish time ``i64``, …

    :param data: The account data.
    :return: ``(fully_verified, feed_id, price, conf, exponent, publish_time)``.
    :raises ValueError: If the data is too short or the verification level is unknown.
    """
    offset = _DISCRIMINATOR + 32
    if len(data) < offset + 2 + 32 + 28:
        raise ValueError("price update too short")
    level = data[offset]
    if level not in (0, 1):
        raise ValueError("unknown verification level")
    offset += 1 if level == _FULL_VERIFICATION else 2
    feed_id = data[offset : offset + 32]
    price, conf, exponent, publish_time = struct.unpack_from("<qQiq", data, offset + 32)
    return level == _FULL_VERIFICATION, feed_id, price, conf, exponent, publish_time


def validate_sol_usd(data: bytes, owner: str, *, now: float, max_age_s: float, max_conf_bps: int) -> SolUsd:
    """
    Turn a raw Pyth SOL/USD account into a price, or refuse it.

    :param data: The account data.
    :param owner: The account's owner program (base58).
    :param now: The current unix time.
    :param max_age_s: The oldest acceptable publish time, in seconds.
    :param max_conf_bps: The widest acceptable confidence interval, in basis points of the price.
    :return: The reading.
    :raises PriceUnavailableError: If the account isn't Pyth's, isn't SOL/USD, isn't fully verified, is stale, is
        too uncertain or holds a non-positive price.
    """
    if owner != str(PYTH_RECEIVER_PROGRAM):
        raise PriceUnavailableError("oracle account is not owned by the Pyth receiver")
    try:
        full, feed_id, price, conf, exponent, publish_time = parse_price_update(data)
    except ValueError as exc:
        raise PriceUnavailableError(f"oracle account unreadable: {exc}") from exc
    if feed_id != SOL_USD_FEED_ID:
        raise PriceUnavailableError("oracle account is not the SOL/USD feed")
    if not full:
        raise PriceUnavailableError("oracle update is only partially verified")
    if price <= 0:
        raise PriceUnavailableError("oracle price is not positive")
    if now - publish_time > max_age_s:
        raise PriceUnavailableError(f"oracle price is stale ({int(now - publish_time)} s old)")
    if conf * 10_000 > price * max_conf_bps:
        raise PriceUnavailableError("oracle confidence interval too wide")
    scale = Decimal(10) ** exponent
    return SolUsd(usd_per_sol=Decimal(price) * scale, confidence=Decimal(conf) * scale, publish_time=publish_time)


class PythSolUsd:
    """Reads and validates Pyth's SOL/USD price through our own RPC, cached for a few seconds."""

    def __init__(self, rpc: SolanaRpc, account: Pubkey, *, max_age_s: float, max_conf_bps: int) -> None:
        """
        Configure the reader.

        :param rpc: The Solana RPC.
        :param account: The SOL/USD ``PriceUpdateV2`` account.
        :param max_age_s: The oldest acceptable price, in seconds.
        :param max_conf_bps: The widest acceptable confidence interval, in basis points.
        """
        self.rpc, self.account, self.max_age_s, self.max_conf_bps = rpc, account, max_age_s, max_conf_bps
        self._cached: tuple[float, SolUsd] | None = None

    async def read(self, now: float | None = None) -> SolUsd:
        """
        Return a validated SOL/USD price.

        :param now: The current unix time (injectable for tests).
        :return: The reading.
        :raises PriceUnavailableError: If the oracle can't be read or the reading is refused.
        """
        now = time.time() if now is None else now
        if self._cached and now - self._cached[0] < _CACHE_S:
            reading = self._cached[1]
        else:
            try:
                info = await self.rpc.call(
                    "getAccountInfo", [str(self.account), {"encoding": "base64", "commitment": "confirmed"}]
                )
            except Exception as exc:
                raise PriceUnavailableError(f"oracle unreadable: {type(exc).__name__}") from exc
            value = info.get("value") if isinstance(info, dict) else None
            if not value:
                raise PriceUnavailableError("oracle account not found")
            reading = validate_sol_usd(
                base64.b64decode(value["data"][0]),
                value["owner"],
                now=now,
                max_age_s=self.max_age_s,
                max_conf_bps=self.max_conf_bps,
            )
            self._cached = (now, reading)
        # Re-check age on every use: a cached reading ages too.
        if now - reading.publish_time > self.max_age_s:
            raise PriceUnavailableError(f"oracle price is stale ({int(now - reading.publish_time)} s old)")
        return reading


def lamports_to_usdc(lamports: int, usd_per_sol: Decimal) -> int:
    """
    The USDC price (base units) of a lamport amount, rounded up so a conversion never undercharges.

    :param lamports: The SOL amount.
    :param usd_per_sol: The SOL/USD price.
    :return: USDC base units.
    """
    return math.ceil(Decimal(lamports) * usd_per_sol * USDC_UNITS / LAMPORTS_PER_SOL)


def usdc_to_lamports(usdc: int, usd_per_sol: Decimal) -> int:
    """
    The lamport value of a USDC amount, rounded down so a conversion never over-credits.

    :param usdc: USDC base units.
    :param usd_per_sol: The SOL/USD price.
    :return: Lamports.
    """
    return math.floor(Decimal(usdc) * LAMPORTS_PER_SOL / (usd_per_sol * USDC_UNITS))


def usd_to_lamports(micro_usd: int, usd_per_sol: Decimal) -> int:
    """
    The lamports to charge for a dollar price, rounded up so a conversion never undercharges.

    :param micro_usd: The price in micro-dollars.
    :param usd_per_sol: The SOL/USD price.
    :return: Lamports.
    """
    return math.ceil(Decimal(micro_usd) * LAMPORTS_PER_SOL / (usd_per_sol * MICRO_USD))


def lamports_to_usd(lamports: int, usd_per_sol: Decimal) -> int:
    """
    The dollar value of a lamport amount, rounded down so a conversion never over-credits.

    :param lamports: The SOL amount.
    :param usd_per_sol: The SOL/USD price.
    :return: Micro-dollars.
    """
    return math.floor(Decimal(lamports) * usd_per_sol * MICRO_USD / LAMPORTS_PER_SOL)


def format_usd(micro_usd: int) -> str:
    """
    Format micro-dollars for people, e.g. ``"$0.15"`` (cents, or more places when a price needs them).

    :param micro_usd: The amount.
    :return: The formatted amount.
    """
    cents = Decimal(micro_usd) / MICRO_USD
    text = f"{cents:.2f}" if micro_usd % 10_000 == 0 else format(cents.normalize(), "f")
    return f"${text}"


class PriceBook:
    """The price list: canonical dollar prices; USDC is exact, SOL converted with the oracle."""

    def __init__(self, settings: Settings, oracle: PythSolUsd | None) -> None:
        """
        Configure the book.

        :param settings: The service settings.
        :param oracle: The SOL/USD reader (required for SOL prices and for valuing SOL).
        """
        self.settings, self.oracle = settings, oracle

    def usd(self, product: Product) -> int:
        """
        The canonical price of a product.

        :param product: What is bought.
        :return: Micro-dollars.
        """
        s = self.settings
        return {
            "analysis": s.analysis_price_usd_micros,
            "x402_quick": s.x402_price_quick_usd_micros,
            "x402_deep": s.x402_price_deep_usd_micros,
        }[product]

    async def sol_usd(self, now: float | None = None) -> SolUsd:
        """
        The validated SOL/USD price.

        :param now: The current unix time (injectable for tests).
        :return: The reading.
        :raises PriceUnavailableError: Without an oracle, or if it refuses.
        """
        if self.oracle is None:
            raise PriceUnavailableError("no price oracle configured")
        return await self.oracle.read(now)

    async def price(self, product: Product, currency: Currency, now: float | None = None) -> int:
        """
        The price of ``product`` in ``currency``: USDC base units (exact) or lamports (converted, rounded up).

        A SOL price that can't be given right now raises :class:`PriceUnavailableError`.

        :param product: What is bought.
        :param currency: ``SOL`` or ``USDC``.
        :param now: The current unix time (injectable for tests).
        :return: The price in base units.
        """
        return await self.amount(self.usd(product), currency, now)

    async def amount(self, micro_usd: int, currency: Currency, now: float | None = None) -> int:
        """
        A dollar amount in ``currency``'s base units (SOL rounded up; it can raise :class:`PriceUnavailableError`).

        :param micro_usd: The dollar amount.
        :param currency: ``SOL`` or ``USDC``.
        :param now: The current unix time (injectable for tests).
        :return: Base units.
        """
        if currency == "USDC":
            return micro_usd
        return usd_to_lamports(micro_usd, (await self.sol_usd(now)).usd_per_sol)

    def converted(self, currency: Currency) -> bool:
        """
        Whether ``currency``'s prices come from the oracle (and so may drift between quote and payment).

        :param currency: ``SOL`` or ``USDC``.
        :return: ``True`` for SOL.
        """
        return currency == "SOL"

    async def value(self, amount: int, currency: Currency, now: float | None = None) -> int:
        """
        The dollar value of a payment, e.g. to credit a purchase (SOL rounded down).

        SOL that can't be valued right now raises :class:`PriceUnavailableError`.

        :param amount: The amount paid, in the currency's base units.
        :param currency: ``SOL`` or ``USDC``.
        :param now: The current unix time (injectable for tests).
        :return: Micro-dollars.
        """
        if currency == "USDC":
            return amount
        return lamports_to_usd(amount, (await self.sol_usd(now)).usd_per_sol)
