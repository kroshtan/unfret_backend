"""
The credits door: accounts spend a balance of micro-dollars on analyses.

An account is a wallet that signs in with SIWS, or an API key (:mod:`unfret.credits.keys`) for callers without a
wallet.

The only stored user state is ``account → balance`` (:mod:`unfret.credits.ledger`) and whether the account redeemed
an invite (:mod:`unfret.credits.invites`), plus the hashes of API keys.
Credits are bought by sending SOL or USDC to the x402 hot wallet, which the scanners turn into balance (SOL at its
dollar value at crediting time); failed paid x402 checks are refunded the same way. Sign-in and API keys are off
until ``UNFRET_SESSION_SECRET`` is set; purchases additionally need the hot wallet and running scanners.
"""

import asyncio
import time

import asyncpg
import structlog
from solders.pubkey import Pubkey

from unfret.config import Settings
from unfret.credits import ledger, siws
from unfret.credits.keys import KeyGuard
from unfret.credits.pairing import PairingStore
from unfret.credits.scanner import CreditScanner
from unfret.prices import PriceBook
from unfret.visitors import DailyLimit
from unfret.x402.solana import SolanaRpc, usdc_mint

logger = structlog.get_logger("unfret")


class CreditsDoor:
    """Configuration and state of the credits door for one process."""

    def __init__(self, settings: Settings, rpc: SolanaRpc, hot: Pubkey | None, book: PriceBook | None = None) -> None:
        """
        Set the door up; pieces that aren't configured stay off.

        :param settings: The service settings.
        :param rpc: The Solana RPC client.
        :param hot: The x402 hot wallet (receives purchases), or ``None`` when it isn't configured.
        :param book: The price book (the x402 service's; one without an oracle by default).
        """
        self.settings, self.rpc, self.hot = settings, rpc, hot
        self.book = book or PriceBook(settings, None)
        self.usdc_mint = usdc_mint(settings.solana_network, settings.usdc_mint)
        self.pairings = PairingStore(ttl_s=settings.pairing_ttl_s, per_ip=settings.pairing_per_ip)
        self.key_guard = KeyGuard(
            secret=self.secret,
            bits=settings.key_pow_bits,
            ttl_s=settings.key_pow_ttl_s,
            creations=DailyLimit(settings.key_create_per_ip_per_day, settings.key_create_daily_cap),
            recoveries=DailyLimit(settings.key_recover_per_ip_per_day, 10 * settings.key_create_daily_cap),
        )
        self.scanner: CreditScanner | None = None
        self.usdc_scanner: CreditScanner | None = None

    @property
    def secret(self) -> str:
        """The session secret (empty while sign-in is off)."""
        return self.settings.session_secret.get_secret_value()

    @property
    def sign_in_enabled(self) -> bool:
        """Whether wallets can sign in (and so spend credits)."""
        return bool(self.secret)

    @property
    def purchases_enabled(self) -> bool:
        """Whether credits can be bought: sign-in on, a hot wallet, and a SOL scanner that has its cursor."""
        return self.sign_in_enabled and self.scanner is not None and self.scanner.ready

    @property
    def usdc_purchases_enabled(self) -> bool:
        """Whether credits can also be bought with USDC (its scanner has its cursor too)."""
        return self.purchases_enabled and self.usdc_scanner is not None and self.usdc_scanner.ready

    def attach(self, pool: asyncpg.Pool) -> None:
        """
        Create the scanner once a database is available (only with a hot wallet and sign-in on).

        :param pool: The database pool.
        """
        if self.hot is not None and self.sign_in_enabled:
            s = self.settings
            self.scanner = CreditScanner(self.rpc, pool, self.hot, s, book=self.book)
            self.usdc_scanner = CreditScanner(self.rpc, pool, self.hot, s, book=self.book, usdc_mint=self.usdc_mint)

    async def run_forever(self) -> None:
        """Scan for purchases every ``credits_scan_interval_s`` until cancelled; failures are logged and retried."""
        assert self.scanner is not None  # noqa: S101
        scanners = [s for s in (self.scanner, self.usdc_scanner) if s is not None]
        while True:
            for scanner in scanners:
                try:
                    await scanner.scan_once()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "credits_scan_failed",
                        currency=scanner.currency,
                        error=type(exc).__name__,
                        detail=str(exc)[:200],
                    )
            await asyncio.sleep(self.settings.credits_scan_interval_s)

    def check_token(self, token: str) -> str:
        """
        Verify a bearer token.

        :param token: The session token.
        :return: The wallet address.
        """
        return siws.check_token(self.secret, token)
