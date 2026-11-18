"""
x402 on Base: USDC with the ``exact`` scheme (EIP-3009), next to Solana, settled by a facilitator.

The payer signs a ``transferWithAuthorization`` of exactly the price, in USDC, to ``payTo``; the facilitator
(Coinbase's CDP by default) checks it (``/verify``) and submits it, paying the gas (``/settle``). Neither Unfret nor
the facilitator can change the amount or the recipient: both are in what the payer signed.

**Treasury.** As on Solana, payments go to a hot wallet and are swept to cold. ``payTo`` is the Base hot wallet: the
address of ``x402_base_hot_key``, the only key on Base the server holds. Its only use is a USDC sweep to
``base_cold_address`` (``unfret.x402.evm_wallet``), once the hot wallet holds ``base_usdc_sweep_threshold_units`` (or
dust left a day), paying the gas from the hot wallet's own small ETH balance (``fee_reserve_low`` asks for more). The
cold address is a plain public setting: swapping the cold wallet needs no new key. One sweep at a time, across
processes too (a Postgres advisory lock), and none while a transaction from the hot wallet is still unmined; each
sweep's receipt is checked on Base; failures alert and are retried with backoff.

**Audit.** Every ``sweep_interval_s`` the USDC contract's logs are read for transfers and approvals *from* the hot
wallet: anything but a transfer to cold (or a declared former cold address, before its cut-off) raises the same
sticky alarm as Solana's audit (payments on Base pause, the operator lists and clears the flags). ETH leaves an
account only through a transaction its key signs, so the audit also counts the hot wallet's nonce: every transaction
it signed must be a sweep seen in those logs, and the account must have no code (an EIP-7702 delegation). The cursor
and flags live in the same tables as Solana's, keyed ``<network>:<hot wallet>``.

**Flow.** Unlike Solana's ``upfront`` flow, Base uses x402's default ``authorization`` flow: the payment is checked
(locally, then by the facilitator's ``/verify``) before the work, and settled only once the answer is ready. A page
that can't be read or assessed, or an analysis that fails, is therefore never charged, and nothing ever needs
refunding on Base. After settlement the transaction's receipt is read from Base itself: it must hold the USDC
``Transfer`` from the payer to ``payTo`` of exactly the price and the authorization's ``AuthorizationUsed``.
"""

import asyncio
import math
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg
import structlog

from unfret.config import Settings
from unfret.x402.breaker import SettlementBreaker
from unfret.x402.evm import (
    APPROVAL_TOPIC,
    AUTHORIZATION_USED_TOPIC,
    CAIP2,
    CHAIN_IDS,
    PUBLIC_RPC,
    TRANSFER_TOPIC,
    USDC,
    USDC_DECIMALS,
    USDC_DOMAIN,
    BaseRpc,
    address_from_topic,
    is_address,
    is_bytes32,
    parse_address,
    topic_address,
    word,
)
from unfret.x402.evm_wallet import BaseHotWallet, SignedSweep, build_usdc_sweep, load_hot_key, transfer_data
from unfret.x402.facilitator import (
    Facilitator,
    FacilitatorAnswer,
    FacilitatorError,
    FacilitatorUnreachableError,
    facilitator_auth,
)
from unfret.x402.payment import X402_VERSION, PaymentError
from unfret.x402.service import Flag
from unfret.x402.solana import RpcError
from unfret.x402.verdict import tier_description

logger = structlog.get_logger("unfret")

BaseStatus = Literal["off", "mainnet_refused", "misconfigured", "compromise_suspected", "rpc_unavailable", "ok"]

MAX_TIMEOUT_S = 300  # maxTimeoutSeconds: the authorization must outlive the analysis and the settlement
_SETTLE_MARGIN_S = 60  # beyond the analysis timeout: verification, fetching the page and the settlement
_CLOCK_SKEW_S = 10
_MAX_SIGNATURE_HEX = 4_096  # a plain signature is 65 bytes; smart-wallet (ERC-1271/6492) ones are longer
_BALANCE_TTL_S = 30.0
_RECEIPT_RETRIES = 10
_AUDIT_CHUNK = 1_000  # blocks per eth_getLogs (public RPCs cap the range)
_AUDIT_MAX_CHUNKS = 10
_REASON = re.compile(r"[^a-z0-9_]")
_REASON_MAX = 500
_GAS_MARGIN = 1.3  # gas limit over the estimate
_RECEIPT_POLL_S = 2.0  # Base makes a block every 2 seconds
_SWEEP_FAILING_AFTER = 3  # consecutive failed sweep attempts before the sweep_failing alert
_SWEEP_BACKOFF_MAX_S = 3600.0
_ALREADY_KNOWN = ("already known", "known transaction", "already imported")


class BaseSweepError(RuntimeError):
    """A sweep couldn't be made, or didn't land as built (it is retried with backoff)."""


class SettlementRefusedError(RuntimeError):
    """The facilitator refused to settle, or the transaction reverted: nothing moved."""

    def __init__(self, reason: str, transaction: str = "") -> None:
        """
        Record why.

        :param reason: The facilitator's ``errorReason``.
        :param transaction: The reverted transaction, if one was sent.
        """
        super().__init__(reason)
        self.reason = reason
        self.transaction = transaction


class SettlementUnknownError(RuntimeError):
    """The settlement was broadcast but not confirmed, or the facilitator didn't answer: it may still land."""

    def __init__(self, transaction: str = "") -> None:
        """
        Record the transaction, if known.

        :param transaction: The broadcast transaction's hash (empty if the facilitator didn't answer).
        """
        super().__init__(transaction or "settlement outcome unknown")
        self.transaction = transaction


class BaseSettlementMismatchError(RuntimeError):
    """The facilitator reported a settlement that Base doesn't show as required."""

    def __init__(self, transaction: str, detail: str) -> None:
        """
        Remember the transaction for the alert.

        :param transaction: The reported transaction.
        :param detail: What didn't match.
        """
        super().__init__(f"{transaction}: {detail}")
        self.transaction = transaction


@dataclass(frozen=True)
class BasePrecheck:
    """A Base payment that passed the local checks, waiting for the facilitator."""

    payload: dict[str, Any]
    requirement: dict[str, Any]  # our option it answers
    payer: str  # authorization.from, lowercase
    nonce: str  # authorization.nonce, lowercase
    amount: int

    @property
    def key(self) -> str:
        """
        What identifies this payment for retries: one authorization can be settled only once.

        :return: ``<network>:<payer>:<nonce>``.
        """
        return f"{self.requirement['network']}:{self.payer}:{self.nonce}"


@dataclass(frozen=True)
class BaseSettlement:
    """A payment the facilitator settled (and Base confirmed), or one still pending."""

    transaction: str
    payer: str
    amount: int


@dataclass(frozen=True)
class BaseHealth:
    """Whether Base payments can be accepted right now, the hot wallet's USDC and ETH, and any alerts."""

    status: BaseStatus
    alerts: tuple[str, ...] = field(default_factory=tuple)
    usdc_units: int | None = None
    eth_wei: int | None = None


def _reason(value: str | None) -> str:
    return _REASON.sub("", (value or "").lower())[:64] or "invalid_payment"


def _parse_cursor(value: str | None) -> tuple[int | None, int | None]:
    """
    The audit cursor as saved: ``<block>:<nonce>`` (the hot wallet's nonce at that block), or a bare block.

    :param value: The saved text.
    :return: The block and the nonce (each ``None`` if absent or malformed).
    """
    block, _, nonce = (value or "").partition(":")
    if not block.isdigit():
        return None, None
    return int(block), int(nonce) if nonce.isdigit() else None


def _uint(value: Any) -> int | None:
    text = str(value) if isinstance(value, str | int) and not isinstance(value, bool) else ""
    return int(text) if text.isdigit() else None


class BaseX402:
    """Pay-per-check over x402 on Base: USDC into the hot wallet (payTo), settled by a facilitator, swept to cold."""

    def __init__(self, settings: Settings, rpc: BaseRpc | None = None, facilitator: Facilitator | None = None) -> None:
        """
        Load the configuration. Missing or invalid pieces leave Base payments off rather than raising.

        :param settings: The service settings.
        :param rpc: Optional Base RPC (tests pass a stub).
        :param facilitator: Optional facilitator client (tests pass a stub).
        """
        self.settings = settings
        self.network = CAIP2[settings.base_network]
        self.rpc = rpc or BaseRpc(settings.base_rpc_url or PUBLIC_RPC[settings.base_network])
        self.domain = USDC_DOMAIN[settings.base_network]
        self.facilitator = facilitator
        self.hot: BaseHotWallet | None = None
        self.pay_to = ""  # the hot wallet's address
        self.cold = ""
        self.usdc = ""
        self.breaker = SettlementBreaker(
            settings.x402_breaker_per_payer, settings.x402_breaker_per_ip, settings.x402_breaker_global
        )
        self._status: BaseStatus = "ok"
        self._balance: tuple[float, int, int] | None = None  # (read at, USDC, ETH)
        self._cursor: int | None = None  # every block up to this one is audited
        self._nonce: int | None = None  # the hot wallet's nonce at the cursor's block
        self._compromise: list[Flag] = []
        self._mismatches: list[Flag] = []
        self._pool: asyncpg.Pool | None = None
        self._saved_cursor: str | None = None
        self._unsaved: list[Flag] = []
        self._audit_lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()
        self._sweep_failures = 0  # consecutive
        self._next_sweep = 0.0  # monotonic: the backoff after failures
        self._last_sweep = time.monotonic()
        self._signed: dict[str, int] = {}  # sweeps this process signed: hash -> nonce (a reverted one has no log)
        # Former cold addresses count as cold only for transfers before the cut-off and before this process started:
        # everything this process signs goes to the current cold address (as on Solana).
        self._started = time.time()
        self._former = frozenset(a.lower() for a in settings.base_former_cold_addresses)
        until = settings.base_former_cold_until
        self._former_until = min(self._started, until.timestamp() if until is not None else math.inf)

        if not settings.base_configured:
            self._status = "off"
            return
        if settings.base_network == "base-mainnet" and not settings.x402_base_allow_mainnet:
            self._status = "mainnet_refused"
            return
        try:
            self.hot = self._load(settings)
        except ValueError as exc:
            # Only our own messages: never the exception's text from parsing a secret.
            logger.error("x402_base_misconfigured", error=str(exc) if "secret" not in str(exc) else "cdp_api_key")
            self._status = "misconfigured"
            self.cold = self.usdc = ""
            return
        self.pay_to = self.hot.address

    def _load(self, settings: Settings) -> BaseHotWallet:
        """
        Load the addresses, the hot key and the facilitator.

        :param settings: The service settings.
        :return: The hot wallet.
        :raises ValueError: With a message of our own (never a secret's text) if anything is invalid.
        """
        self.usdc = parse_address(settings.base_usdc_address or USDC[settings.base_network])
        self.cold = parse_address(settings.base_cold_address)
        hot = BaseHotWallet(
            load_hot_key(settings.x402_base_hot_key.get_secret_value()),
            self.cold,
            self.usdc,
            chain_id=CHAIN_IDS[settings.base_network],
            max_gas=settings.base_sweep_max_gas,
            max_fee=settings.base_max_fee_per_gas_wei,
        )
        if settings.base_pay_to and parse_address(settings.base_pay_to) != hot.address:
            raise ValueError("base_pay_to is not the hot key's address")
        if hot.address.lower() in self._former:
            raise ValueError("the hot wallet is a former cold address")
        if self.facilitator is None:
            auth = facilitator_auth(
                settings.base_facilitator_url,
                settings.cdp_api_key_id,
                settings.cdp_api_key_secret.get_secret_value(),
            )
            self.facilitator = Facilitator(
                settings.base_facilitator_url, auth, timeout_s=settings.x402_confirm_timeout_s + 15
            )
        return hot

    @property
    def configured(self) -> bool:
        """Whether Base payments are set up (they may still be paused)."""
        return self._status == "ok" and self.hot is not None

    @property
    def audit_key(self) -> str:
        """The audit's key in the database tables it shares with Solana's: ``<network>:<hot wallet>``."""
        return f"{self.network}:{self.pay_to.lower()}"

    async def attach(self, pool: asyncpg.Pool) -> None:
        """
        Keep the outflow audit's cursor and flags in the database, and pick up where the last process left them.

        :param pool: The database pool (migration 23's tables).
        """
        self._pool = pool
        if not self.configured:
            return
        async with self._audit_lock:
            cursor = await pool.fetchval(
                "SELECT signature FROM x402_audit_cursor WHERE hot_wallet = $1", self.audit_key
            )
            self._saved_cursor = None if cursor is None else str(cursor)
            self._cursor, self._nonce = _parse_cursor(self._saved_cursor)
            rows = await pool.fetch(
                "SELECT signature, reason, found_at FROM x402_audit_flags "
                "WHERE hot_wallet = $1 AND cleared_at IS NULL ORDER BY id",
                self.audit_key,
            )
            self._compromise = [Flag(r["signature"], r["reason"], r["found_at"]) for r in rows]
        if rows:
            logger.error("x402_base_outflow_alerts_restored", count=len(rows))
        logger.info("x402_base_audit_resumed", cursor=self._cursor is not None)

    def usd_price(self, tier: str) -> int:
        """
        The tier's price in micro-dollars, which is also its USDC amount (6 decimals).

        :param tier: ``quick`` or ``deep``.
        :return: Micro-dollars.
        """
        s = self.settings
        return s.x402_price_deep_usd_micros if tier == "deep" else s.x402_price_quick_usd_micros

    def requirements(self, tier: str) -> dict[str, Any]:
        """
        The ``PaymentRequirements`` for a tier in USDC on Base. Only valid while configured.

        :param tier: ``quick`` or ``deep``.
        :return: The option.
        """
        name, version = self.domain
        return {
            "scheme": "exact",
            "network": self.network,
            "amount": str(self.usd_price(tier)),
            "asset": self.usdc,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": MAX_TIMEOUT_S,
            "extra": {
                # The token's EIP-712 domain, which the payer's transferWithAuthorization signature commits to.
                "name": name,
                "version": version,
                "decimals": USDC_DECIMALS,
                "tier": tier,
                "includes": tier_description("deep" if tier == "deep" else "quick"),
            },
        }

    async def accepts(self, tier: str) -> list[dict[str, Any]]:
        """
        The Base options for a tier: USDC while Base payments are healthy, nothing otherwise.

        :param tier: ``quick`` or ``deep``.
        :return: Zero or one option.
        """
        return [self.requirements(tier)] if (await self.health()).status == "ok" else []

    def owns(self, header_payload: dict[str, Any]) -> bool:
        """
        Whether a decoded ``PAYMENT-SIGNATURE`` payload is a payment on this network.

        :param header_payload: The decoded payload.
        :return: ``True`` if its ``accepted.network`` is ours.
        """
        accepted = header_payload.get("accepted")
        return isinstance(accepted, dict) and accepted.get("network") == self.network

    def precheck(  # noqa: C901, PLR0912
        self, payload: dict[str, Any], accepts: list[dict[str, Any]], now: float | None = None
    ) -> BasePrecheck:
        """
        Every check that needs no network: the option, the recipient, the exact amount, the time window, the shape.

        :param payload: The decoded ``PaymentPayload``.
        :param accepts: The options offered right now (Base's among them).
        :param now: The current unix time (tests).
        :return: The prechecked payment.
        :raises PaymentError: With a reason, if anything does not match.
        """
        if payload.get("x402Version") != X402_VERSION:
            raise PaymentError("unsupported_x402_version")
        accepted = payload.get("accepted")
        if not isinstance(accepted, dict):
            raise PaymentError("requirements_mismatch")
        option = next(
            (
                o
                for o in accepts
                if o["network"] == self.network
                and accepted.get("network") == o["network"]
                and accepted.get("scheme") == o["scheme"]
                and str(accepted.get("asset", "")).lower() == o["asset"].lower()
                and str(accepted.get("payTo", "")).lower() == o["payTo"].lower()
                and str(accepted.get("amount")) == o["amount"]
            ),
            None,
        )
        if option is None:
            raise PaymentError("requirements_mismatch")
        if (accepted.get("extra") or {}).get("assetTransferMethod", "eip3009") != "eip3009":
            raise PaymentError("unsupported_asset_transfer_method")
        inner = payload.get("payload")
        authorization = inner.get("authorization") if isinstance(inner, dict) else None
        signature = inner.get("signature") if isinstance(inner, dict) else None
        if not isinstance(authorization, dict) or not isinstance(signature, str):
            raise PaymentError("invalid_payload", malformed=True)
        if not re.fullmatch(r"0x[0-9a-fA-F]{130,}", signature) or len(signature) > _MAX_SIGNATURE_HEX:
            raise PaymentError("invalid_exact_evm_payload_signature")
        payer, to = authorization.get("from"), authorization.get("to")
        if not is_address(payer) or not is_address(to) or not is_bytes32(authorization.get("nonce")):
            raise PaymentError("invalid_payload", malformed=True)
        if to.lower() != self.pay_to.lower():
            raise PaymentError("invalid_exact_evm_payload_recipient_mismatch")
        if payer.lower() in {self.pay_to.lower(), self.cold.lower()}:
            raise PaymentError("payer_is_pay_to")
        value = _uint(authorization.get("value"))
        if value is None or value != int(option["amount"]):
            raise PaymentError("invalid_exact_evm_payload_authorization_value_mismatch")
        valid_after, valid_before = _uint(authorization.get("validAfter")), _uint(authorization.get("validBefore"))
        if valid_after is None or valid_before is None:
            raise PaymentError("invalid_payload", malformed=True)
        clock = time.time() if now is None else now
        if valid_after > clock + _CLOCK_SKEW_S:
            raise PaymentError("invalid_exact_evm_payload_authorization_valid_after")
        if valid_before < clock + self.settings.x402_analysis_timeout_s + _SETTLE_MARGIN_S:
            # It would expire before the analysis and the settlement are done: ask for a fresh one, nothing is lost.
            raise PaymentError("invalid_exact_evm_payload_authorization_valid_before")
        return BasePrecheck(
            payload=payload,
            requirement=option,
            payer=payer.lower(),
            nonce=str(authorization["nonce"]).lower(),
            amount=value,
        )

    def _outgoing(self, pre: BasePrecheck, resource: dict[str, Any], extensions: dict[str, Any]) -> dict[str, Any]:
        """
        The payload as sent to the facilitator: the client's, with Unfret's own resource and discovery metadata.

        :param pre: The prechecked payment.
        :param resource: Unfret's ``resource`` (the facilitator catalogues it with ``extensions.bazaar``).
        :param extensions: Unfret's extensions (never the client's copy).
        :return: The payload.
        """
        payload = {k: v for k, v in pre.payload.items() if k not in {"resource", "extensions"}}
        payload["resource"] = resource
        if extensions:
            payload["extensions"] = extensions
        return payload

    async def verify(self, pre: BasePrecheck, resource: dict[str, Any], extensions: dict[str, Any]) -> None:
        """
        Ask the facilitator whether the payment is valid (signature, balance, a simulation); nothing moves.

        :param pre: A payment from :meth:`precheck`.
        :param resource: Unfret's resource description.
        :param extensions: Unfret's extensions.
        :raises PaymentError: With the facilitator's reason, if it isn't valid.
        """
        assert self.facilitator is not None  # noqa: S101
        answer = await self.facilitator.verify(self._outgoing(pre, resource, extensions), pre.requirement)
        if not answer.ok:
            raise PaymentError(_reason(answer.reason))
        if answer.payer and answer.payer.lower() != pre.payer:
            raise PaymentError("payer_mismatch")

    async def settle(self, pre: BasePrecheck, resource: dict[str, Any], extensions: dict[str, Any]) -> BaseSettlement:
        """
        Have the facilitator settle the payment, then check on Base what really moved.

        :param pre: A payment from :meth:`precheck` that passed :meth:`verify`.
        :param resource: Unfret's resource description.
        :param extensions: Unfret's extensions (``bazaar``: the facilitator lists the endpoint after a settlement).
        :return: The settlement.
        :raises SettlementRefusedError: If it was refused or reverted (nothing moved).
        :raises SettlementUnknownError: If it was broadcast but not confirmed, or the facilitator didn't answer.
        :raises FacilitatorUnreachableError: If the facilitator couldn't be reached (nothing was sent).
        """
        assert self.facilitator is not None  # noqa: S101
        try:
            answer = await self.facilitator.settle(self._outgoing(pre, resource, extensions), pre.requirement)
        except FacilitatorUnreachableError:
            raise
        except FacilitatorError as exc:
            logger.warning("x402_base_settle_unanswered", error=str(exc)[:120])
            raise SettlementUnknownError from exc
        self._log_extensions(answer)
        if not answer.ok:
            if answer.reason == "settlement_pending":
                raise SettlementUnknownError(answer.transaction)
            raise SettlementRefusedError(_reason(answer.reason), answer.transaction)
        await self._check_settled(pre, answer.transaction)
        self._balance = None
        logger.info("x402_settled", amount=pre.amount, currency="USDC", network=self.network)
        return BaseSettlement(transaction=answer.transaction, payer=pre.payer, amount=pre.amount)

    @staticmethod
    def _log_extensions(answer: FacilitatorAnswer) -> None:
        bazaar = (answer.extension_responses or {}).get("bazaar")
        if isinstance(bazaar, dict):
            logger.info(
                "x402_base_bazaar",
                status=str(bazaar.get("status"))[:20],
                reason=str(bazaar.get("rejectedReason") or "")[:200],
            )

    async def _receipt(self, transaction: str) -> dict[str, Any] | None:
        if not is_bytes32(transaction):
            return None
        for attempt in range(_RECEIPT_RETRIES):
            try:
                receipt = await self.rpc.receipt(transaction)
            except RpcError:
                receipt = None
            if receipt is not None:
                return receipt
            if attempt < _RECEIPT_RETRIES - 1:
                await asyncio.sleep(1)
        return None

    async def _check_settled(self, pre: BasePrecheck, transaction: str) -> None:
        """
        Check on Base that the settlement moved exactly the price from the payer to ``payTo``.

        The receipt must show success, the USDC ``Transfer`` and the authorization's ``AuthorizationUsed``. Without a
        receipt (our RPC hasn't indexed it), the authorization's state on chain decides: used is accepted (logged);
        unused means the facilitator claimed a settlement that didn't happen.

        :param pre: The payment.
        :param transaction: The transaction the facilitator reported.
        :raises BaseSettlementMismatchError: On any difference (also kept as an alert).
        """
        receipt = await self._receipt(transaction)
        detail = ""
        if receipt is None:
            try:
                used = await self.rpc.authorization_used(self.usdc, pre.payer, pre.nonce)
            except RpcError:
                logger.warning("x402_base_settlement_unverified", transaction=transaction)
                return
            if used:
                logger.warning("x402_base_settlement_unverified", transaction=transaction, authorization="used")
                return
            detail = "no receipt, and the authorization is unused on chain"
        elif word(str(receipt.get("status", "0x0"))) != 1:
            detail = "the transaction reverted"
        else:
            logs = [entry for entry in receipt.get("logs") or [] if isinstance(entry, dict)]
            ours = [
                (entry.get("topics") or [], str(entry.get("data", "0x")))
                for entry in logs
                if str(entry.get("address", "")).lower() == self.usdc.lower()
            ]
            moved = any(
                len(topics) == 3
                and str(topics[0]).lower() == TRANSFER_TOPIC
                and address_from_topic(str(topics[1])) == pre.payer
                and address_from_topic(str(topics[2])) == self.pay_to.lower()
                and word(data) == pre.amount
                for topics, data in ours
            )
            used = any(
                len(topics) == 3
                and str(topics[0]).lower() == AUTHORIZATION_USED_TOPIC
                and address_from_topic(str(topics[1])) == pre.payer
                and str(topics[2]).lower() == pre.nonce
                for topics, _ in ours
            )
            if not moved:
                detail = f"no USDC transfer of {pre.amount} from the payer to payTo"
            elif not used:
                detail = "the authorization wasn't used"
        if detail:
            logger.error("x402_post_settlement_mismatch", transaction=transaction, detail=detail, network=self.network)
            self._mismatches.append(Flag(transaction, f"post-settlement mismatch: {detail}", datetime.now(UTC)))
            raise BaseSettlementMismatchError(transaction, detail)

    async def _read_balances(self) -> tuple[int, int]:
        now = time.monotonic()
        if self._balance is None or now - self._balance[0] > _BALANCE_TTL_S:
            usdc = await self.rpc.token_balance(self.usdc, self.pay_to)
            eth = await self.rpc.eth_balance(self.pay_to)
            self._balance = (now, usdc, eth)
        return self._balance[1], self._balance[2]

    async def health(self) -> BaseHealth:
        """
        Whether Base payments can be accepted, the hot wallet's USDC and ETH, and alerts (logged at error level).

        Alerts: ``fee_reserve_low`` (the hot wallet's ETH, which pays the sweeps' gas, is below ``base_min_gas_wei``;
        payments go on, since the facilitator pays their gas, but sweeps wait), ``usdc_above_threshold`` (USDC piles
        up beyond 3x the sweep threshold), ``sweep_failing`` (the last three sweep attempts failed),
        ``post_settlement_mismatch``, ``settlements_failing`` (the breaker's global count) and ``outflow_to_non_cold``
        (value left the hot wallet other than as a sweep to cold, or it approved a spender: payments pause).

        :return: The current health.
        """
        if not self.configured:
            return BaseHealth(self._status)
        try:
            usdc, eth = await self._read_balances()
        except RpcError as exc:
            logger.warning("x402_base_balance_check_failed", error=str(exc)[:120])
            return BaseHealth("rpc_unavailable")
        s = self.settings
        alerts: list[str] = []
        status: BaseStatus = "ok"
        if eth < s.base_min_gas_wei:
            logger.error("x402_base_fee_reserve_low", eth_wei=eth, minimum=s.base_min_gas_wei)
            alerts.append("fee_reserve_low")
        if usdc > s.base_usdc_sweep_threshold_units * 3:
            logger.error("x402_base_usdc_above_threshold", usdc_units=usdc)
            alerts.append("usdc_above_threshold")
        if self._sweep_failures >= _SWEEP_FAILING_AFTER:
            logger.error("x402_base_sweep_failing", failures=self._sweep_failures)
            alerts.append("sweep_failing")
        if self._mismatches:
            alerts.append("post_settlement_mismatch")
        if self.breaker.global_failures >= s.x402_breaker_global:
            alerts.append("settlements_failing")
        if self._compromise:
            alerts.append("outflow_to_non_cold")
            status = "compromise_suspected"
        return BaseHealth(status, tuple(alerts), usdc, eth)

    def _sweep_due(self, usdc: int) -> bool:
        s = self.settings
        if usdc >= s.base_usdc_sweep_threshold_units:
            return True
        return (
            usdc >= s.base_usdc_sweep_dust_units
            and time.monotonic() - self._last_sweep >= s.base_usdc_sweep_max_interval_s
        )

    @asynccontextmanager
    async def _exclusive(self) -> AsyncIterator[bool]:
        """
        Hold the sweep for this hot wallet across processes (a Postgres advisory lock), if there is a database.

        :yield: Whether this process may sweep now (``False``: another one is sweeping).
        """
        if self._pool is None:
            yield True
            return
        name = f"x402_base_sweep:{self.audit_key}"
        async with self._pool.acquire() as conn:
            held = bool(await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", name))
            try:
                yield held
            finally:
                if held:
                    await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", name)

    async def sweep_once(self) -> str | None:
        """
        Sweep all of the hot wallet's USDC, as USDC, to the cold address once it holds enough (or dust long).

        One transaction, ``USDC.transfer(cold, balance)``, signed by the hot wallet and paid from its ETH. It waits
        (returns ``None``) while nothing is due, another process holds the sweep, or the ETH can't pay the gas at the
        current price. It refuses (:class:`BaseSweepError`, retried with backoff) while a transaction from the hot
        wallet is still unmined (one at a time: never a second sweep on top of the first), while gas costs more than
        ``base_max_fee_per_gas_wei``, or when the sweep didn't land as built. Balances and the nonce are read every
        time, so retrying is always safe: one nonce can only be used once.

        :return: The sweep's transaction hash (confirmed, or broadcast and still pending), or ``None``.
        :raises BaseSweepError: If it can't sweep now, or the sweep reverted or moved something else.
        :raises RpcError: If the node refused the sweep (or couldn't be reached).
        """
        assert self.hot is not None  # noqa: S101
        s = self.settings
        async with self._sweep_lock, self._exclusive() as held:
            if not held:
                logger.info("x402_base_sweep_elsewhere")
                return None
            usdc = await self.rpc.token_balance(self.usdc, self.pay_to)
            if not usdc or not self._sweep_due(usdc):
                return None
            nonce = await self.rpc.transaction_count(self.pay_to, "latest")
            if await self.rpc.transaction_count(self.pay_to, "pending") != nonce:
                raise BaseSweepError("a transaction from the hot wallet is still unmined")
            tip = await self.rpc.max_priority_fee()
            max_fee = 2 * await self.rpc.base_fee() + tip
            if max_fee > s.base_max_fee_per_gas_wei:
                raise BaseSweepError(f"gas costs {max_fee} wei, above base_max_fee_per_gas_wei")
            call = {"from": self.pay_to, "to": self.usdc, "data": transfer_data(self.cold, usdc), "value": "0x0"}
            estimate = await self.rpc.estimate_gas(call)
            if estimate > s.base_sweep_max_gas:
                raise BaseSweepError(f"the sweep would use {estimate} gas, above base_sweep_max_gas")
            gas = min(math.ceil(estimate * _GAS_MARGIN), s.base_sweep_max_gas)
            eth = await self.rpc.eth_balance(self.pay_to)
            if eth < gas * max_fee:
                logger.warning("x402_base_sweep_waiting_for_gas", eth_wei=eth, needed_wei=gas * max_fee)
                return None
            sweep = build_usdc_sweep(
                self.cold,
                self.usdc,
                chain_id=self.hot.chain_id,
                nonce=nonce,
                amount=usdc,
                gas=gas,
                max_fee_per_gas=max_fee,
                max_priority_fee_per_gas=tip,
            )
            signed = self.hot.sign(sweep)
            self._signed[signed.hash] = signed.nonce
            try:
                await self.rpc.send_raw(signed.raw)
            except RpcError as exc:
                if not any(marker in str(exc).lower() for marker in _ALREADY_KNOWN):
                    raise
            self._balance = None
            receipt = await self._await_receipt(signed.hash)
            if receipt is None:
                logger.warning("x402_base_sweep_pending", transaction=signed.hash, usdc_units=usdc)
                return signed.hash
            self._check_sweep_receipt(signed, receipt)
            self._last_sweep = time.monotonic()
            logger.info(
                "x402_base_sweep", transaction=signed.hash, usdc_units=usdc, gas_used=word(str(receipt.get("gasUsed")))
            )
            return signed.hash

    async def _await_receipt(self, transaction: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.settings.x402_confirm_timeout_s
        while True:
            try:
                receipt = await self.rpc.receipt(transaction)
            except RpcError:
                receipt = None
            if receipt is not None or time.monotonic() >= deadline:
                return receipt
            await asyncio.sleep(_RECEIPT_POLL_S)

    def _check_sweep_receipt(self, signed: SignedSweep, receipt: dict[str, Any]) -> None:
        """
        Check on Base that a sweep moved exactly its amount from the hot wallet to cold.

        :param signed: The sweep.
        :param receipt: Its receipt.
        :raises BaseSweepError: If it reverted or the transfer isn't there.
        """
        if word(str(receipt.get("status", "0x0"))) != 1:
            logger.error("x402_base_sweep_reverted", transaction=signed.hash)
            raise BaseSweepError(f"sweep {signed.hash} reverted")
        moved = any(
            str(entry.get("address", "")).lower() == self.usdc.lower()
            and [str(t).lower() for t in entry.get("topics") or []]
            == [TRANSFER_TOPIC, topic_address(self.pay_to), topic_address(self.cold)]
            and word(str(entry.get("data", "0x"))) == signed.amount
            for entry in receipt.get("logs") or []
            if isinstance(entry, dict)
        )
        if not moved:
            logger.error("x402_base_sweep_mismatch", transaction=signed.hash)
            raise BaseSweepError(f"sweep {signed.hash} didn't move {signed.amount} to cold")

    async def _block_time(self, log: dict[str, Any]) -> int | None:
        stamp = log.get("blockTimestamp")
        if isinstance(stamp, str) and stamp.startswith("0x"):
            return word(stamp)
        number = log.get("blockNumber")
        if not isinstance(number, str) or not number.startswith("0x"):
            return None
        return await self.rpc.block_timestamp(word(number))

    async def _outflow(self, log: dict[str, Any]) -> tuple[str | None, bool]:  # noqa: PLR0911
        """
        What one USDC log from the hot wallet means.

        Zero-value transfers are ignored: anyone can emit one "from" any address (``transferFrom`` of 0 needs no
        allowance; address-poisoning spam), and they move nothing. A zero approval is a revocation. A transfer to a
        former cold address counts as a sweep only before its cut-off (and before this process started), as on
        Solana; later it is flagged like any other.

        :param log: An ``eth_getLogs`` entry (Transfer or Approval with the hot wallet as the first indexed address).
        :return: The issue (``None`` if fine), and whether it is a sweep to cold (to count against the nonce).
        """
        topics = [str(t).lower() for t in log.get("topics") or []]
        if len(topics) != 3:
            return None, False
        value = word(str(log.get("data", "0x")))
        counterpart = address_from_topic(topics[2])
        if value == 0:
            return None, False
        if topics[0] == TRANSFER_TOPIC:
            if counterpart == self.cold.lower():
                return None, True
            issue = f"USDC transfer of {value} from the hot wallet to {counterpart}"
            if counterpart in self._former:
                block_time = await self._block_time(log)
                if block_time is not None and block_time < self._former_until:
                    return None, True
                return f"{issue} (a former cold address, after its cut-off)", False
            return issue, False
        if topics[0] == APPROVAL_TOPIC:
            return f"USDC approval of {value} by the hot wallet to spender {counterpart}", False
        return None, False

    async def _signed_by_hot(self, transaction: str, first: int, last: int) -> bool:
        """
        Whether a transaction was signed by the hot wallet and mined in a block range (so it used one of its nonces).

        :param transaction: The hash.
        :param first: The range's first block.
        :param last: Its last block.
        :return: ``True`` if its receipt says so.
        """
        receipt = await self.rpc.receipt(transaction)
        if receipt is None:
            return False
        block = word(str(receipt.get("blockNumber", "0x0")))
        return str(receipt.get("from", "")).lower() == self.pay_to.lower() and first <= block <= last

    async def _own_transactions(self, sweeps: set[str], baseline: int, nonce: int, first: int, last: int) -> int:
        """
        How many of the hot wallet's new nonces its own sweeps used.

        Those in the logs, and reverted ones this process signed (a reverted sweep moves nothing, so it has no log;
        after a restart it isn't known any more, and its nonce is flagged).

        :param sweeps: Transactions in the logs that moved USDC from the hot wallet to cold.
        :param baseline: The nonce at the audit's start.
        :param nonce: The nonce at its end.
        :param first: The first block audited.
        :param last: The last block audited.
        :return: The count.
        """
        own = [t for t in sweeps if await self._signed_by_hot(t, first, last)]
        reverted = [
            t
            for t, n in self._signed.items()
            if t not in sweeps and baseline <= n < nonce and await self._signed_by_hot(t, first, last)
        ]
        return len(own) + len(reverted)

    async def audit_outflows(self) -> list[str]:  # noqa: C901
        """
        Check what left the hot wallet since the last audited block: USDC, approvals, and every transaction it signed.

        Reads the USDC contract's ``Transfer`` and ``Approval`` logs from the hot wallet up to
        ``base_audit_confirmations`` blocks behind the head, in chunks; a backlog of more than ten chunks is skipped
        and flagged (as Solana's audit flags its backlog). The first run (no cursor) reads the newest chunk. Then the
        nonce: ETH (or anything else) leaves an account only through a transaction its key signs, so the hot wallet's
        nonce may grow only by the sweeps to cold in those logs (and sweeps this process signed that reverted); any
        other transaction it signed is flagged, and so is code at its address (an EIP-7702 delegation). Flags and the
        cursor (``<block>:<nonce>``) are saved together, the cursor only once the whole range is read; a transaction
        the operator already cleared doesn't alarm again.

        :return: The issues found that now raise the alarm (not those it already raises).
        """
        head = await self.rpc.block_number() - self.settings.base_audit_confirmations
        start = self._cursor + 1 if self._cursor is not None else max(0, head - _AUDIT_CHUNK + 1)
        baseline = self._nonce if self._cursor is not None else None
        flags: list[Flag] = []
        now = datetime.now(UTC)

        def flag(signature: str, reason: str) -> None:
            logger.error("x402_outflow_to_non_cold", transaction=signature, issue=reason)
            new = Flag(signature, reason[:_REASON_MAX], now)
            if all((f.signature, f.reason) != (new.signature, new.reason) for f in flags):
                flags.append(new)

        if head - start + 1 > _AUDIT_CHUNK * _AUDIT_MAX_CHUNKS:
            skip_to = head - _AUDIT_CHUNK * _AUDIT_MAX_CHUNKS + 1
            flag(f"blocks {start}-{skip_to - 1}", "audit backlog: blocks skipped unaudited")
            start, baseline = skip_to, None  # the skipped blocks' transactions can't be accounted for
        first = start
        try:
            sweeps: set[str] = set()
            while start <= head:
                end = min(head, start + _AUDIT_CHUNK - 1)
                logs = await self.rpc.logs(
                    self.usdc, [[TRANSFER_TOPIC, APPROVAL_TOPIC], topic_address(self.pay_to)], start, end
                )
                for log in logs:
                    if log.get("removed"):
                        continue
                    issue, sweep = await self._outflow(log)
                    transaction = str(log.get("transactionHash", ""))[:100]
                    if issue:
                        flag(transaction, issue)
                    elif sweep:
                        sweeps.add(transaction.lower())
                start = end + 1
            nonce = await self.rpc.transaction_count(self.pay_to, head) if head >= first else self._nonce
            if baseline is not None and nonce is not None and nonce > baseline:
                unexplained = nonce - baseline - await self._own_transactions(sweeps, baseline, nonce, first, head)
                if unexplained > 0:
                    flag(
                        f"nonce {baseline}-{nonce - 1}",
                        f"the hot wallet signed {unexplained} transaction(s) that weren't USDC sweeps to cold "
                        f"(nonces {baseline}-{nonce - 1}, blocks {first}-{head})",
                    )
            code = await self.rpc.code(self.pay_to, head)
            if code not in {"0x", ""}:
                flag(
                    "hot wallet code",
                    f"the hot wallet has code ({code[:48]}): an EIP-7702 delegation can move its ETH unsigned",
                )
            if head >= first:
                self._cursor, self._nonce = head, nonce
                self._signed = {t: n for t, n in self._signed.items() if nonce is None or n >= nonce}
        finally:
            async with self._audit_lock:
                known = {(f.signature, f.reason) for f in self._compromise}
                raised = [f for f in await self._save(flags) if (f.signature, f.reason) not in known]
                self._compromise.extend(raised)
        return [f.reason for f in raised]

    async def _save(self, flags: list[Flag]) -> list[Flag]:
        """
        Save new flags and the cursor in one database transaction (call under the audit lock); fails closed.

        :param flags: Flags found by this audit.
        :return: The flags that raise the alarm (all but ones the operator already cleared).
        """
        if self._pool is None:
            return flags
        pending = [*self._unsaved, *flags]
        cursor = None if self._cursor is None else f"{self._cursor}:{'' if self._nonce is None else self._nonce}"
        if not pending and cursor == self._saved_cursor:
            return flags
        cleared: set[Flag] = set()
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                for flag in pending:
                    if await conn.fetchval(
                        "INSERT INTO x402_audit_flags (hot_wallet, signature, reason, found_at) "
                        "VALUES ($1, $2, $3, $4) "
                        "ON CONFLICT (hot_wallet, signature, reason) DO UPDATE SET reason = EXCLUDED.reason "
                        "RETURNING cleared_at IS NOT NULL",
                        self.audit_key,
                        flag.signature,
                        flag.reason,
                        flag.found_at,
                    ):
                        cleared.add(flag)
                if cursor is not None and cursor != self._saved_cursor:
                    await conn.execute(
                        "INSERT INTO x402_audit_cursor (hot_wallet, signature, updated_at) VALUES ($1, $2, NOW()) "
                        "ON CONFLICT (hot_wallet) DO UPDATE SET signature = EXCLUDED.signature, updated_at = NOW()",
                        self.audit_key,
                        cursor,
                    )
        except Exception as exc:  # noqa: BLE001 - the alarm must not depend on the database
            logger.error(
                "x402_audit_state_unsaved", error=type(exc).__name__, flags=len(pending), network=self.network
            )
            self._unsaved = pending
            return flags
        self._unsaved, self._saved_cursor = [], cursor
        return [flag for flag in flags if flag not in cleared]

    def flagged(self) -> list[Flag]:
        """
        The transactions behind the sticky alerts, oldest first: outflows (and audit gaps), then mismatches.

        :return: The flags raised and not cleared.
        """
        return [*self._compromise, *self._mismatches]

    async def clear_alerts(self) -> list[str]:
        """
        Clear the sticky alerts once the operator has looked (in the database too).

        :return: What was cleared.
        """
        async with self._audit_lock:
            if self._pool is not None and self.configured:
                await self._pool.execute(
                    "UPDATE x402_audit_flags SET cleared_at = NOW() WHERE hot_wallet = $1 AND cleared_at IS NULL",
                    self.audit_key,
                )
            cleared = [
                *(flag.reason for flag in self._compromise),
                *(f"post-settlement mismatch {flag.signature}" for flag in self._mismatches),
            ]
            logger.warning("x402_alerts_cleared", count=len(cleared), network=self.network)
            self._compromise.clear()
            self._mismatches.clear()
            self._unsaved.clear()
        return cleared

    async def sweep_step(self) -> None:
        """
        One turn of the sweeper: sweep unless backing off after failures; a failure doubles the wait (up to an hour).

        Failures are logged at error level; three in a row raise ``sweep_failing``.
        """
        if time.monotonic() < self._next_sweep:
            return
        try:
            await self.sweep_once()
        except Exception as exc:  # noqa: BLE001
            self._sweep_failures += 1
            delay = min(self.settings.sweep_interval_s * 2 ** min(self._sweep_failures, 12), _SWEEP_BACKOFF_MAX_S)
            self._next_sweep = time.monotonic() + delay
            logger.error(
                "x402_base_sweep_failed",
                error=type(exc).__name__,
                detail=str(exc)[:200],
                failures=self._sweep_failures,
                retry_in_s=round(delay),
            )
        else:
            self._sweep_failures, self._next_sweep = 0, 0.0

    async def run_forever(self) -> None:
        """Sweep USDC to cold and audit the hot wallet every ``sweep_interval_s`` until cancelled."""
        while True:
            await self.sweep_step()
            try:
                await self.audit_outflows()
            except Exception as exc:  # noqa: BLE001
                logger.warning("x402_base_audit_failed", error=type(exc).__name__)
            await asyncio.sleep(self.settings.sweep_interval_s)

    async def aclose(self) -> None:
        """Close the RPC and facilitator connection pools (on shutdown)."""
        await self.rpc.aclose()
        if self.facilitator is not None:
            await self.facilitator.aclose()
