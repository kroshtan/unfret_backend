"""
VAT on crypto credit purchases: which rate applies, the binding price quote, and the sales records.

Card purchases are sold by the Merchant of Record, which charges and remits their VAT/sales tax; nothing here applies
to them. Crypto purchases are sold by us, so we charge VAT and keep a record of each sale.

**Which rate.** The buyer states their country at checkout. It is prefilled from Cloudflare's ``CF-IPCountry``
header, but only for a request that reached our edge from Cloudflare (Caddy passes the peer it saw on as
``X-Edge-Peer``; it is checked against Cloudflare's published ranges, vendored in ``cloudflare_ips.json``). The
visitor's IP address is never stored or logged. If the stated country differs from Cloudflare's, the sale records
``country_mismatch``. Under ``vat_mode="nl_under_threshold"`` (the default, while EU cross-border sales stay below
the €10,000 threshold) every EU country pays the Dutch rate and every other country none; under ``"per_country"``
each EU country pays its own standard rate, from ``vat_rates.json`` (vendored from the EU's TEDB by
``scripts/update_vat_rates.py``; no network calls at request time).

**The quote.** ``/v1/credits/purchase-tx`` and the Solana Pay link price a pack for a country and store that as a
quote (a random id; the pack, country, rate, net and VAT, and who to credit), valid ``vat_quote_ttl_s``. The
transaction's memo is ``unfret:quote:<id>``, so the scanner credits the right account and records the right VAT
for whatever actually arrives. A transfer without a (known) quote, a manual one, is treated conservatively: country
``unknown`` at the Dutch rate, ``net = received / (1 + rate)``.

**The record.** Each crypto credit purchase leaves one ``crypto_sales`` row: day, country, mismatch flag, net, rate,
VAT, asset, amount, transaction signature. No IP and no wallet (the transaction on chain already shows who paid).
Kept ``vat_records_retention_days`` (ten years) for the bookkeeping; :func:`sales_csv` exports a quarter.
"""

import csv
import io
import ipaddress
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any

import asyncpg
from solders.keypair import Keypair

from unfret.config import Settings

HOME_COUNTRY = "NL"
UNKNOWN = "unknown"
QUOTE_MEMO_PREFIX = "unfret:quote:"
_DATA = Path(__file__).resolve().parent
_QUOTE_ID_CHARS = "abcdefghijklmnopqrstuvwxyz234567"
QUOTE_ID_PATTERN = r"^[a-z2-7]{20}$"


@cache
def standard_rates() -> dict[str, Decimal]:
    """
    Each EU member state's standard VAT rate in percent (vendored from TEDB).

    :return: ISO 3166 code → percent.
    """
    table = json.loads((_DATA / "vat_rates.json").read_text())
    return {code: Decimal(rate) for code, rate in table["standard_rates"].items()}


def rates_checked_on() -> date:
    """
    When the vendored rates were last compared with TEDB.

    :return: The day.
    """
    return date.fromisoformat(json.loads((_DATA / "vat_rates.json").read_text())["checked_on"])


@cache
def _cloudflare() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    ranges = json.loads((_DATA / "cloudflare_ips.json").read_text())["ranges"]
    return [ipaddress.ip_network(r) for r in ranges]


def normalise_country(value: str | None) -> str | None:
    """
    A country as an upper-case ISO 3166 alpha-2 code (Greece's ``EL`` as ``GR``), or ``None`` if it isn't one.

    :param value: What the buyer or Cloudflare gave.
    :return: The code, or ``None``.
    """
    code = (value or "").strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha() or code in {"XX", "T1"}:
        return None  # Cloudflare uses XX for unknown and T1 for Tor
    return "GR" if code == "EL" else code


def edge_country(headers: Mapping[str, str]) -> str | None:
    """
    Cloudflare's idea of the visitor's country, if the request really came through Cloudflare.

    :param headers: The request headers (lower-case names).
    :return: The country code, or ``None`` (not via Cloudflare, or unknown).
    """
    try:
        peer = ipaddress.ip_address(headers.get("x-edge-peer", "").strip())
    except ValueError:
        return None
    if not any(peer in network for network in _cloudflare()):
        return None
    return normalise_country(headers.get("cf-ipcountry"))


def is_eu(country: str) -> bool:
    """
    Whether ``country`` is an EU member state.

    :param country: An ISO 3166 code.
    :return: ``True`` for the 27 member states.
    """
    return country in standard_rates()


def rate_bps(settings: Settings, country: str) -> int:
    """
    The VAT rate a buyer in ``country`` pays on a crypto purchase, in basis points.

    :param settings: The service settings.
    :param country: An ISO 3166 code, or ``unknown`` (the Dutch rate: the conservative choice).
    :return: E.g. 2100 for 21%.
    """
    rates = standard_rates()
    if country == UNKNOWN:
        return int(rates[HOME_COUNTRY] * 100)
    if not is_eu(country):
        return 0
    chosen = rates[HOME_COUNTRY] if settings.vat_mode == "nl_under_threshold" else rates[country]
    return int(chosen * 100)


def effective_rates(settings: Settings) -> dict[str, str]:
    """
    The rate each EU country pays now (others pay none), in percent as text, for ``/v1/pricing``.

    :param settings: The service settings.
    :return: ISO code → percent.
    """
    return {code: format(Decimal(rate_bps(settings, code)) / 100, "f") for code in standard_rates()}


def vat_on(net_usd_micros: int, bps: int) -> int:
    """
    The VAT on a net amount, rounded half up to the micro-dollar.

    :param net_usd_micros: The net amount.
    :param bps: The rate.
    :return: Micro-dollars.
    """
    return (net_usd_micros * bps + 5_000) // 10_000


def split_gross(gross_usd_micros: int, bps: int) -> tuple[int, int]:
    """
    Split an amount received, VAT included, into net and VAT.

    :param gross_usd_micros: What arrived, in micro-dollars.
    :param bps: The rate.
    :return: ``(net, vat)``; net is rounded down, so the account is never credited more than was paid net.
    """
    net = gross_usd_micros * 10_000 // (10_000 + bps)
    return net, gross_usd_micros - net


@dataclass(frozen=True)
class Quote:
    """A priced crypto purchase: what the buyer pays for, where, and who gets the credits."""

    id: str
    checks: int
    country: str
    country_mismatch: bool
    vat_rate_bps: int
    net_usd_micros: int
    vat_usd_micros: int
    beneficiary: str | None  # a wallet or key:<id>; None credits whoever pays
    reference: str | None = None  # the Solana Pay reference (base58) the transaction carries; None for older quotes

    @property
    def gross_usd_micros(self) -> int:
        """What the buyer pays, VAT included."""
        return self.net_usd_micros + self.vat_usd_micros

    @property
    def memo(self) -> str:
        """The memo the purchase transaction carries."""
        return f"{QUOTE_MEMO_PREFIX}{self.id}"


def price_quote(
    settings: Settings, checks: int, net_usd_micros: int, country: str, *, edge: str | None, beneficiary: str | None
) -> Quote:
    """
    Price a pack for a buyer in ``country`` (not stored yet: see :func:`save_quote`).

    :param settings: The service settings.
    :param checks: The pack size.
    :param net_usd_micros: The pack's price before VAT.
    :param country: The buyer's stated country (normalised).
    :param edge: Cloudflare's country for the request, if known.
    :param beneficiary: Who to credit (``None``: whoever pays).
    :return: The quote.
    """
    bps = rate_bps(settings, country)
    return Quote(
        id="".join(secrets.choice(_QUOTE_ID_CHARS) for _ in range(20)),
        checks=checks,
        country=country,
        country_mismatch=edge is not None and edge != country,
        vat_rate_bps=bps,
        net_usd_micros=net_usd_micros,
        vat_usd_micros=vat_on(net_usd_micros, bps),
        beneficiary=beneficiary,
        # A fresh public key nobody holds the private key of: only this purchase's transaction will carry it.
        reference=str(Keypair().pubkey()),
    )


async def save_quote(db: asyncpg.Pool, quote: Quote, *, claim_hash: bytes | None = None) -> None:
    """
    Store a quote so the scanner can apply it to the payment.

    :param db: The database pool.
    :param quote: The quote.
    :param claim_hash: The hash of the buyer's claim token, which collects the purchase's invites
        (:mod:`unfret.credits.invites`); ``None``: the purchase gets none.
    """
    await db.execute(
        "INSERT INTO vat_quotes (id, checks, country, country_mismatch, vat_rate_bps, net_usd_micros, "
        "vat_usd_micros, beneficiary, reference, claim_hash) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        quote.id,
        quote.checks,
        quote.country,
        quote.country_mismatch,
        quote.vat_rate_bps,
        quote.net_usd_micros,
        quote.vat_usd_micros,
        quote.beneficiary,
        quote.reference,
        claim_hash,
    )


async def get_quote(db: asyncpg.Pool | asyncpg.Connection, quote_id: str) -> Quote | None:
    """
    A stored quote.

    :param db: The database pool or connection.
    :param quote_id: The quote's id (from the memo).
    :return: The quote, or ``None`` if unknown or expired (purged).
    """
    row = await db.fetchrow("SELECT * FROM vat_quotes WHERE id = $1", quote_id)
    if row is None:
        return None
    fields = {k: row[k] for k in Quote.__dataclass_fields__}
    return Quote(**fields)


async def quote_by_reference(db: asyncpg.Pool | asyncpg.Connection, accounts: list[str]) -> Quote | None:
    """
    The stored quote whose reference is one of a transaction's accounts.

    :param db: The database pool or connection.
    :param accounts: Every account the transaction names (base58).
    :return: The quote, or ``None`` if none (or more than one: ambiguous) matches.
    """
    rows = await db.fetch("SELECT * FROM vat_quotes WHERE reference = ANY($1::text[]) LIMIT 2", accounts)
    if len(rows) != 1:
        return None
    return Quote(**{k: rows[0][k] for k in Quote.__dataclass_fields__})


async def mark_paid(conn: asyncpg.Connection, quote_id: str) -> bool:
    """
    Record that a payment naming the quote was credited (in the scanner's transaction; the first payment counts).

    :param conn: The connection (inside the crediting transaction).
    :param quote_id: The quote's id.
    :return: Whether this was the quote's first payment.
    """
    first = await conn.fetchval(
        "UPDATE vat_quotes SET paid_at = NOW() WHERE id = $1 AND paid_at IS NULL RETURNING TRUE", quote_id
    )
    return bool(first)


async def quote_status(db: asyncpg.Pool, quote_id: str) -> tuple[bool, int] | None:
    """
    Whether a quote has been paid, and for how many checks.

    :param db: The database pool.
    :param quote_id: The quote's id.
    :return: ``(paid, checks)``, or ``None`` if unknown or purged.
    """
    row = await db.fetchrow("SELECT paid_at IS NOT NULL AS paid, checks FROM vat_quotes WHERE id = $1", quote_id)
    return None if row is None else (bool(row["paid"]), int(row["checks"]))


@dataclass(frozen=True)
class Sale:
    """How a crypto payment splits into credits and VAT, and the record it leaves."""

    country: str
    country_mismatch: bool
    vat_rate_bps: int
    net_usd_micros: int
    vat_usd_micros: int


def sale_for(settings: Settings, received_usd_micros: int, quote: Quote | None) -> Sale:
    """
    Split a payment by its quote's rate, or, without one, as a sale to an unknown country at the Dutch rate.

    What arrived is what counts (SOL is valued at crediting time, so it can differ a little from the quote).

    :param settings: The service settings.
    :param received_usd_micros: The payment's dollar value.
    :param quote: Its quote, if the transaction named a known one.
    :return: The split.
    """
    country = quote.country if quote else UNKNOWN
    bps = quote.vat_rate_bps if quote else rate_bps(settings, UNKNOWN)
    net, vat = split_gross(received_usd_micros, bps)
    return Sale(country, quote.country_mismatch if quote else False, bps, net, vat)


async def record_sale(
    conn: asyncpg.Connection, sale: Sale, currency: str, amount: int, *, signature: str, today: date | None = None
) -> None:
    """
    Keep the sales record of one crypto purchase (in the scanner's transaction, so exactly once).

    :param conn: The connection (inside the crediting transaction).
    :param sale: The split.
    :param currency: ``SOL`` or ``USDC``.
    :param amount: The amount received in the currency's base units.
    :param signature: The transaction's signature.
    :param today: The day of the sale (injectable for tests).
    """
    await conn.execute(
        "INSERT INTO crypto_sales (sold_on, country, country_mismatch, net_usd_micros, vat_rate_bps, "
        "vat_usd_micros, asset, amount, signature) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        today or datetime.now(UTC).date(),
        sale.country,
        sale.country_mismatch,
        sale.net_usd_micros,
        sale.vat_rate_bps,
        sale.vat_usd_micros,
        currency,
        amount,
        signature,
    )


def quarter_bounds(quarter: str) -> tuple[date, date]:
    """
    The first and last day of a quarter.

    :param quarter: ``YYYY-Qn``.
    :return: ``(first, last)``.
    :raises ValueError: For anything else.
    """
    year, q = quarter.upper().split("-Q")
    first = date(int(year), 3 * (int(q) - 1) + 1, 1)
    if not 1 <= int(q) <= 4:
        raise ValueError(quarter)
    after = date(first.year + (first.month == 10), (first.month + 2) % 12 + 1, 1)
    return first, after - timedelta(days=1)


async def sales_csv(db: asyncpg.Pool, quarter: str) -> str:
    """
    A quarter's crypto sales as CSV, for the bookkeeping (amounts in dollars).

    :param db: The database pool.
    :param quarter: ``YYYY-Qn``.
    :return: The CSV text, one line per sale, with a header.
    """
    first, last = quarter_bounds(quarter)
    rows = await db.fetch(
        "SELECT * FROM crypto_sales WHERE sold_on BETWEEN $1 AND $2 ORDER BY sold_on, id", first, last
    )
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "date",
            "country",
            "country_mismatch",
            "net_usd",
            "vat_rate_pct",
            "vat_usd",
            "gross_usd",
            "asset",
            "amount",
            "signature",
        ]
    )
    usd = Decimal(1_000_000)
    for row in rows:
        net, vat = row["net_usd_micros"], row["vat_usd_micros"]
        writer.writerow(
            [
                row["sold_on"].isoformat(),
                row["country"],
                "yes" if row["country_mismatch"] else "no",
                format(Decimal(net) / usd, "f"),
                format(Decimal(row["vat_rate_bps"]) / 100, "f"),
                format(Decimal(vat) / usd, "f"),
                format(Decimal(net + vat) / usd, "f"),
                row["asset"],
                row["amount"],
                row["signature"],
            ]
        )
    return out.getvalue()


async def purge(db: asyncpg.Pool, settings: Settings, now: datetime | None = None) -> None:
    """
    Delete expired quotes and sales records past their retention.

    Quotes go a day past their validity, so a late-finalizing payment still finds its quote.

    :param db: The database pool.
    :param settings: The service settings.
    :param now: The current time (injectable for tests).
    """
    now = now or datetime.now(UTC)
    await db.execute(
        "DELETE FROM vat_quotes WHERE created_at < $1", now - timedelta(seconds=settings.vat_quote_ttl_s, days=1)
    )
    await db.execute(
        "DELETE FROM crypto_sales WHERE sold_on < $1",
        now.date() - timedelta(days=settings.vat_records_retention_days),
    )


def describe(settings: Settings) -> dict[str, Any]:
    """
    The VAT rules for ``/v1/pricing``.

    :param settings: The service settings.
    :return: Mode, home country and the rate each EU country pays now.
    """
    return {
        "mode": settings.vat_mode,
        "home_country": HOME_COUNTRY,
        "eu_rates_pct": effective_rates(settings),
        "applies_to": "crypto purchases (card purchases: the payment provider adds its own sales tax/VAT)",
    }
