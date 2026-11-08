"""
The x402 service: configuration, hot wallet health and alerts, settlement, and the sweep to cold.

Money flows through the hot wallet: payments land in its wSOL account, it pays their fees, and a background sweep
moves everything above a reserve to the cold address. Run a single instance: the sweeper is not coordinated across
processes. Credits (phase 4) will pay into the same hot wallet; their scanner must count only incoming transfers and
ignore these sweeps.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from solders.pubkey import Pubkey

from veridex.config import Settings
from veridex.x402.payment import VerifiedPayment, requirements
from veridex.x402.solana import CAIP2, RpcError, SolanaRpc, associated_token_address
from veridex.x402.wallet import HotWallet, build_sweep, load_keypair, unexpected_outflows

logger = structlog.get_logger("veridex")

Status = Literal[
    "off", "mainnet_refused", "misconfigured", "low_balance", "compromise_suspected", "rpc_unavailable", "ok"
]

_BALANCE_TTL_S = 30.0
# Twice a blockhash's lifetime: after this a transaction can no longer land, so it can't be settled twice.
_DUPLICATE_WINDOW_S = 120.0
_FEE_BUFFER = 10_000  # lamports kept back for the sweep transaction's own fee
_TOKEN_ACCOUNT_SIZE = 165


class DuplicateSettlementError(RpcError):
    """The same payment transaction is already being (or was just) settled."""


@dataclass(frozen=True)
class Health:
    """Whether payments can be accepted right now, the hot wallet's balances, and any alerts."""

    status: Status
    sol_lamports: int | None = None
    wsol_lamports: int | None = None
    alerts: tuple[str, ...] = field(default_factory=tuple)


class X402:
    """Pay-per-check over x402, with the hot wallet as merchant (``payTo``) and fee-paying sponsor."""

    def __init__(self, settings: Settings, rpc: SolanaRpc | None = None) -> None:
        """
        Load the payment configuration. Missing or invalid pieces leave the feature off rather than raising.

        :param settings: The service settings.
        :param rpc: Optional RPC client (tests pass a stub).
        """
        self.settings = settings
        self.network = CAIP2[settings.solana_network]
        self.rpc = rpc or SolanaRpc(settings.solana_rpc_url)
        self.hot: HotWallet | None = None
        self._config_status: Status = "ok"
        self._balances: tuple[float, int, int | None] | None = None
        self._lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()
        self._settling: dict[str, float] = {}
        self._seen: set[str] = set()
        self._compromise: list[str] = []

        secret = settings.hot_wallet_key.get_secret_value()
        if not settings.cold_address or not secret:
            self._config_status = "off"
        elif settings.solana_network == "solana-mainnet" and not settings.x402_allow_mainnet:
            self._config_status = "mainnet_refused"
        else:
            try:
                self.hot = HotWallet(load_keypair(secret), Pubkey.from_string(settings.cold_address))
            except ValueError as exc:
                # Never log the exception text: it could echo part of the secret.
                logger.error("x402_misconfigured", error=type(exc).__name__)
                self._config_status = "misconfigured"

    @property
    def configured(self) -> bool:
        """Whether the hot wallet and cold address are set up (payments may still be paused)."""
        return self.hot is not None

    def price(self, tier: str) -> int:
        """
        Return the price of a tier in lamports.

        :param tier: ``quick`` or ``deep``.
        :return: The price.
        """
        return self.settings.x402_price_deep_lamports if tier == "deep" else self.settings.x402_price_quick_lamports

    def requirements(self, tier: str) -> dict[str, Any]:
        """
        Return the ``PaymentRequirements`` for a tier. Only valid while configured.

        :param tier: ``quick`` or ``deep``.
        :return: The requirements object.
        """
        assert self.hot is not None  # noqa: S101
        return requirements(
            network=self.network, amount=self.price(tier), pay_to=self.hot.pubkey, fee_payer=self.hot.pubkey
        )

    async def _read_balances(self) -> tuple[int, int | None]:
        assert self.hot is not None  # noqa: S101
        async with self._lock:
            now = time.monotonic()
            if self._balances is None or now - self._balances[0] > _BALANCE_TTL_S:
                sol = await self.rpc.get_balance(self.hot.pubkey)
                wsol = await self.rpc.token_balance(associated_token_address(self.hot.pubkey))
                self._balances = (now, sol, wsol)
            return self._balances[1], self._balances[2]

    async def health(self) -> Health:
        """
        Report whether payments can be accepted, with the hot wallet's balances and alerts (logged at error level).

        Alerts: ``sweep_failing`` (value stays above 1.5x the sweep threshold), ``fee_reserve_low`` (SOL below
        ``x402_min_fee_lamports``, which also pauses payments), ``outflow_to_non_cold`` (a transfer from the hot
        wallet to anywhere but cold was seen on chain, which also pauses payments).

        :return: The current health.
        """
        if self._config_status != "ok" or self.hot is None:
            return Health(self._config_status)
        try:
            sol, wsol = await self._read_balances()
        except Exception as exc:  # noqa: BLE001
            logger.warning("x402_balance_check_failed", error=type(exc).__name__)
            return Health("rpc_unavailable")
        alerts: list[str] = []
        if sol + (wsol or 0) > self.settings.sweep_threshold_lamports * 1.5:
            logger.error("x402_sweep_failing", hot_lamports=sol + (wsol or 0))
            alerts.append("sweep_failing")
        status: Status = "ok"
        if sol < self.settings.x402_min_fee_lamports:
            logger.error("x402_fee_reserve_low", sol_lamports=sol, minimum=self.settings.x402_min_fee_lamports)
            alerts.append("fee_reserve_low")
            status = "low_balance"
        if self._compromise:
            alerts.append("outflow_to_non_cold")
            status = "compromise_suspected"
        return Health(status, sol, wsol, tuple(alerts))

    async def settle(self, payment: VerifiedPayment) -> str:
        """
        Co-sign a verified payment as fee payer, simulate it, submit it and wait for confirmation.

        The same transaction is settled at most once per process within the duplicate window: Solana deduplicates
        the transfer itself, but the RPC can report success to every resubmission.

        :param payment: A payment from :func:`~veridex.x402.payment.verify_payment`.
        :return: The confirmed transaction signature.
        :raises DuplicateSettlementError: If this transaction is already being settled.
        """
        assert self.hot is not None  # noqa: S101
        key = str(payment.transaction.signatures[1])
        now = time.monotonic()
        self._settling = {k: t for k, t in self._settling.items() if now - t < _DUPLICATE_WINDOW_S}
        if key in self._settling:
            raise DuplicateSettlementError("duplicate_settlement")
        self._settling[key] = now

        raw = self.hot.sign(payment)
        await self.rpc.simulate(raw)
        signature = await self.rpc.send_and_confirm(raw, self.settings.x402_confirm_timeout_s)
        self._balances = None  # value and fees moved; re-read on the next health check
        logger.info("x402_settled", amount_lamports=payment.amount)
        return signature

    async def sweep_once(self) -> str | None:
        """
        Sweep the hot wallet to cold if it holds more than the threshold, or maintain it otherwise.

        One atomic transaction: close the wSOL account into the hot wallet (unwrapping), recreate it, and send
        everything above the reserve to the cold address. Below the threshold it still (re)creates a missing wSOL
        account, and unwraps when SOL for fees has fallen under the reserve. Balances are re-read every time and the
        next sweep only starts after the previous one confirmed, so retrying is always safe.

        :return: The sweep's signature, or ``None`` if nothing was needed.
        """
        assert self.hot is not None  # noqa: S101
        async with self._sweep_lock:
            self._balances = None
            sol, wsol = await self._read_balances()
            reserve = self.settings.sweep_reserve_lamports
            if wsol is None:
                available = sol - await self.rpc.rent_exempt_minimum(_TOKEN_ACCOUNT_SIZE) - _FEE_BUFFER
            else:
                available = sol + wsol - _FEE_BUFFER
            to_cold = available - reserve
            over_threshold = sol + (wsol or 0) > self.settings.sweep_threshold_lamports
            if over_threshold and to_cold > 0:
                kind = "sweep"
            elif wsol is None and available >= 0:
                kind, to_cold = "create_account", 0
            elif wsol and sol < reserve:
                kind, to_cold = "unwrap", 0
            else:
                return None
            sweep = build_sweep(
                self.hot.pubkey,
                self.hot.cold,
                ata_exists=wsol is not None,
                to_cold=to_cold,
                blockhash=await self.rpc.latest_blockhash(),
            )
            raw = self.hot.sign(sweep)
            await self.rpc.simulate(raw)
            signature = await self.rpc.send_and_confirm(raw, self.settings.x402_confirm_timeout_s)
            self._balances = None
            logger.info("x402_sweep", kind=kind, to_cold_lamports=to_cold)
            return signature

    async def audit_outflows(self) -> list[str]:
        """
        Check the hot wallet's recent transactions for transfers to anywhere but cold (a compromise signal).

        :return: The unexpected outflows found in transactions not checked before.
        """
        assert self.hot is not None  # noqa: S101
        found: list[str] = []
        for signature in await self.rpc.recent_signatures(self.hot.pubkey):
            if signature in self._seen:
                continue
            tx = await self.rpc.parsed_transaction(signature)
            if tx is None:
                continue  # not indexed yet; look again next time
            self._seen.add(signature)
            issues = unexpected_outflows(tx, self.hot.pubkey, self.hot.cold)
            if issues:
                logger.error("x402_outflow_to_non_cold", signature=signature, issues=issues)
                self._compromise.extend(issues)
                found.extend(issues)
        return found

    async def run_forever(self) -> None:
        """Sweep and audit every ``sweep_interval_s`` until cancelled; failures are logged and retried."""
        while True:
            try:
                await self.sweep_once()
            except Exception as exc:  # noqa: BLE001
                logger.error("x402_sweep_failed", error=type(exc).__name__, detail=str(exc)[:200])
            try:
                await self.audit_outflows()
            except Exception as exc:  # noqa: BLE001
                logger.warning("x402_audit_failed", error=type(exc).__name__)
            await asyncio.sleep(self.settings.sweep_interval_s)
