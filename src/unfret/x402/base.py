"""
x402 on Base: USDC with the ``exact`` scheme (EIP-3009), next to Solana, settled by a facilitator.

The payer signs a ``transferWithAuthorization`` of exactly the price, in USDC, to ``payTo``; the facilitator
(Coinbase's CDP by default) checks it (``/verify``) and submits it, paying the gas (``/settle``). Neither Unfret nor
the facilitator can change the amount or the recipient: both are in what the payer signed.

**Treasury.** ``payTo`` is an address Unfret controls whose key is never on the server (a hardware wallet): the
facilitator pays the gas, so nothing on Base needs a hot key, and there is no hot balance to drain. It is the cold
wallet itself, or a receiving address the operator sweeps to ``base_cold_address`` by hand (an alert asks for that
sweep once it holds more than ``base_sweep_alert_units``). The outflow audit reads the USDC contract's logs for
transfers and approvals *from* ``payTo``: anything but a transfer to ``base_cold_address`` raises the same sticky
alarm as Solana's audit (payments on Base pause, the operator lists and clears the flags), with its cursor and flags
in the same tables, keyed ``<network>:<payTo>``.

**Flow.** Unlike Solana's ``upfront`` flow, Base uses x402's default ``authorization`` flow: the payment is checked
(locally, then by the facilitator's ``/verify``) before the work, and settled only once the answer is ready. A page
that can't be read or assessed, or an analysis that fails, is therefore never charged, and nothing ever needs
refunding on Base. After settlement the transaction's receipt is read from Base itself: it must hold the USDC
``Transfer`` from the payer to ``payTo`` of exactly the price and the authorization's ``AuthorizationUsed``.
"""

import asyncio
import re
import time
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
from unfret.x402.facilitator import Facilitator, FacilitatorAnswer, FacilitatorError, facilitator_auth
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
    """Whether Base payments can be accepted right now, the receiving address's USDC and any alerts."""

    status: BaseStatus
    alerts: tuple[str, ...] = field(default_factory=tuple)
    usdc_units: int | None = None


def _reason(value: str | None) -> str:
    return _REASON.sub("", (value or "").lower())[:64] or "invalid_payment"


def _uint(value: Any) -> int | None:
    text = str(value) if isinstance(value, str | int) and not isinstance(value, bool) else ""
    return int(text) if text.isdigit() else None


class BaseX402:
    """Pay-per-check over x402 on Base: USDC into ``payTo``, settled by the facilitator."""

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
        self.pay_to = ""
        self.cold = ""
        self.usdc = ""
        self.breaker = SettlementBreaker(
            settings.x402_breaker_per_payer, settings.x402_breaker_per_ip, settings.x402_breaker_global
        )
        self._status: BaseStatus = "ok"
        self._balance: tuple[float, int] | None = None
        self._cursor: int | None = None  # every block up to this one is audited
        self._compromise: list[Flag] = []
        self._mismatches: list[Flag] = []
        self._pool: asyncpg.Pool | None = None
        self._saved_cursor: int | None = None
        self._unsaved: list[Flag] = []
        self._audit_lock = asyncio.Lock()

        if not settings.x402_base_enabled or not settings.base_pay_to:
            self._status = "off"
            return
        if settings.base_network == "base-mainnet" and not settings.x402_base_allow_mainnet:
            self._status = "mainnet_refused"
            return
        try:
            self.pay_to = parse_address(settings.base_pay_to)
            self.cold = parse_address(settings.base_cold_address) if settings.base_cold_address else self.pay_to
            self.usdc = parse_address(settings.base_usdc_address or USDC[settings.base_network])
            if self.facilitator is None:
                auth = facilitator_auth(
                    settings.base_facilitator_url,
                    settings.cdp_api_key_id,
                    settings.cdp_api_key_secret.get_secret_value(),
                )
                self.facilitator = Facilitator(
                    settings.base_facilitator_url,
                    auth,
                    timeout_s=settings.x402_confirm_timeout_s + 15,
                )
        except ValueError as exc:
            # Only our own messages: never the exception's text from parsing a secret.
            logger.error("x402_base_misconfigured", error=str(exc) if "secret" not in str(exc) else "cdp_api_key")
            self._status = "misconfigured"

    @property
    def configured(self) -> bool:
        """Whether Base payments are set up (they may still be paused)."""
        return self._status == "ok"

    @property
    def audit_key(self) -> str:
        """The audit's key in the database tables it shares with Solana's: ``<network>:<payTo>``."""
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
            self._cursor = self._saved_cursor = int(cursor) if cursor is not None and str(cursor).isdigit() else None
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
        """
        assert self.facilitator is not None  # noqa: S101
        try:
            answer = await self.facilitator.settle(self._outgoing(pre, resource, extensions), pre.requirement)
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

    async def _read_balance(self) -> int:
        now = time.monotonic()
        if self._balance is None or now - self._balance[0] > _BALANCE_TTL_S:
            self._balance = (now, await self.rpc.token_balance(self.usdc, self.pay_to))
        return self._balance[1]

    async def health(self) -> BaseHealth:
        """
        Whether Base payments can be accepted, ``payTo``'s USDC, and alerts (logged at error level).

        Alerts: ``base_sweep_due`` (``payTo`` isn't the cold wallet and holds more than ``base_sweep_alert_units``),
        ``post_settlement_mismatch``, ``settlements_failing`` (the breaker's global count) and ``outflow_to_non_cold``
        (USDC left ``payTo`` other than to the cold wallet, or ``payTo`` approved a spender: payments pause).

        :return: The current health.
        """
        if not self.configured:
            return BaseHealth(self._status)
        try:
            usdc = await self._read_balance()
        except RpcError as exc:
            logger.warning("x402_base_balance_check_failed", error=str(exc)[:120])
            return BaseHealth("rpc_unavailable")
        alerts: list[str] = []
        status: BaseStatus = "ok"
        if self.cold.lower() != self.pay_to.lower() and usdc > self.settings.base_sweep_alert_units:
            logger.error("x402_base_sweep_due", usdc_units=usdc)
            alerts.append("base_sweep_due")
        if self._mismatches:
            alerts.append("post_settlement_mismatch")
        if self.breaker.global_failures >= self.settings.x402_breaker_global:
            alerts.append("settlements_failing")
        if self._compromise:
            alerts.append("outflow_to_non_cold")
            status = "compromise_suspected"
        return BaseHealth(status, tuple(alerts), usdc)

    def _outflow(self, log: dict[str, Any]) -> str | None:
        """
        What one USDC log from ``payTo`` means: ``None`` for a sweep to cold, otherwise the issue.

        Zero-value transfers are ignored: anyone can emit one "from" any address (``transferFrom`` of 0 needs no
        allowance; address-poisoning spam), and they move nothing. A zero approval is a revocation.

        :param log: An ``eth_getLogs`` entry (Transfer or Approval with ``payTo`` as the first indexed address).
        :return: The issue, or ``None``.
        """
        topics = [str(t).lower() for t in log.get("topics") or []]
        if len(topics) != 3:
            return None
        value = word(str(log.get("data", "0x")))
        counterpart = address_from_topic(topics[2])
        if value == 0:
            return None
        if topics[0] == TRANSFER_TOPIC:
            if self.cold.lower() != self.pay_to.lower() and counterpart == self.cold.lower():
                return None
            return f"USDC transfer of {value} from payTo to {counterpart}"
        if topics[0] == APPROVAL_TOPIC:
            return f"USDC approval of {value} by payTo to spender {counterpart}"
        return None

    async def audit_outflows(self) -> list[str]:
        """
        Read the USDC logs of transfers and approvals from ``payTo`` since the last audited block.

        Reads up to ``base_audit_confirmations`` blocks behind the head, in chunks; a backlog of more than ten chunks
        is skipped and flagged (as Solana's audit flags its backlog). The first run (no cursor) reads the newest chunk.
        Flags and the cursor are saved together; a transaction the operator already cleared doesn't alarm again.

        :return: The issues found (that now raise the alarm).
        """
        head = await self.rpc.block_number() - self.settings.base_audit_confirmations
        start = self._cursor + 1 if self._cursor is not None else max(0, head - _AUDIT_CHUNK + 1)
        flags: list[Flag] = []
        now = datetime.now(UTC)
        if head - start + 1 > _AUDIT_CHUNK * _AUDIT_MAX_CHUNKS:
            skip_to = head - _AUDIT_CHUNK * _AUDIT_MAX_CHUNKS + 1
            logger.error("x402_base_audit_backlog", blocks=skip_to - start)
            flags.append(Flag(f"blocks {start}-{skip_to - 1}", "audit backlog: blocks skipped unaudited", now))
            start = skip_to
        try:
            while start <= head:
                end = min(head, start + _AUDIT_CHUNK - 1)
                logs = await self.rpc.logs(
                    self.usdc, [[TRANSFER_TOPIC, APPROVAL_TOPIC], topic_address(self.pay_to)], start, end
                )
                for log in logs:
                    issue = None if log.get("removed") else self._outflow(log)
                    if issue:
                        transaction = str(log.get("transactionHash", ""))[:100]
                        logger.error("x402_outflow_to_non_cold", transaction=transaction, issue=issue)
                        flag = Flag(transaction, issue[:_REASON_MAX], now)
                        if all((f.signature, f.reason) != (flag.signature, flag.reason) for f in flags):
                            flags.append(flag)
                self._cursor = end
                start = end + 1
        finally:
            async with self._audit_lock:
                raised = await self._save(flags)
                self._compromise.extend(raised)
        return [flag.reason for flag in raised]

    async def _save(self, flags: list[Flag]) -> list[Flag]:
        """
        Save new flags and the cursor in one database transaction (call under the audit lock); fails closed.

        :param flags: Flags found by this audit.
        :return: The flags that raise the alarm (all but ones the operator already cleared).
        """
        if self._pool is None:
            return flags
        pending = [*self._unsaved, *flags]
        if not pending and self._cursor == self._saved_cursor:
            return flags
        cursor = self._cursor
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
                        str(cursor),
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

    async def run_forever(self) -> None:
        """Audit ``payTo``'s outflows every ``sweep_interval_s`` until cancelled; failures are logged and retried."""
        while True:
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
