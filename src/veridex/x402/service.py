"""
The x402 service: configuration, hot wallet health and alerts, settlement, the USDC swap, and the sweep to cold.

Money flows through the hot wallet: payments land in its wSOL or USDC account, it pays their fees, USDC is swapped
to SOL (:mod:`veridex.x402.swap`), and a background sweep moves everything above a reserve to the cold address, which
only ever receives SOL. Run a single instance: the sweeper is not coordinated across processes. Credit purchases pay
into the same hot wallet; their scanner (veridex.credits.scanner) counts only incoming transfers and ignores sweeps,
swaps and fee payments.

Prices come from :class:`~veridex.prices.PriceBook`: wSOL at the canonical SOL price, USDC converted at quote time.
While USDC can't be priced (oracle stale or uncertain) or the hot wallet's USDC account doesn't exist yet, only wSOL
is offered.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from solders.pubkey import Pubkey

from veridex.config import Settings
from veridex.prices import PriceBook, PriceUnavailableError, Product, PythSolUsd, usdc_to_lamports
from veridex.x402.payment import Precheck, confirm_by_simulation, precheck, requirements
from veridex.x402.solana import CAIP2, WSOL_MINT, RpcError, SolanaRpc, associated_token_address, usdc_mint
from veridex.x402.swap import JupiterClient, SwapError, verify_swap
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
_META_RETRIES = 10
_SWAP_FAILURES_ALERT = 3
_MAINNET_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
# The audit recognises a swap only at no worse than this fraction of the oracle rate (a compromise signal).
_AUDIT_RATE_FRACTION = 0.5


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
        :param prices: Optional price book (by default canonical SOL prices with Pyth for USDC, via ``rpc``).
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
        # Jupiter routes mainnet tokens only; on devnet received USDC stays put.
        self.swaps_enabled = mainnet
        self.prices = prices or PriceBook(
            settings,
            PythSolUsd(
                oracle_rpc,
                Pubkey.from_string(settings.pyth_sol_usd_account),
                max_age_s=settings.pyth_max_age_s,
                max_conf_bps=settings.pyth_max_conf_bps,
            ),
        )
        self.swapper = JupiterClient(settings.swap_api_url, settings.swap_api_key.get_secret_value())
        self.hot: HotWallet | None = None
        self._config_status: Status = "ok"
        self._balances: tuple[float, int, int | None, int | None] | None = None
        self._lock = asyncio.Lock()
        # Settlements, swaps and sweeps take turns, so balance reads, simulations and post-settlement checks see only
        # their own effect on the hot wallet.
        self._wallet_lock = asyncio.Lock()
        self._settling: dict[str, float] = {}
        self._seen: set[str] = set()
        self._compromise: list[str] = []
        self._mismatches: list[str] = []
        self._swap_failures = 0
        self._last_swap = time.monotonic()
        self._audit_rate_floor: float | None = None  # lamports per USDC unit, from the last oracle reading

        secret = settings.hot_wallet_key.get_secret_value()
        if not settings.cold_address or not secret:
            self._config_status = "off"
        elif settings.solana_network == "solana-mainnet" and not settings.x402_allow_mainnet:
            self._config_status = "mainnet_refused"
        else:
            try:
                self.hot = HotWallet(load_keypair(secret), Pubkey.from_string(settings.cold_address), self.usdc_mint)
            except ValueError as exc:
                # Never log the exception text: it could echo part of the secret.
                logger.error("x402_misconfigured", error=type(exc).__name__)
                self._config_status = "misconfigured"

    @property
    def configured(self) -> bool:
        """Whether the hot wallet and cold address are set up (payments may still be paused)."""
        return self.hot is not None

    async def price(self, tier: str, currency: Literal["SOL", "USDC"] = "SOL") -> int:
        """
        Return the price of a tier (USDC raises :class:`~veridex.prices.PriceUnavailableError` if unpriceable).

        :param tier: ``quick`` or ``deep``.
        :param currency: ``SOL`` (lamports of wSOL) or ``USDC`` (base units).
        :return: The price.
        """
        return await self.prices.price(_product(tier), currency)

    def requirements(self, tier: str) -> dict[str, Any]:
        """
        Return the wSOL ``PaymentRequirements`` for a tier (always offered). Only valid while configured.

        :param tier: ``quick`` or ``deep``.
        :return: The requirements object.
        """
        assert self.hot is not None  # noqa: S101
        return requirements(
            network=self.network,
            amount=self.prices.lamports(_product(tier)),
            pay_to=self.hot.pubkey,
            fee_payer=self.hot.pubkey,
        )

    async def accepts(self, tier: str) -> list[dict[str, Any]]:
        """
        Return the ``accepts`` list for a tier: wSOL first, then USDC while it can be priced and received.

        :param tier: ``quick`` or ``deep``.
        :return: The payment options.
        """
        assert self.hot is not None  # noqa: S101
        options = [self.requirements(tier)]
        try:
            _, _, usdc = await self._read_balances()
            if usdc is None:
                return options  # the USDC account doesn't exist yet; the sweep job creates it
            amount = await self.price(tier, "USDC")
        except PriceUnavailableError as exc:
            logger.info("x402_usdc_not_offered", reason=str(exc)[:120])
            return options
        except Exception as exc:  # noqa: BLE001 - USDC is optional; wSOL still works
            logger.warning("x402_usdc_not_offered", error=type(exc).__name__)
            return options
        options.append(
            requirements(
                network=self.network,
                amount=amount,
                pay_to=self.hot.pubkey,
                fee_payer=self.hot.pubkey,
                asset=self.usdc_mint,
            )
        )
        return options

    def _tolerance(self) -> dict[str, int]:
        return {str(self.usdc_mint): self.settings.usdc_quote_tolerance_bps}

    async def _read_balances(self) -> tuple[int, int | None, int | None]:
        assert self.hot is not None  # noqa: S101
        async with self._lock:
            now = time.monotonic()
            if self._balances is None or now - self._balances[0] > _BALANCE_TTL_S:
                sol = await self.rpc.get_balance(self.hot.pubkey)
                wsol = await self.rpc.token_balance(associated_token_address(self.hot.pubkey))
                usdc = await self.rpc.token_balance(associated_token_address(self.hot.pubkey, self.usdc_mint))
                self._balances = (now, sol, wsol, usdc)
            return self._balances[1], self._balances[2], self._balances[3]

    async def health(self) -> Health:
        """
        Report whether payments can be accepted, with the hot wallet's balances and alerts (logged at error level).

        Alerts: ``sweep_failing`` (value stays above 1.5x the sweep threshold), ``fee_reserve_low`` (SOL below
        ``x402_min_fee_lamports``, which also pauses payments), ``outflow_to_non_cold`` (a transfer from the hot
        wallet to anywhere but cold was seen on chain, which also pauses payments), ``swap_failing`` (USDC swaps
        failed repeatedly) and ``usdc_above_threshold`` (USDC piles up beyond 3x the swap threshold).

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
        if self._swap_failures >= _SWAP_FAILURES_ALERT:
            alerts.append("swap_failing")
        if self.swaps_enabled and (usdc or 0) > self.settings.swap_threshold_usdc * 3:
            logger.error("x402_usdc_above_threshold", usdc_units=usdc)
            alerts.append("usdc_above_threshold")
        if self._mismatches:
            alerts.append("post_settlement_mismatch")
        if self._compromise:
            alerts.append("outflow_to_non_cold")
            status = "compromise_suspected"
        return Health(status, sol, wsol, tuple(alerts), usdc)

    def precheck(self, header_value: str, tier: str, accepts: list[dict[str, Any]] | None = None) -> Precheck:
        """
        Run the network-free payment checks for a tier (see :func:`~veridex.x402.payment.precheck`).

        :param header_value: The ``PAYMENT-SIGNATURE`` header.
        :param tier: ``quick`` or ``deep``.
        :param accepts: The options currently offered (from :meth:`accepts`); wSOL only by default.
        :return: The prechecked payment.
        """
        assert self.hot is not None  # noqa: S101
        return precheck(
            header_value,
            accepts if accepts is not None else [self.requirements(tier)],
            fee_payer=self.hot.pubkey,
            max_compute_units=self.settings.x402_max_compute_units,
            max_compute_unit_price=self.settings.x402_max_compute_unit_price_microlamports,
            tolerance_bps=self._tolerance(),
        )

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
        """
        assert self.hot is not None  # noqa: S101
        key = str(pre.transaction.signatures[1])
        async with self._wallet_lock:
            now = time.monotonic()
            self._settling = {k: t for k, t in self._settling.items() if now - t < _DUPLICATE_WINDOW_S}
            if key in self._settling:
                raise DuplicateSettlementError("duplicate_settlement")
            account = associated_token_address(self.hot.pubkey, pre.mint)
            payment = await confirm_by_simulation(pre, rpc=self.rpc, pay_to=self.hot.pubkey)
            before = await self.rpc.token_balance(account)
            self._settling[key] = now

            raw = self.hot.sign(payment)
            signature = await self.rpc.send_and_confirm(raw, self.settings.x402_confirm_timeout_s)
            self._balances = None  # value and fees moved; re-read on the next health check
            await self._check_settled(signature, payment.amount, payment.max_fee, before or 0, payment.mint)
        currency = "SOL" if payment.mint == WSOL_MINT else "USDC"
        logger.info("x402_settled", amount=payment.amount, currency=currency)
        return Settlement(signature=signature, payer=payment.payer, amount=payment.amount, mint=payment.mint)

    async def _check_settled(  # noqa: PLR0913
        self, signature: str, amount: int, max_fee: int, before: int, mint: Pubkey = WSOL_MINT
    ) -> None:
        """
        Confirm from the landed transaction that the token account grew by exactly ``amount`` and fees stayed bounded.

        Uses the transaction's own pre/post token balances; if the RPC hasn't indexed it yet, falls back to the
        account balance (exact, since settlements hold the wallet lock).

        :param signature: The settled transaction.
        :param amount: The amount paid.
        :param max_fee: The largest fee the payment allowed.
        :param before: The token account balance before submission.
        :param mint: The paid mint.
        :raises SettlementMismatchError: On any difference (also logged as an error and kept as an alert).
        """
        assert self.hot is not None  # noqa: S101
        token_account = associated_token_address(self.hot.pubkey, mint)
        account = str(token_account)
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
            delta = (await self.rpc.token_balance(token_account) or 0) - before
            if delta != amount:
                detail = f"token account grew by {delta}, expected {amount} (balance fallback)"
        if detail:
            logger.error("x402_post_settlement_mismatch", signature=signature, detail=detail)
            self._mismatches.append(signature)
            raise SettlementMismatchError(signature, detail)

    def _swap_due(self, usdc: int) -> bool:
        s = self.settings
        if usdc >= s.swap_threshold_usdc:
            return True
        return usdc >= s.swap_dust_usdc and time.monotonic() - self._last_swap >= s.swap_max_interval_s

    async def swap_once(self) -> str | None:
        """
        Swap the hot wallet's USDC to wSOL once it holds enough (or has held dust too long).

        The minimum output is the stricter of the route's slippage bound and a floor from Pyth's SOL/USD price; no
        swap runs while the oracle is unavailable. The transaction from the swap API is verified by simulation
        (:func:`~veridex.x402.swap.verify_swap`) before the hot wallet signs it; the output lands in its wSOL account
        for the next sweep.

        :return: The swap's signature, or ``None`` if nothing was due.
        :raises SwapError: If a due swap could not be done (counted towards the ``swap_failing`` alert).
        """
        assert self.hot is not None  # noqa: S101
        if not self.swaps_enabled:
            return None
        s = self.settings
        async with self._wallet_lock:
            self._balances = None
            _, wsol, usdc = await self._read_balances()
            if not usdc or wsol is None or not self._swap_due(usdc):
                return None
            try:
                reading = await self.prices.sol_usd()
                expected = usdc_to_lamports(usdc, reading.usd_per_sol)
                self._audit_rate_floor = expected / usdc * _AUDIT_RATE_FRACTION
                floor = expected * (10_000 - s.swap_oracle_tolerance_bps) // 10_000
                quote = await self.swapper.quote(self.usdc_mint, WSOL_MINT, usdc, s.swap_max_slippage_bps)
                quoted = int(quote["outAmount"])
                if quoted < floor:
                    raise SwapError("quote below the oracle floor")
                min_out = max(floor, quoted * (10_000 - s.swap_max_slippage_bps) // 10_000)
                raw = await self.swapper.transaction(
                    quote, self.hot.pubkey, associated_token_address(self.hot.pubkey), s.swap_max_priority_lamports
                )
                swap = await verify_swap(
                    raw,
                    rpc=self.rpc,
                    hot=self.hot.pubkey,
                    usdc_mint=self.usdc_mint,
                    amount_in=usdc,
                    min_out=min_out,
                    max_compute_units=s.swap_max_compute_units,
                    max_priority_lamports=s.swap_max_priority_lamports,
                )
                signature = await self.rpc.send_and_confirm(self.hot.sign(swap), s.x402_confirm_timeout_s)
            except (PriceUnavailableError, SwapError, RpcError, KeyError, ValueError) as exc:
                self._swap_failures += 1
                raise SwapError(f"swap not done: {type(exc).__name__}: {str(exc)[:120]}") from exc
            self._balances = None
            self._swap_failures, self._last_swap = 0, time.monotonic()
            logger.info("x402_swap", usdc_units=usdc, min_lamports=min_out)
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
        Check the hot wallet's recent transactions for value leaving it except to cold or in a USDC→SOL swap.

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
            issues = unexpected_outflows(tx, self.hot.pubkey, self.hot.cold, self.usdc_mint, self._audit_rate_floor)
            if issues:
                logger.error("x402_outflow_to_non_cold", signature=signature, issues=issues)
                self._compromise.extend(issues)
                found.extend(issues)
        return found

    async def run_forever(self) -> None:
        """Swap, sweep and audit every ``sweep_interval_s`` until cancelled; failures are logged and retried."""
        while True:
            for step, event in ((self.swap_once, "x402_swap_failed"), (self.sweep_once, "x402_sweep_failed")):
                try:
                    await step()
                except Exception as exc:  # noqa: BLE001
                    logger.error(event, error=type(exc).__name__, detail=str(exc)[:200])
            try:
                await self.audit_outflows()
            except Exception as exc:  # noqa: BLE001
                logger.warning("x402_audit_failed", error=type(exc).__name__)
            await asyncio.sleep(self.settings.sweep_interval_s)
