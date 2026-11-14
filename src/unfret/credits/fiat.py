"""
Fiat top-ups for API keys through a Merchant of Record (Polar: :mod:`unfret.credits.polar`).

A provider does two things: start a hosted checkout for a pack at its card price in US dollars, and turn its signed
webhooks into :class:`FiatEvent` s: an order paid, refunded (possibly partly, possibly more than once) or disputed.
Everything about the buyer (card, email, name, address, invoice, tax) stays with the provider: a webhook is reduced to
the few fields of :class:`FiatEvent` on receipt, and nothing else from it is stored or logged.

Which key an order is for: our account id goes into the checkout's metadata, and we also keep the checkout id →
account mapping ourselves (``fiat_checkouts``), since a provider may not echo checkout metadata on the order. The
mapping wins when both are present.

What a payment buys: the value of the pack whose card price it paid (:func:`unfret.credits.packs.card_credits`;
the amount is the order's amount before the provider's sales tax/VAT). The provider is the seller of record and
handles the tax, so no VAT record is kept here. A paid pack also issues its invites (:mod:`unfret.credits.invites`)
in the crediting transaction, for the buyer's page to collect once with its claim token.

``fiat_payments`` keeps one row per order: the account, the amount, the credits granted and how much was taken back.
It makes crediting idempotent (providers retry webhooks), lets a refund or chargeback debit the right account, and
answers support questions. Rows are deleted after ``fiat_retention_days``.

Refunds debit credits pro rata to the refunded share of the order; a dispute debits all of the order's credits.
Either may push the balance below zero, which blocks paid use until the account is topped up again.

Webhooks are verified the Standard Webhooks way (Polar uses it): an HMAC-SHA256 over ``id.timestamp.body`` with the
``whsec_`` secret, and a timestamp within ``fiat_webhook_tolerance_s``.

:class:`unfret.credits.polar.PolarProvider` is the real provider; it is built at start-up only when it is fully
configured, and ``app.fiat`` stays ``None`` (fiat off) otherwise. :class:`FakeFiatProvider` stands in for it in
tests.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlencode

import asyncpg

from unfret.config import Settings
from unfret.credits import invites, keys, ledger
from unfret.credits.packs import Pack, card_credits, pack_for_card

USD_MICROS_PER_CENT = 10_000


class FiatWebhookError(ValueError):
    """A webhook was not authentic, too old, or not understood."""


class FiatProviderError(RuntimeError):
    """The provider could not start a checkout (unreachable, refused, or misconfigured)."""


@dataclass(frozen=True)
class FiatCheckout:
    """A hosted checkout the buyer is sent to."""

    checkout_id: str
    url: str


@dataclass(frozen=True)
class FiatEvent:
    """What a webhook says about an order, and nothing else."""

    kind: Literal["paid", "refunded", "disputed"]
    provider: str
    order_id: str
    amount_cents: int = 0
    refunded_cents: int = 0  # refunded: the total refunded on the order so far
    checkout_id: str | None = None
    account: str | None = None  # from the checkout metadata, when the provider echoes it
    site: str | None = None  # the site that made the checkout (checkout metadata), when the provider echoes it


class FiatProvider(Protocol):
    """A Merchant of Record's hosted checkout and its webhooks."""

    name: str

    async def create_checkout(self, account: str, pack: Pack, return_url: str) -> FiatCheckout:
        """
        Start a hosted checkout.

        :param account: The ledger account to credit (``key:<id>``), for the checkout's metadata.
        :param pack: The pack bought, charged its card price in US dollars.
        :param return_url: Where the provider sends the buyer afterwards.
        :return: The checkout's id and page. Raises :class:`FiatProviderError` if the provider can't start one.
        """
        ...

    async def aclose(self) -> None:
        """Release the provider's connections."""
        ...

    def parse_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> FiatEvent | None:
        """
        Verify and read a webhook, keeping only the fields of :class:`FiatEvent`.

        :param headers: The request headers (lower-case names).
        :param body: The raw body, exactly as received (signatures cover the bytes).
        :param now: Current time, for the replay window.
        :return: The event, or ``None`` for event types that change nothing. Raises :class:`FiatWebhookError` if
            the signature, the timestamp or the body is not acceptable.
        """
        ...


def _secret_bytes(secret: str) -> bytes:
    return base64.b64decode(secret.removeprefix("whsec_"))


def sign_standard_webhook(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    """
    A Standard Webhooks signature header value.

    :param secret: The ``whsec_…`` secret.
    :param msg_id: The ``webhook-id``.
    :param timestamp: The ``webhook-timestamp`` (Unix seconds).
    :param body: The raw body.
    :return: ``v1,<base64 HMAC-SHA256>``.
    """
    mac = hmac.new(_secret_bytes(secret), f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify_standard_webhook(
    secret: str, headers: Mapping[str, str], body: bytes, *, now: float, tolerance_s: float
) -> None:
    """
    Check a Standard Webhooks signature and that the message is recent (replays of old messages are refused).

    :param secret: The ``whsec_…`` secret.
    :param headers: The request headers (lower-case names).
    :param body: The raw body.
    :param now: Current time.
    :param tolerance_s: How far the timestamp may be from now.
    :raises FiatWebhookError: If the signature doesn't match or the timestamp is outside the window.
    """
    msg_id, stamp = headers.get("webhook-id", ""), headers.get("webhook-timestamp", "")
    try:
        timestamp = int(stamp)
    except ValueError as exc:
        raise FiatWebhookError("Missing timestamp.") from exc
    if not msg_id or abs(now - timestamp) > tolerance_s:
        raise FiatWebhookError("Stale or incomplete webhook.")
    expected = sign_standard_webhook(secret, msg_id, timestamp, body).encode()
    offered = [s.encode() for s in headers.get("webhook-signature", "").split()]
    if not any(hmac.compare_digest(expected, s) for s in offered):
        raise FiatWebhookError("Bad signature.")


class FakeFiatProvider:
    """
    A stand-in provider for tests, shaped like Polar: Standard Webhooks signatures and ``order.*`` events.

    Events: ``order.paid`` and ``order.refunded`` (``data``: ``id``, ``checkout_id``, ``metadata.account``,
    ``total_amount`` and ``refunded_amount`` in cents, plus a ``customer`` block that is dropped), and
    ``order.disputed`` (Polar has no dispute event: :mod:`unfret.credits.polar` reads a lost dispute from its refund
    events).
    """

    name = "fake"
    _KINDS: dict[str, Literal["paid", "refunded", "disputed"]] = {
        "order.paid": "paid",
        "order.refunded": "refunded",
        "order.disputed": "disputed",
    }

    def __init__(self, secret: str, tolerance_s: float = 300) -> None:
        """
        Set the provider up.

        :param secret: The ``whsec_…`` webhook secret.
        :param tolerance_s: The replay window.
        """
        self.secret, self.tolerance_s = secret, tolerance_s

    async def create_checkout(self, account: str, pack: Pack, return_url: str) -> FiatCheckout:
        """See :meth:`FiatProvider.create_checkout`."""
        checkout_id = str(uuid.uuid4())
        query = urlencode(
            {"id": checkout_id, "amount": pack.card_usd_micros // USD_MICROS_PER_CENT, "return": return_url}
        )
        return FiatCheckout(checkout_id, f"https://fiat.invalid/checkout?{query}")

    async def aclose(self) -> None:
        """See :meth:`FiatProvider.aclose`."""

    def webhook(self, event: dict[str, object], now: float | None = None) -> tuple[bytes, dict[str, str]]:
        """
        A signed webhook as the provider would send it (the test side of :meth:`parse_webhook`).

        :param event: The event body.
        :param now: The timestamp to sign.
        :return: The body and headers.
        """
        body, msg_id, stamp = json.dumps(event).encode(), f"msg_{uuid.uuid4().hex}", int(now or time.time())
        headers = {
            "webhook-id": msg_id,
            "webhook-timestamp": str(stamp),
            "webhook-signature": sign_standard_webhook(self.secret, msg_id, stamp, body),
            "content-type": "application/json",
        }
        return body, headers

    def parse_webhook(self, headers: Mapping[str, str], body: bytes, now: float) -> FiatEvent | None:
        """See :meth:`FiatProvider.parse_webhook`."""
        verify_standard_webhook(self.secret, headers, body, now=now, tolerance_s=self.tolerance_s)
        try:
            event = json.loads(body)
            kind = self._KINDS.get(event["type"])
            if kind is None:
                return None
            data = event["data"]
            metadata = data.get("metadata") or {}
            return FiatEvent(
                kind=kind,
                provider=self.name,
                order_id=str(data["id"]),
                amount_cents=int(data.get("total_amount") or 0),
                refunded_cents=int(data.get("refunded_amount") or 0),
                checkout_id=str(data["checkout_id"]) if data.get("checkout_id") else None,
                account=str(metadata["account"]) if metadata.get("account") else None,
                site=str(metadata["site"]) if metadata.get("site") else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise FiatWebhookError("Unreadable event.") from exc


# ── The ledger side ───────────────────────────────────────────────────────────────────────────────────────────────

Outcome = Literal[
    "credited", "duplicate", "debited", "unchanged", "unknown_order", "unknown_account", "too_small", "other_site"
]


async def remember_checkout(
    db: asyncpg.Pool,
    provider: str,
    checkout: FiatCheckout,
    account: str,
    *,
    checks: int | None = None,
    claim_hash: bytes | None = None,
) -> None:
    """
    Keep which account a checkout is for, in case the order's webhook doesn't echo the metadata.

    :param db: The database pool.
    :param provider: The provider's name.
    :param checkout: The checkout.
    :param account: The account (``key:<id>``).
    :param checks: The pack size, for the buyer's status page.
    :param claim_hash: The hash of the buyer's claim token, which collects the order's invites
        (:mod:`unfret.credits.invites`).
    """
    await db.execute(
        "INSERT INTO fiat_checkouts (provider, checkout_id, account, checks, claim_hash) VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT DO NOTHING",
        provider,
        checkout.checkout_id,
        account,
        checks,
        claim_hash,
    )


async def _account_for(conn: asyncpg.Connection, event: FiatEvent, site: str) -> str | Literal["other_site"] | None:
    """
    The account an order credits: our own checkout mapping first, else the checkout metadata, if this site made it.

    Dev and production can share one provider account, whose webhooks then reach both. Only the site that made the
    checkout may credit it: elsewhere the metadata's ``key:<id>`` names no key, and crediting it would create a balance
    nobody holds and a payment row for an order that isn't this site's.

    :param conn: A connection.
    :param event: The paid order.
    :param site: This site (``public_base_url``).
    :return: The account; ``"other_site"`` for another site's order; ``None`` if nothing names one.
    """
    if event.checkout_id:
        mapped = await conn.fetchval(
            "SELECT account FROM fiat_checkouts WHERE provider = $1 AND checkout_id = $2",
            event.provider,
            event.checkout_id,
        )
        if mapped is not None:
            return str(mapped)
    if not event.account or not keys.is_key_account(event.account):
        return None
    return event.account if event.site == site else "other_site"


async def _paid(conn: asyncpg.Connection, event: FiatEvent, settings: Settings) -> Outcome:
    account = await _account_for(conn, event, settings.public_base_url)
    if account == "other_site":
        return "other_site"
    if account is None or event.amount_cents <= 0:
        return "unknown_account"
    credits = card_credits(settings, event.amount_cents)
    if credits <= 0:
        return "too_small"
    fresh = await conn.fetchval(
        "INSERT INTO fiat_payments (provider, order_id, account, amount_cents, credited_usd_micros) "
        "VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING RETURNING TRUE",
        event.provider,
        event.order_id,
        account,
        event.amount_cents,
        credits,
    )
    if not fresh:
        return "duplicate"
    await ledger.credit(conn, account, credits, purchase=True)
    if event.checkout_id:  # marks the checkout paid; invites only for exactly a pack's card price
        matched = pack_for_card(settings, event.amount_cents)
        checks = matched.checks if matched else 0
        await invites.issue_for_checkout(conn, settings, event.provider, event.checkout_id, checks)
    return "credited"


async def _taken_back(conn: asyncpg.Connection, event: FiatEvent) -> Outcome:
    row = await conn.fetchrow(
        "SELECT * FROM fiat_payments WHERE provider = $1 AND order_id = $2 FOR UPDATE", event.provider, event.order_id
    )
    if row is None:
        return "unknown_order"
    credited, amount = row["credited_usd_micros"], row["amount_cents"]
    disputed = row["disputed"] or event.kind == "disputed"
    refunded = row["refunded_cents"]
    if event.kind == "refunded":
        refunded = max(refunded, min(event.refunded_cents, amount))
    target = credited if disputed else credited * refunded // amount
    delta = max(target - row["debited_usd_micros"], 0)
    await conn.execute(
        "UPDATE fiat_payments SET refunded_cents = $3, disputed = $4, debited_usd_micros = debited_usd_micros + $5 "
        "WHERE provider = $1 AND order_id = $2",
        event.provider,
        event.order_id,
        refunded,
        disputed,
        delta,
    )
    if not delta:
        return "unchanged"
    await ledger.charge(conn, row["account"], delta)
    return "debited"


async def apply_event(db: asyncpg.Pool, event: FiatEvent, settings: Settings) -> Outcome:
    """
    Apply a webhook event to the ledger, idempotently.

    ``paid`` credits the value of the pack the order paid for, once per order. ``refunded`` debits the
    refunded share of those credits (``refunded_cents`` is the order's total refunded so far, so retries and
    successive partial refunds add up correctly). ``disputed`` debits all of them. Debits may make the balance
    negative.

    :param db: The database pool.
    :param event: The event.
    :param settings: The service settings (the packs and their card prices).
    :return: What happened.
    """
    async with db.acquire() as conn, conn.transaction():
        if event.kind == "paid":
            return await _paid(conn, event, settings)
        return await _taken_back(conn, event)


async def purge(db: asyncpg.Pool, retention_days: int, now: datetime | None = None) -> None:
    """
    Delete payment rows past their retention, checkout mappings after a month (orders follow within minutes).

    Invite codes a buyer never collected are wiped after ``invites.UNCLAIMED_DAYS``.

    :param db: The database pool.
    :param retention_days: Days to keep payment rows.
    :param now: Current time (injectable for tests).
    """
    today = (now or datetime.now(UTC)).date()
    await db.execute("DELETE FROM fiat_payments WHERE created_on < $1", today - timedelta(days=retention_days))
    await db.execute("DELETE FROM fiat_checkouts WHERE created_on < $1", today - timedelta(days=30))
    await db.execute(
        "UPDATE fiat_checkouts SET invite_codes = NULL WHERE invite_codes IS NOT NULL AND created_on < $1",
        today - timedelta(days=invites.UNCLAIMED_DAYS),
    )
