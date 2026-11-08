"""
The credits door: wallets sign in with SIWS and spend a lamport balance on analyses.

The only stored user state is ``pubkey → balance_lamports`` (:mod:`veridex.credits.ledger`). Credits are bought by
sending SOL to the x402 hot wallet, which the scanner turns into balance; failed paid x402 checks are refunded the
same way. Sign-in is off until ``VERIDEX_SESSION_SECRET`` is set; purchases additionally need the hot wallet and a
running scanner.
"""

import asyncio
import time

import asyncpg
import structlog
from solders.pubkey import Pubkey

from veridex.config import Settings
from veridex.credits import ledger, siws
from veridex.credits.scanner import CreditScanner
from veridex.credits.trial import TrialLimiter, has_history
from veridex.x402.solana import SolanaRpc

logger = structlog.get_logger("veridex")


class CreditsDoor:
    """Configuration and state of the credits door for one process."""

    def __init__(self, settings: Settings, rpc: SolanaRpc, hot: Pubkey | None) -> None:
        """
        Set the door up; pieces that aren't configured stay off.

        :param settings: The service settings.
        :param rpc: The Solana RPC client.
        :param hot: The x402 hot wallet (receives purchases), or ``None`` when it isn't configured.
        """
        self.settings, self.rpc, self.hot = settings, rpc, hot
        self.trials = TrialLimiter(settings.trial_per_ip_per_day, settings.trial_daily_cap)
        self.scanner: CreditScanner | None = None

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
        """Whether credits can be bought: sign-in on, a hot wallet, and a scanner that has its cursor."""
        return self.sign_in_enabled and self.scanner is not None and self.scanner.ready

    @property
    def chain_id(self) -> str:
        """``mainnet`` or ``devnet``, for the sign-in message."""
        return "mainnet" if self.settings.solana_network == "solana-mainnet" else "devnet"

    def attach(self, pool: asyncpg.Pool) -> None:
        """
        Create the scanner once a database is available (only with a hot wallet and sign-in on).

        :param pool: The database pool.
        """
        if self.hot is not None and self.sign_in_enabled:
            self.scanner = CreditScanner(self.rpc, pool, self.hot, self.settings.credit_bonus_tiers)

    async def run_forever(self) -> None:
        """Scan for purchases every ``credits_scan_interval_s`` until cancelled; failures are logged and retried."""
        assert self.scanner is not None  # noqa: S101
        while True:
            try:
                await self.scanner.scan_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("credits_scan_failed", error=type(exc).__name__, detail=str(exc)[:200])
            await asyncio.sleep(self.settings.credits_scan_interval_s)

    async def first_sign_in(self, pool: asyncpg.Pool, address: str, ip: str) -> bool:
        """
        Grant the free trial on a wallet's first sign-in, when it qualifies.

        A row is created only when the trial is granted, so a wallet without history yet (or turned away by the
        limits) can still get it later; a row means the trial is spent.

        :param pool: The database pool.
        :param address: The verified wallet (base58).
        :param ip: The visitor's address, for the per-IP limit.
        :return: ``True`` if a trial was granted now.
        """
        s = self.settings
        if s.trial_checks <= 0 or await ledger.balance(pool, address) is not None:
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
        granted = await ledger.create(pool, address, s.trial_checks * s.analysis_price_lamports)
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
