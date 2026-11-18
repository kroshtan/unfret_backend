"""
The x402 service: configuration, hot wallet health and alerts, settlement, and the sweeps to cold.

Money flows through the hot wallet: payments land in its wSOL or USDC account, it pays their fees, and background
sweeps move SOL above a reserve to the cold address and USDC, as USDC, into the cold address's USDC account. The
product never swaps; converting USDC is the operator's business. Run a single instance: the sweeper is not
coordinated across processes. Credit purchases pay into the same hot wallet; their scanner (unfret.credits.scanner)
counts only incoming transfers and ignores sweeps and fee payments.

Prices come from :class:`~unfret.prices.PriceBook`: canonical dollar prices, charged exactly in USDC and converted to
wSOL at quote time with Pyth's SOL/USD price. While SOL can't be priced (oracle stale or uncertain) only USDC is
offered; while the hot wallet's USDC account doesn't exist yet, only wSOL.
"""

import asyncio
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg
import structlog
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from unfret.config import Settings
from unfret.prices import PriceBook, PriceUnavailableError, Product, PythSolUsd
from unfret.x402.breaker import SettlementBreaker
from unfret.x402.payment import (
    PAYMENT_PROGRAMS,
    PaymentError,
    Precheck,
    VerifiedPayment,
    confirm_by_simulation,
    precheck,
    requirements,
)
from unfret.x402.solana import (
    CAIP2,
    WSOL_MINT,
    RpcError,
    SettlementPendingError,
    SolanaRpc,
    TransactionFailedError,
    associated_token_address,
    usdc_mint,
)
from unfret.x402.verdict import tier_description
from unfret.x402.wallet import HotWallet, build_sweep, build_usdc_sweep, load_keypair, unexpected_outflows

logger = structlog.get_logger("unfret")

Status = Literal[
    "off", "mainnet_refused", "misconfigured", "low_balance", "compromise_suspected", "rpc_unavailable", "ok"
]

_BALANCE_TTL_S = 30.0
# Twice a blockhash's lifetime: after this a transaction can no longer land, so it can't be settled twice.
_DUPLICATE_WINDOW_S = 120.0
_AUDIT_PAGE = 100
_AUDIT_MAX_PAGES = 10
_SEEN_MAX = 2_000
_FEE_BUFFER = 10_000  # lamports kept back for the sweep transaction's own fee
_TOKEN_ACCOUNT_SIZE = 165
_META_RETRIES = 10
_MAINNET_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
_REASON_MAX = 500


class DuplicateSettlementError(RpcError):
    """The same payment transaction is already being (or was just) settled."""


class SettlementMismatchError(RpcError):
    """A payment landed, but the hot wallet's token account did not grow by exactly the amount (or fees were off)."""

    def __init__(self, signature: str, detail: str) -> None:
        """
        Remember the transaction so it can be reported and investigated.

        :param signature: The settled transaction's signature.
        :param detail: What didn't match.
        """
        super().__init__(f"{signature}: {detail}")
        self.signature = signature


@dataclass(frozen=True)
class Settlement:
    """A confirmed payment."""

    signature: str
    payer: Pubkey
    amount: int
    mint: Pubkey = WSOL_MINT


@dataclass(frozen=True)
class Flag:
    """A transaction behind a sticky alert: an unexpected outflow, an audit gap, or a post-settlement mismatch."""

    signature: str
    reason: str
    found_at: datetime


@dataclass(frozen=True)
class Health:
    """Whether payments can be accepted right now, the hot wallet's balances, and any alerts."""

    status: Status
    sol_lamports: int | None = None
    wsol_lamports: int | None = None
    alerts: tuple[str, ...] = field(default_factory=tuple)
    usdc_units: int | None = None


def _product(tier: str) -> Product:
    return "x402_deep" if tier == "deep" else "x402_quick"


class X402:
    """Pay-per-check over x402, with the hot wallet as merchant (``payTo``) and fee-paying sponsor."""

    def __init__(self, settings: Settings, rpc: SolanaRpc | None = None, prices: PriceBook | None = None) -> None:
        """
        Load the payment configuration. Missing or invalid pieces leave the feature off rather than raising.

        :param settings: The service settings.
        :param rpc: Optional RPC client (tests pass a stub).
        :param prices: Optional price book (by default dollar prices with Pyth for SOL, via ``rpc``).
        """
        self.settings = settings
        self.network = CAIP2[settings.solana_network]
        self.rpc = rpc or SolanaRpc(settings.solana_rpc_url)
        self.usdc_mint = usdc_mint(settings.solana_network, settings.usdc_mint)
        mainnet = settings.solana_network == "solana-mainnet"
        oracle_rpc = (
            SolanaRpc(settings.pyth_rpc_url)
            if settings.pyth_rpc_url
            else (self.rpc if mainnet or rpc is not None else SolanaRpc(_MAINNET_PUBLIC_RPC))
        )
        self.prices = prices or PriceBook(
            settings,
            PythSolUsd(
                oracle_rpc,
                Pubkey.from_string(settings.pyth_sol_usd_account),
                max_age_s=settings.pyth_max_age_s,
                max_conf_bps=settings.pyth_max_conf_bps,
            ),
        )
        self.hot: HotWallet | None = None
        # (hot wallet, origin) -> the hot wallet's ownership proof: signed once per key, so a new key gets a new one.
        self._proofs: dict[tuple[Pubkey, str], str] = {}
        self._config_status: Status = "ok"
        self._balances: tuple[float, int, int | None, int | None] | None = None
        self._lock = asyncio.Lock()
        # Settlements and sweeps take turns, so balance reads, simulations and post-settlement checks see only
        # their own effect on the hot wallet.
        self._wallet_lock = asyncio.Lock()
        self._settling: dict[str, float] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()  # audited, newest last; bounded
        self._audit_cursor: str | None = None  # every transaction up to this one is audited
        self._compromise: list[Flag] = []
        self._mismatches: list[Flag] = []
        # The audit's state in the database (attach): the cursor last saved, and flags a failed save left behind.
        self._pool: asyncpg.Pool | None = None
        self._saved_cursor: str | None = None
        self._unsaved: list[Flag] = []
        self._audit_lock = asyncio.Lock()
        # Former cold addresses count as cold only for transactions before the cut-off and before this process
        # started: everything this process signs goes to the current cold address, so a later outflow to a former
        # one is not ours.
        self._started = time.time()
        self._former = tuple(Pubkey.from_string(a) for a in settings.former_cold_addresses)
        until = settings.former_cold_until
        self._former_until = min(self._started, until.timestamp() if until is not None else math.inf)
        self._former_names = {str(a) for a in self._former} | {
            str(associated_token_address(a, self.usdc_mint)) for a in self._former
        }
        self.breaker = SettlementBreaker(
            settings.x402_breaker_per_payer, settings.x402_breaker_per_ip, settings.x402_breaker_global
        )
        self._last_usdc_sweep = time.monotonic()

        secret = settings.hot_wallet_key.get_secret_value()
        if not settings.cold_address or not secret:
            self._config_status = "off"
        elif settings.solana_network == "solana-mainnet" and not settings.x402_allow_mainnet:
            self._config_status = "mainnet_refused"
        else:
            try:
                hot = HotWallet(load_keypair(secret), Pubkey.from_string(settings.cold_address), self.usdc_mint)
            except ValueError as exc:
                # Never log the exception text: it could echo part of the secret.
                logger.error("x402_misconfigured", error=type(exc).__name__)
                self._config_status = "misconfigured"
            else:
                if hot.pubkey in self._former:
                    logger.error("x402_misconfigured", error="hot_wallet_is_a_former_cold_address")
                    self._config_status = "misconfigured"
                else:
                    self.hot = hot

    async def attach(self, pool: asyncpg.Pool) -> None:
        """
        Keep the outflow audit's state in the database, and pick up where the last process left it.

        Loads the audit cursor (so a restart continues from the newest audited transaction instead of reading the
        hot wallet's history again; without one, the first audit looks at the newest page only, as before) and every
        flag the operator hasn't cleared (so a suspected compromise survives a restart).

        :param pool: The database pool (migration 23).
        """
        self._pool = pool
        if self.hot is None:
            return
        hot = str(self.hot.pubkey)
        async with self._audit_lock:
            cursor = await pool.fetchval("SELECT signature FROM x402_audit_cursor WHERE hot_wallet = $1", hot)
            self._audit_cursor = self._saved_cursor = None if cursor is None else str(cursor)
            rows = await pool.fetch(
                "SELECT signature, reason, found_at FROM x402_audit_flags "
                "WHERE hot_wallet = $1 AND cleared_at IS NULL ORDER BY id",
                hot,
            )
            self._compromise = [Flag(r["signature"], r["reason"], r["found_at"]) for r in rows]
        if rows:
            logger.error("x402_outflow_alerts_restored", count=len(rows), signatures=[r["signature"] for r in rows])
        logger.info("x402_audit_resumed", cursor=self._audit_cursor is not None)

    @property
    def configured(self) -> bool:
        """Whether the hot wallet and cold address are set up (payments may still be paused)."""
        return self.hot is not None

    def ownership_proof(self, origin: str) -> str | None:
        """
        The origin signed by the hot wallet (``payTo``), for x402scan's ownership check (unfret.x402.ownership).

        :param origin: The public origin.
        :return: The proof (base58), or None while x402 is off (no hot wallet).
        """
        if self.hot is None:
            return None
        key = (self.hot.pubkey, origin)
        if (proof := self._proofs.get(key)) is None:
            proof = self._proofs[key] = self.hot.ownership_proof(origin)
        return proof

    async def price(self, tier: str, currency: Literal["SOL", "USDC"] = "SOL") -> int:
        """
        Return the price of a tier (SOL raises :class:`~unfret.prices.PriceUnavailableError` if unpriceable).

        :param tier: ``quick`` or ``deep``.
        :param currency: ``SOL`` (lamports of wSOL) or ``USDC`` (base units).
        :return: The price.
        """
        return await self.prices.price(_product(tier), currency)

    def usd_price(self, tier: str) -> int:
        """
        Return the canonical price of a tier.

        :param tier: ``quick`` or ``deep``.
        :return: Micro-dollars.
        """
        return self.prices.usd(_product(tier))

    async def requirements(self, tier: str, currency: Literal["SOL", "USDC"] = "SOL") -> dict[str, Any]:
        """
        Return the ``PaymentRequirements`` for a tier in one currency. Only valid while configured.

        :param tier: ``quick`` or ``deep``.
        :param currency: ``SOL`` (paid in wSOL) or ``USDC``.
        :return: The requirements object (SOL raises :class:`~unfret.prices.PriceUnavailableError` while the oracle
            refuses).
        """
        assert self.hot is not None  # noqa: S101
        option = requirements(
            network=self.network,
            amount=await self.price(tier, currency),
            pay_to=self.hot.pubkey,
            fee_payer=self.hot.pubkey,
            asset=WSOL_MINT if currency == "SOL" else self.usdc_mint,
        )
        # What this price buys (descriptive only: payments are matched on scheme, network, asset, payTo and amount).
        option["extra"] |= {"tier": tier, "includes": tier_description("deep" if tier == "deep" else "quick")}
        return option

    async def accepts(self, tier: str) -> list[dict[str, Any]]:
        """
        Return the ``accepts`` list for a tier: wSOL first while SOL can be priced, then USDC once it can be received.

        :param tier: ``quick`` or ``deep``.
        :return: The payment options (empty when neither is possible right now).
        """
        assert self.hot is not None  # noqa: S101
        options: list[dict[str, Any]] = []
        try:
            options.append(await self.requirements(tier, "SOL"))
        except PriceUnavailableError as exc:
            logger.info("x402_wsol_not_offered", reason=str(exc)[:120])
        try:
            _, _, usdc = await self._read_balances()
        except Exception as exc:  # noqa: BLE001 - USDC is optional; wSOL may still work
            logger.warning("x402_usdc_not_offered", error=type(exc).__name__)
            usdc = None
        if usdc is not None:  # the USDC account exists (the sweep job creates it)
            options.append(await self.requirements(tier, "USDC"))
        constraints = self.constraints(options)
        note = self.fee_cap_note(options)
        tolerance = self._tolerance()
        for option in options:
            option["extra"]["constraints"] = constraints
            option["extra"]["feeCapNote"] = note
            # How far a paid amount may be from this quote (wSOL moves with the price; USDC is exact).
            option["extra"]["quoteToleranceBps"] = tolerance.get(option["asset"], 0)
        return options

    def fee_cap_note(self, accepts: list[dict[str, Any]]) -> str:
        """
        Explain ``constraints.maxPriorityFeeLamports``, which is in lamports for every option, USDC included.

        :param accepts: The options offered for the tier.
        :return: The note (``extra.feeCapNote``).
        """
        s = self.settings
        share = f"{s.x402_max_priority_fee_bps / 100:g}%"
        if any(a["asset"] == str(WSOL_MINT) for a in accepts):
            basis = f"{share} of this tier's dollar price, converted to lamports at the current SOL/USD quote"
        else:
            basis = f"a fixed {s.x402_priority_fee_fallback_lamports} lamports while SOL can't be priced"
        return (
            "constraints.maxPriorityFeeLamports is in lamports (SOL) for every option, USDC included, because Unfret "
            f"pays the network fee in SOL: it caps the transaction's priority fee at {basis}."
        )

    def _tolerance(self) -> dict[str, int]:
        # wSOL is converted at quote time and SOL moves before the payment arrives; USDC amounts are exact.
        return {str(WSOL_MINT): self.settings.quote_tolerance_bps}

    async def _read_balances(self) -> tuple[int, int | None, int | None]:
        assert self.hot is not None  # noqa: S101
        async with self._lock:
            now = time.monotonic()
            if self._balances is None or now - self._balances[0] > _BALANCE_TTL_S:
                sol, (wsol, usdc) = await self.rpc.balances(
                    self.hot.pubkey,
                    [
                        associated_token_address(self.hot.pubkey),
                        associated_token_address(self.hot.pubkey, self.usdc_mint),
                    ],
                )
                self._balances = (now, sol, wsol, usdc)
            return self._balances[1], self._balances[2], self._balances[3]

    async def health(self) -> Health:
        """
        Report whether payments can be accepted, with the hot wallet's balances and alerts (logged at error level).

        Alerts: ``sweep_failing`` (value stays above 1.5x the sweep threshold), ``fee_reserve_low`` (SOL below
        ``x402_min_fee_lamports``, which also pauses payments), ``outflow_to_non_cold`` (a transfer from the hot
        wallet to anywhere but cold was seen on chain, which also pauses payments) and ``usdc_above_threshold`` (USDC
        piles up beyond 3x the USDC sweep threshold: USDC sweeps are failing).

        :return: The current health.
        """
        if self._config_status != "ok" or self.hot is None:
            return Health(self._config_status)
        try:
            sol, wsol, usdc = await self._read_balances()
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
        if (usdc or 0) > self.settings.usdc_sweep_threshold_units * 3:
            logger.error("x402_usdc_above_threshold", usdc_units=usdc)
            alerts.append("usdc_above_threshold")
        if self._mismatches:
            alerts.append("post_settlement_mismatch")
        if self.breaker.global_failures >= self.settings.x402_breaker_global:
            alerts.append("settlements_failing")  # payments pause through the breaker, not the status
        if self._compromise:
            alerts.append("outflow_to_non_cold")
            status = "compromise_suspected"
        return Health(status, sol, wsol, tuple(alerts), usdc)

    def precheck(self, header_value: str, tier: str, accepts: list[dict[str, Any]]) -> Precheck:
        """
        Run the network-free payment checks for a tier (see :func:`~unfret.x402.payment.precheck`).

        :param header_value: The ``PAYMENT-SIGNATURE`` header.
        :param tier: ``quick`` or ``deep``.
        :param accepts: The options currently offered (from :meth:`accepts`).
        :return: The prechecked payment.
        """
        assert self.hot is not None  # noqa: S101
        return precheck(
            header_value,
            accepts,
            fee_payer=self.hot.pubkey,
            max_compute_units=self.settings.x402_max_compute_units,
            max_compute_unit_price=self.settings.x402_max_compute_unit_price_microlamports,
            tolerance_bps=self._tolerance(),
            max_priority_fee=self.max_priority_fee(accepts),
            programs=self.programs,
        )

    def max_priority_fee(self, accepts: list[dict[str, Any]]) -> int:
        """
        The largest priority fee (lamports) a payment for this tier may make the hot wallet pay.

        :param accepts: The options offered for the tier.
        :return: A share of the tier's wSOL price, or the fallback while SOL isn't offered.
        """
        s = self.settings
        sol = next((int(a["amount"]) for a in accepts if a["asset"] == str(WSOL_MINT)), None)
        return s.x402_priority_fee_fallback_lamports if sol is None else sol * s.x402_max_priority_fee_bps // 10_000

    def constraints(self, accepts: list[dict[str, Any]]) -> dict[str, Any]:
        """
        What a payment transaction must look like, as the 402 states it (``extra.constraints``).

        :param accepts: The options offered for the tier.
        :return: The rules :meth:`precheck` and the simulation apply.
        """
        s = self.settings
        return {
            "memo": "exactly one memo instruction, 1-256 bytes (any unique text: it makes the transaction unique)",
            "maxComputeUnits": s.x402_max_compute_units,
            "maxComputeUnitPriceMicroLamports": s.x402_max_compute_unit_price_microlamports,
            "computeUnitLimitRequiredWithPrice": True,
            "maxPriorityFeeLamports": self.max_priority_fee(accepts),
            "feePayerInInstructions": False,
            "addressLookupTables": False,
            "programs": sorted(str(p) for p in self.programs),
        }

    @property
    def programs(self) -> frozenset[Pubkey]:
        """
        The programs a payment may run: the built-in wallet programs plus any configured extras.

        :return: The allowlist.
        """
        return PAYMENT_PROGRAMS | {Pubkey.from_string(p) for p in self.settings.x402_extra_programs}

    async def settle(self, pre: Precheck) -> Settlement:
        """
        Verify a prechecked payment by simulation, co-sign it as fee payer, submit it, and check what really landed.

        Runs under the wallet lock. The same transaction is settled at most once per process within the duplicate
        window: Solana deduplicates the transfer itself, but the RPC can report success to every resubmission.

        :param pre: A payment from :meth:`precheck`.
        If the transaction lands but the hot wallet's token account didn't grow by exactly the amount, the
        post-settlement check raises :class:`SettlementMismatchError` (and logs an alert).

        :return: The settlement.
        :raises DuplicateSettlementError: If this transaction is already being settled.
        :raises TransactionFailedError: If it landed and failed (the fee was paid).
        :raises SettlementPendingError: If it was submitted but not confirmed in time.
        :raises RpcError: If the network didn't accept it (the payment may then be retried).
        """
        assert self.hot is not None  # noqa: S101
        key = str(pre.transaction.signatures[1])
        # The lock covers verification and signing only: sweeps can't move the balances the simulation
        # is compared with in the meantime. Waiting for confirmation runs outside it, so payments settle in parallel.
        async with self._wallet_lock:
            now = time.monotonic()
            self._settling = {k: t for k, t in self._settling.items() if now - t < _DUPLICATE_WINDOW_S}
            if key in self._settling:
                raise DuplicateSettlementError("duplicate_settlement")
            payment = await self._simulate(pre)
            self._settling[key] = now
            raw = self.hot.sign(payment)
        try:
            signature = await self._landed(raw) or await self.rpc.send_and_confirm(
                raw, self.settings.x402_confirm_timeout_s
            )
        except TransactionFailedError, SettlementPendingError:
            raise
        except RpcError:
            # Not accepted by the network: the client may retry this payment (resending the same signed bytes is
            # idempotent on chain anyway).
            self._settling.pop(key, None)
            raise
        self._balances = None  # value and fees moved; re-read on the next health check
        await self._check_settled(signature, payment.amount, payment.max_fee, payment.mint)
        currency = "SOL" if payment.mint == WSOL_MINT else "USDC"
        logger.info("x402_settled", amount=payment.amount, currency=currency)
        return Settlement(signature=signature, payer=payment.payer, amount=payment.amount, mint=payment.mint)

    async def _simulate(self, pre: Precheck) -> VerifiedPayment:
        assert self.hot is not None  # noqa: S101
        try:
            return await confirm_by_simulation(pre, rpc=self.rpc, pay_to=self.hot.pubkey, programs=self.programs)
        except PaymentError as exc:
            if exc.reason != "wrong_amount":
                raise
            # Another payment of ours may have landed between the balance read and the simulation: read again once.
            return await confirm_by_simulation(pre, rpc=self.rpc, pay_to=self.hot.pubkey, programs=self.programs)

    async def _landed(self, raw: bytes) -> str | None:
        """
        Whether this exact transaction already landed (a retry after a restart, say), so it isn't sent again.

        :param raw: The fully signed transaction.
        :return: Its signature if it is confirmed, ``None`` if it isn't known (or the lookup failed).
        :raises TransactionFailedError: If it landed and failed.
        """
        signature = str(VersionedTransaction.from_bytes(raw).signatures[0])
        try:
            status = await self.rpc.signature_status(signature)
        except RpcError:
            return None
        if status is None:
            return None
        if status.get("err") is not None:
            raise TransactionFailedError(f"transaction failed: {status['err']}")
        return signature if status.get("confirmationStatus") in {"confirmed", "finalized"} else None

    async def _check_settled(self, signature: str, amount: int, max_fee: int, mint: Pubkey = WSOL_MINT) -> None:
        """
        Confirm from the landed transaction that the token account grew by exactly ``amount`` and fees stayed bounded.

        Uses only the transaction's own pre/post token balances, never the account balance: credit purchases land in
        the same token account at any time, so a balance difference can't be attributed. If the RPC hasn't indexed
        the transaction after the retries, the payment stands on its confirmation and the simulation (logged).

        :param signature: The settled transaction.
        :param amount: The amount paid.
        :param max_fee: The largest fee the payment allowed.
        :param mint: The paid mint.
        :raises SettlementMismatchError: On any difference (also logged as an error and kept as an alert).
        """
        assert self.hot is not None  # noqa: S101
        account = str(associated_token_address(self.hot.pubkey, mint))

        detail = ""
        for _ in range(_META_RETRIES):
            tx = await self.rpc.parsed_transaction(signature)
            if tx is not None:
                meta = tx.get("meta") or {}
                keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
                index = keys.index(account) if account in keys else -1

                def token_amount(entries: list[dict[str, Any]], index: int = index) -> int:
                    for entry in entries:
                        if entry.get("accountIndex") == index:
                            return int(entry["uiTokenAmount"]["amount"])
                    return 0

                delta = token_amount(meta.get("postTokenBalances") or []) - token_amount(
                    meta.get("preTokenBalances") or []
                )
                fee, paid = int(meta.get("fee", 0)), meta["preBalances"][0] - meta["postBalances"][0]
                if delta != amount:
                    detail = f"token account grew by {delta}, expected {amount}"
                elif fee > max_fee or paid != fee:
                    detail = f"hot wallet paid {paid} with fee {fee}, allowed {max_fee}"
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning("x402_settlement_unverified", signature=signature)
        if detail:
            logger.error("x402_post_settlement_mismatch", signature=signature, detail=detail)
            self._mismatches.append(Flag(signature, f"post-settlement mismatch: {detail}", datetime.now(UTC)))
            raise SettlementMismatchError(signature, detail)

    def _usdc_sweep_due(self, usdc: int) -> bool:
        s = self.settings
        if usdc >= s.usdc_sweep_threshold_units:
            return True
        return (
            usdc >= s.usdc_sweep_dust_units and time.monotonic() - self._last_usdc_sweep >= s.usdc_sweep_max_interval_s
        )

    async def sweep_usdc_once(self) -> str | None:
        """
        Sweep the hot wallet's USDC, as USDC, into the cold address's USDC account once it holds enough (or dust long).

        One atomic transaction: create the cold address's USDC account if it is missing (the hot wallet pays the rent)
        and transfer the whole USDC balance into it. It waits while paying that rent and the fee would take the hot
        wallet's SOL under ``x402_min_fee_lamports``; the SOL sweep's unwrapping refills it. Balances are re-read every
        time and the next sweep only starts after the previous one confirmed, so retrying is always safe.

        :return: The sweep's signature, or ``None`` if nothing was due (or SOL for the rent is short).
        """
        assert self.hot is not None  # noqa: S101
        async with self._wallet_lock:
            self._balances = None
            sol, _, usdc = await self._read_balances()
            if not usdc or not self._usdc_sweep_due(usdc):
                return None
            cold_account = associated_token_address(self.hot.cold, self.usdc_mint)
            missing = await self.rpc.token_balance(cold_account) is None
            rent = await self.rpc.rent_exempt_minimum(_TOKEN_ACCOUNT_SIZE) if missing else 0
            if sol - rent - _FEE_BUFFER < self.settings.x402_min_fee_lamports:
                logger.warning("x402_usdc_sweep_waiting_for_sol", sol_lamports=sol, rent_lamports=rent)
                return None
            sweep = build_usdc_sweep(
                self.hot.pubkey,
                self.hot.cold,
                self.usdc_mint,
                amount=usdc,
                blockhash=await self.rpc.latest_blockhash(),
            )
            raw = self.hot.sign(sweep)
            await self.rpc.simulate(raw)
            signature = await self.rpc.send_and_confirm(raw, self.settings.x402_confirm_timeout_s)
            self._balances = None
            self._last_usdc_sweep = time.monotonic()
            logger.info("x402_usdc_sweep", usdc_units=usdc, created_cold_account=missing)
            return signature

    async def sweep_once(self) -> str | None:
        """
        Sweep the hot wallet to cold if it holds more than the threshold, or maintain it otherwise.

        One atomic transaction: close the wSOL account into the hot wallet (unwrapping), recreate it, create the USDC
        account if it is missing, and send everything above the reserve to the cold address. Below the threshold it
        still creates missing token accounts (both are needed before payments in that mint can land), and unwraps
        when SOL for fees has fallen under the reserve. Balances are re-read every time and the next sweep only
        starts after the previous one confirmed, so retrying is always safe.

        :return: The sweep's signature, or ``None`` if nothing was needed.
        """
        assert self.hot is not None  # noqa: S101
        async with self._wallet_lock:
            self._balances = None
            sol, wsol, usdc = await self._read_balances()
            reserve = self.settings.sweep_reserve_lamports
            missing = (wsol is None) + (usdc is None)
            rent = await self.rpc.rent_exempt_minimum(_TOKEN_ACCOUNT_SIZE) * missing if missing else 0
            available = sol + (wsol or 0) - rent - _FEE_BUFFER
            to_cold = available - reserve
            over_threshold = sol + (wsol or 0) > self.settings.sweep_threshold_lamports
            if over_threshold and to_cold > 0:
                kind = "sweep"
            elif missing and available >= 0:
                kind, to_cold = "create_accounts", 0
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
                usdc_mint=self.usdc_mint if usdc is None else None,
            )
            raw = self.hot.sign(sweep)
            await self.rpc.simulate(raw)
            signature = await self.rpc.send_and_confirm(raw, self.settings.x402_confirm_timeout_s)
            self._balances = None
            logger.info("x402_sweep", kind=kind, to_cold_lamports=to_cold)
            return signature

    async def audit_outflows(self) -> list[str]:
        """
        Check the hot wallet's transactions for value leaving it except to cold (SOL) or cold's USDC account.

        Pages back to the last audited transaction (``until``), so a burst of payments can't push an outflow out of
        view; failed transactions are skipped without fetching them. The first run (no cursor in memory or, once
        :meth:`attach` ran, in the database) looks at the newest page only. Former cold addresses count as cold for
        transactions before their cut-off (see :meth:`_outflows`). With a database, each flag and the cursor are
        saved together; a transaction the operator already cleared for the same reason doesn't raise the alarm again.

        :return: The unexpected outflows found in transactions not checked before (that now raise the alarm).
        """
        assert self.hot is not None  # noqa: S101
        entries: list[dict[str, Any]] = []
        before: str | None = None
        flags: list[Flag] = []
        for _ in range(_AUDIT_MAX_PAGES):
            page = await self.rpc.signature_page(
                self.hot.pubkey, before=before, until=self._audit_cursor, limit=_AUDIT_PAGE, commitment="confirmed"
            )
            entries.extend(page)
            if len(page) < _AUDIT_PAGE or self._audit_cursor is None:
                break
            before = page[-1]["signature"]
        else:
            logger.error("x402_audit_backlog", pages=_AUDIT_MAX_PAGES)  # older ones are skipped, and alerted on
            flags.append(
                Flag(entries[-1]["signature"], "audit backlog: older transactions skipped", datetime.now(UTC))
            )
        contiguous = True  # everything older is audited, so the cursor may move here
        try:
            for entry in reversed(entries):  # oldest first
                signature = entry["signature"]
                if signature not in self._seen:
                    tx = None if entry.get("err") is not None else await self.rpc.parsed_transaction(signature)
                    if tx is None and entry.get("err") is None:
                        contiguous = False  # not indexed yet; look again next time
                        continue
                    issues = [] if tx is None else self._outflows(tx)
                    self._mark_seen(signature)  # only once read: one that can't be is looked at again next time
                    if issues:
                        logger.error("x402_outflow_to_non_cold", signature=signature, issues=issues)
                        now = datetime.now(UTC)
                        flags.extend(Flag(signature, issue[:_REASON_MAX], now) for issue in issues)
                if contiguous:
                    self._audit_cursor = signature
        finally:
            # Also when a lookup fails half-way (the RPC): what was found so far is marked seen, so it must raise
            # the alarm now, and the cursor keeps the progress.
            async with self._audit_lock:
                raised = await self._save(flags)
                self._compromise.extend(raised)
        return [flag.reason for flag in raised]

    def _outflows(self, tx: dict[str, Any]) -> list[str]:
        """
        The unexpected outflows in one transaction, with former cold addresses counted as cold before the cut-off.

        A former cold address counts only for a transaction whose block time is before both ``former_cold_until``
        and this process's start: the sweeps of the time it was cold. Later, an outflow to it is flagged like any
        other (a stolen hot key could drain to an abandoned or compromised old wallet), and so is one without a block
        time.

        :param tx: A ``getTransaction`` result in ``jsonParsed`` encoding.
        :return: The issues (empty if none).
        """
        assert self.hot is not None  # noqa: S101
        issues = unexpected_outflows(tx, self.hot.pubkey, self.hot.cold, self.usdc_mint)
        if not issues or not self._former:
            return issues
        block_time = tx.get("blockTime")
        if (
            isinstance(block_time, int | float)
            and not isinstance(block_time, bool)
            and block_time < self._former_until
        ):
            return unexpected_outflows(tx, self.hot.pubkey, self.hot.cold, self.usdc_mint, self._former)
        return [
            f"{issue} (a former cold address, after its cut-off)"
            if issue.rsplit(" ", 1)[-1] in self._former_names
            else issue
            for issue in issues
        ]

    async def _save(self, flags: list[Flag]) -> list[Flag]:
        """
        Save new flags and the audit cursor in one database transaction (call under the audit lock).

        Fails closed: if the database can't be written, every new flag raises the alarm in memory, the saved cursor
        stays behind them, and they are saved with the next audit.

        :param flags: Flags found by this audit.
        :return: The flags that raise the alarm: all of them, except a transaction the operator already cleared for
            the same reason.
        """
        if self._pool is None or self.hot is None:
            return flags
        pending = [*self._unsaved, *flags]
        if not pending and self._audit_cursor == self._saved_cursor:
            return flags
        hot, cursor = str(self.hot.pubkey), self._audit_cursor
        cleared: set[Flag] = set()
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                for flag in pending:
                    if await conn.fetchval(
                        "INSERT INTO x402_audit_flags (hot_wallet, signature, reason, found_at) "
                        "VALUES ($1, $2, $3, $4) "
                        "ON CONFLICT (hot_wallet, signature, reason) DO UPDATE SET reason = EXCLUDED.reason "
                        "RETURNING cleared_at IS NOT NULL",
                        hot,
                        flag.signature,
                        flag.reason,
                        flag.found_at,
                    ):
                        cleared.add(flag)
                if cursor is not None and cursor != self._saved_cursor:
                    await conn.execute(
                        "INSERT INTO x402_audit_cursor (hot_wallet, signature, updated_at) VALUES ($1, $2, NOW()) "
                        "ON CONFLICT (hot_wallet) DO UPDATE SET signature = EXCLUDED.signature, updated_at = NOW()",
                        hot,
                        cursor,
                    )
        except Exception as exc:  # noqa: BLE001 - the alarm must not depend on the database
            logger.error("x402_audit_state_unsaved", error=type(exc).__name__, flags=len(pending))
            self._unsaved = pending
            return flags
        self._unsaved, self._saved_cursor = [], cursor
        for flag in flags:
            if flag in cleared:
                logger.warning("x402_outflow_cleared_before", signature=flag.signature, issue=flag.reason)
        return [flag for flag in flags if flag not in cleared]

    def _mark_seen(self, signature: str) -> None:
        self._seen[signature] = None
        while len(self._seen) > _SEEN_MAX:
            self._seen.popitem(last=False)

    def flagged(self) -> list[Flag]:
        """
        The transactions behind the sticky alerts, oldest first: outflows (and audit gaps), then mismatches.

        :return: The flags raised and not cleared.
        """
        return [*self._compromise, *self._mismatches]

    async def clear_alerts(self) -> list[str]:
        """
        Clear the sticky alerts (suspected compromise, post-settlement mismatches) once the operator has looked.

        Also in the database, so they don't come back on a restart (nor when the same transaction is audited again).

        :return: What was cleared.
        """
        async with self._audit_lock:
            if self._pool is not None and self.hot is not None:
                await self._pool.execute(
                    "UPDATE x402_audit_flags SET cleared_at = NOW() WHERE hot_wallet = $1 AND cleared_at IS NULL",
                    str(self.hot.pubkey),
                )
            cleared = [
                *(flag.reason for flag in self._compromise),
                *(f"post-settlement mismatch {flag.signature}" for flag in self._mismatches),
            ]
            logger.warning("x402_alerts_cleared", count=len(cleared))
            self._compromise.clear()
            self._mismatches.clear()
            self._unsaved.clear()
        return cleared

    async def run_forever(self) -> None:
        """Sweep USDC, sweep SOL and audit every ``sweep_interval_s`` until cancelled; failures are logged, retried."""
        while True:
            for step, event in (
                (self.sweep_usdc_once, "x402_usdc_sweep_failed"),
                (self.sweep_once, "x402_sweep_failed"),
            ):
                try:
                    await step()
                except Exception as exc:  # noqa: BLE001
                    logger.error(event, error=type(exc).__name__, detail=str(exc)[:200])
            try:
                await self.audit_outflows()
            except Exception as exc:  # noqa: BLE001
                logger.warning("x402_audit_failed", error=type(exc).__name__)
            await asyncio.sleep(self.settings.sweep_interval_s)
