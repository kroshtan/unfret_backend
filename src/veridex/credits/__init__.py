"""
The credits door: accounts spend a balance of micro-dollars on analyses.

An account is a wallet that signs in with SIWS, or an API key (:mod:`veridex.credits.keys`) for callers without a
wallet.

The only stored user state is ``account → balance`` (:mod:`veridex.credits.ledger`), plus the hashes of API keys.
Credits are bought by sending SOL or USDC to the x402 hot wallet, which the scanners turn into balance (SOL at its
dollar value at crediting time); failed paid x402 checks are refunded the same way. Sign-in and API keys are off
until ``VERIDEX_SESSION_SECRET`` is set; purchases additionally need the hot wallet and running scanners.
"""

import asyncio
import time

import asyncpg
import structlog
from solders.pubkey import Pubkey

from veridex.config import Settings
from veridex.credits import ledger, siws
from veridex.credits.keys import KeyGuard
from veridex.credits.pairing import PairingStore
from veridex.credits.scanner import CreditScanner
from veridex.credits.trial import TrialLimiter, has_history
from veridex.prices import PriceBook
from veridex.x402.solana import SolanaRpc, usdc_mint

logger = structlog.get_logger("veridex")


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
        self.trials = TrialLimiter(settings.trial_per_ip_per_day, settings.trial_daily_cap)
        self.pairings = PairingStore(ttl_s=settings.pairing_ttl_s, per_ip=settings.pairing_per_ip)
        self.key_guard = KeyGuard(
            secret=self.secret,
            bits=settings.key_pow_bits,
            ttl_s=settings.key_pow_ttl_s,
            creations=TrialLimiter(settings.key_create_per_ip_per_day, settings.key_create_daily_cap),
            recoveries=TrialLimiter(settings.key_recover_per_ip_per_day, 10 * settings.key_create_daily_cap),
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
            tiers = self.settings.credit_bonus_tiers
            self.scanner = CreditScanner(self.rpc, pool, self.hot, tiers, book=self.book)
            self.usdc_scanner = CreditScanner(
                self.rpc, pool, self.hot, tiers, book=self.book, usdc_mint=self.usdc_mint
            )

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

    async def first_sign_in(self, pool: asyncpg.Pool, address: str, ip: str) -> bool:
        """
        Grant the free trial on a wallet's first sign-in, when it qualifies.

        The trial is recorded on the wallet's row (``trial_granted``), so a wallet without history yet (or turned away
        by the limits) can still get it later, also after buying credits first; each wallet gets it at most once.

        :param pool: The database pool.
        :param address: The verified wallet (base58).
        :param ip: The visitor's address, for the per-IP limit.
        :return: ``True`` if a trial was granted now.
        """
        s = self.settings
        if s.trial_checks <= 0 or await ledger.trial_used(pool, address):
            return False
        try:
            eligible = await has_history(
                self.rpc,
                address,
                min_age_days=s.trial_min_wallet_age_days,
                min_signatures=s.trial_min_signatures,
                now=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - the trial waits for the next sign-in
            logger.warning("trial_history_check_failed", error=type(exc).__name__)
            return False
        if not eligible or not self.trials.allow(ip):
            return False
        granted = await ledger.grant_trial(pool, address, s.trial_checks * s.analysis_price_usd_micros)
        if granted:
            logger.info("trial_granted", checks=s.trial_checks)
        return granted

    def check_token(self, token: str) -> str:
        """
        Verify a bearer token.

        :param token: The session token.
        :return: The wallet address.
        """
        return siws.check_token(self.secret, token)
