"""
The e2e sweep's own logic, kept free of network calls so the unit tests can check it (tests/unit/test_e2e_helpers.py).

Settings come from ``UNFRET_E2E_*`` environment variables (:meth:`E2EConfig.from_env`); the README ("Deploying",
"End-to-end sweep") lists them and the repo secrets they map from.
"""

import base64
import json
import os
import re
import secrets
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlsplit

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from unfret_mcp.solana import USDC_MINTS, associated_token_address, transfer_checked  # on sys.path: tests/e2e/__init__

from unfret.credits.fiat import sign_standard_webhook

DEV_URL = "https://dev.unfret.kroshtan.com"
DEVNET_RPC = "https://api.devnet.solana.com"
DEVNET_CAIP2 = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
MAINNET_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
# Fixture pages are served from the forge at an immutable commit, so the page a run checks can't change under it.
FORGE_RAW = "https://git.kroshtan.com/kroshtan/unfret_backend/raw/commit"
FIXTURES = "tests/fixtures/pages"
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no")
# The dev containers on the server (config/deploy.dev.yml): Kamal labels the app's, the accessory has a fixed name.
DEV_APP_FILTER = (
    "--filter",
    "label=service=unfret",
    "--filter",
    "label=destination=dev",
    "--filter",
    "label=role=web",
)
DEV_DB_CONTAINER = "unfret-dev-db"

LAMPORTS_PER_SOL = 1_000_000_000
USDC_UNITS = 1_000_000
# What the payer must hold before anything is spent: a 25-pack in USDC, an x402 quick check in USDC, a 25-pack and an
# x402 deep check in SOL, the wSOL wrap and the fees, with room to spare.
MIN_SOL_LAMPORTS = 1_500_000_000
MIN_USDC_UNITS = 5_010_000  # the 25-pack ($5.00) and the quick check ($0.01)
# An admin grant or debit moves at most this much (unfret.routes.admin.MAX_GRANT_USD_MICROS).
MAX_ADJUST_USD_MICROS = 10_000_000


class E2EConfigError(RuntimeError):
    """A setting the sweep needs is missing or malformed."""


def _products(value: str) -> dict[int, str]:
    products: dict[int, str] = {}
    for item in filter(None, (part.strip() for part in value.split(","))):
        size, sep, product = (piece.strip() for piece in item.partition("="))
        if not sep or not size.isdigit() or not product:
            raise E2EConfigError("UNFRET_E2E_POLAR_PRODUCTS must read like 25=<product id>,50=<product id>")
        products[int(size)] = product
    return products


@dataclass(frozen=True)
class E2EConfig:
    """What the sweep runs against and with which (test-only) credentials."""

    base_url: str = DEV_URL
    rpc_url: str = DEVNET_RPC
    expected_commit: str = ""
    fixture_ref: str = ""
    payer_key: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    admin_token: str = field(default="", repr=False)
    fiat_webhook_secret: str = field(default="", repr=False)
    polar_products: dict[int, str] = field(default_factory=dict)
    expect_fiat: str = "polar:production"
    ssh_target: str = ""
    no_skip: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> E2EConfig:
        """
        Read the ``UNFRET_E2E_*`` settings.

        :param env: The environment (default: ``os.environ``).
        :return: The configuration.
        """
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return env.get(f"UNFRET_E2E_{name}", "").strip() or default

        return cls(
            base_url=get("URL", DEV_URL).rstrip("/"),
            rpc_url=get("RPC_URL", DEVNET_RPC),
            expected_commit=get("COMMIT"),
            fixture_ref=get("FIXTURE_REF"),
            payer_key=get("PAYER_KEY"),
            api_key=get("KEY"),
            admin_token=get("ADMIN_TOKEN"),
            fiat_webhook_secret=get("FIAT_WEBHOOK_SECRET"),
            polar_products=_products(get("POLAR_PRODUCTS")),
            expect_fiat=get("EXPECT_FIAT", "polar:production"),
            ssh_target=get("SSH"),
            no_skip=get("NO_SKIP") in {"1", "true", "yes"},
        )

    def need(self, name: str) -> str:
        """
        A required setting, by its attribute name.

        :param name: E.g. ``"api_key"``.
        :return: Its value.
        :raises E2EConfigError: If it is empty.
        """
        value = getattr(self, name)
        if not value:
            raise E2EConfigError(f"UNFRET_E2E_{name.upper()} is not set (see README, 'End-to-end sweep')")
        return str(value)


def refuse_mainnet(discovery: Mapping[str, Any]) -> None:
    """
    Refuse to run against anything that isn't on devnet: the sweep spends from its payer.

    :param discovery: The target's ``/.well-known/x402`` document.
    :raises E2EConfigError: Unless it says devnet, and only devnet.
    """
    network, name = discovery.get("network"), discovery.get("network_name")
    if network == MAINNET_CAIP2 or name == "solana-mainnet":
        raise E2EConfigError(f"refusing to run: the target is on Solana MAINNET ({name}, {network})")
    if network != DEVNET_CAIP2 or name != "solana-devnet":
        raise E2EConfigError(f"refusing to run: the target is not on Solana devnet ({name}, {network})")


def parse_keypair(text: str) -> Keypair:
    """
    The payer's keypair from a Solana CLI JSON byte array or a base58 secret key.

    :param text: The key.
    :return: The keypair.
    :raises E2EConfigError: If it is neither.
    """
    text = text.strip()
    try:
        if text.startswith("["):
            return Keypair.from_bytes(bytes(json.loads(text)))
        return Keypair.from_base58_string(text)
    except (ValueError, TypeError) as exc:
        raise E2EConfigError("UNFRET_E2E_PAYER_KEY is not a keypair (JSON byte array or base58)") from exc


def run_nonce() -> str:
    """
    A per-run marker for the fixture URLs, searched for in the logs and the database afterwards.

    :return: ``e2e`` and 24 hex digits.
    """
    return "e2e" + secrets.token_hex(12)


def fixture_url(ref: str, name: str, nonce: str, label: str) -> str:
    """
    A fixture page's URL at an immutable commit, unique to this run and this use of it.

    The marker goes in a query parameter that the listing key keeps (``e2e``; tracking parameters are dropped), so
    every use is a distinct page for the result cache and the x402 fast path.

    :param ref: The commit the fixtures are served from.
    :param name: The fixture's file name (in ``tests/fixtures/pages``).
    :param nonce: The run's marker.
    :param label: Which use of the page this is.
    :return: The URL.
    :raises E2EConfigError: If ``ref`` isn't a commit SHA.
    """
    if not re.fullmatch(r"[0-9a-f]{7,40}", ref):
        raise E2EConfigError(f"not a commit to serve the fixtures from: {ref!r}")
    return f"{FORGE_RAW}/{ref}/{FIXTURES}/{name}?e2e={nonce}-{quote(label)}"


def funding_problem(address: str, sol_lamports: int, usdc_units: int) -> str | None:
    """
    Why the payer can't run the sweep, if it can't: it is never topped up from a faucet here.

    :param address: The payer's address.
    :param sol_lamports: Its SOL.
    :param usdc_units: Its devnet USDC (base units).
    :return: A "fund <address>" message, or ``None`` when it holds enough.
    """
    short = []
    if sol_lamports < MIN_SOL_LAMPORTS:
        short.append(f"{MIN_SOL_LAMPORTS / LAMPORTS_PER_SOL:g} SOL (has {sol_lamports / LAMPORTS_PER_SOL:g})")
    if usdc_units < MIN_USDC_UNITS:
        short.append(f"{MIN_USDC_UNITS / USDC_UNITS:g} devnet USDC (has {usdc_units / USDC_UNITS:g})")
    if not short:
        return None
    return f"fund {address} on devnet with at least " + " and ".join(short) + " (faucet.solana.com, faucet.circle.com)"


def netting(delta: int) -> list[tuple[Literal["grant", "debit"], int]]:
    """
    The admin calls that bring an account back to its starting balance.

    :param delta: Balance now minus balance at the start (micro-dollars).
    :return: ``("debit", n)`` while above the start, ``("grant", n)`` while below, each at most $10.
    """
    kind: Literal["grant", "debit"] = "debit" if delta > 0 else "grant"
    left, steps = abs(delta), []
    while left:
        step = min(left, MAX_ADJUST_USD_MICROS)
        steps.append((kind, step))
        left -= step
    return steps


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """
    Server-sent events as ``(event, data)`` pairs.

    :param text: The stream's body.
    :return: The events in order.
    """
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


def b64json(header: str) -> dict[str, Any]:
    """
    Decode an x402 header (base64 JSON).

    :param header: The header value.
    :return: The object.
    """
    decoded: dict[str, Any] = json.loads(base64.b64decode(header))
    return decoded


def x402_header(requirement: Mapping[str, Any], tx: VersionedTransaction) -> str:
    """
    The ``PAYMENT-SIGNATURE`` header for a payment transaction.

    :param requirement: The accepted requirement, as offered.
    :param tx: The transaction, signed by the payer.
    :return: The header value.
    """
    payload = {
        "x402Version": 2,
        "accepted": dict(requirement),
        "payload": {"transaction": base64.b64encode(bytes(tx)).decode()},
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def sign_purchase(transaction_b64: str, payer: Keypair) -> bytes:
    """
    Sign the unsigned purchase transaction ``/v1/credits/purchase-tx`` returns, as the buyer's wallet would.

    :param transaction_b64: Its ``transaction``.
    :param payer: The buyer (the fee payer and only signer).
    :return: The signed transaction.
    :raises E2EConfigError: If it isn't the payer's to sign alone.
    """
    unsigned = VersionedTransaction.from_bytes(base64.b64decode(transaction_b64))
    message = unsigned.message
    if message.account_keys[0] != payer.pubkey() or message.header.num_required_signatures != 1:
        raise E2EConfigError("the purchase transaction isn't the payer's alone to sign")
    return bytes(VersionedTransaction(message, [payer]))


@dataclass(frozen=True)
class SolanaPayRequest:
    """A Solana Pay transfer request (``solana:<recipient>?amount=…``)."""

    recipient: Pubkey
    amount: int  # base units: lamports, or the token's
    mint: Pubkey | None
    reference: Pubkey | None
    memo: str | None


def parse_solana_pay(url: str, network: str = "solana-devnet") -> SolanaPayRequest:
    """
    Read a Solana Pay transfer request.

    :param url: The ``solana:`` URL.
    :param network: For the USDC mint's decimals.
    :return: The request, its amount in base units.
    :raises E2EConfigError: If it isn't one.
    """
    parts = urlsplit(url)
    if parts.scheme != "solana" or not parts.path:
        raise E2EConfigError(f"not a Solana Pay URL: {url[:60]}")
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    mint = Pubkey.from_string(query["spl-token"]) if "spl-token" in query else None
    if mint is not None and mint != USDC_MINTS[network]:
        raise E2EConfigError(f"unexpected token {mint}")
    places = 9 if mint is None else 6
    amount = Decimal(query["amount"]) * 10**places
    if amount != amount.to_integral_value():
        raise E2EConfigError(f"amount {query['amount']} has more than {places} decimals")
    return SolanaPayRequest(
        recipient=Pubkey.from_string(parts.path),
        amount=int(amount),
        mint=mint,
        reference=Pubkey.from_string(query["reference"]) if "reference" in query else None,
        memo=query.get("memo"),
    )


def reference_only_transfer(request: SolanaPayRequest, payer: Keypair, blockhash: str) -> bytes:
    """
    Pay a Solana Pay request the way a wallet that drops memos does: the transfer and its reference, no memo.

    :param request: The request.
    :param payer: The paying wallet.
    :param blockhash: A recent blockhash.
    :return: The signed transaction.
    :raises E2EConfigError: If the request has no reference.
    """
    if request.reference is None:
        raise E2EConfigError("the Solana Pay request carries no reference")
    if request.mint is None:
        moved = transfer(
            TransferParams(from_pubkey=payer.pubkey(), to_pubkey=request.recipient, lamports=request.amount)
        )
    else:
        moved = transfer_checked(
            associated_token_address(payer.pubkey(), request.mint),
            associated_token_address(request.recipient, request.mint),
            payer.pubkey(),
            request.amount,
            request.mint,
        )
    extra = AccountMeta(request.reference, is_signer=False, is_writable=False)
    instruction = Instruction(moved.program_id, moved.data, [*moved.accounts, extra])
    message = MessageV0.try_compile(payer.pubkey(), [instruction], [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction(message, [payer]))


def polar_order(
    order_id: str, account: str, product_id: str, net_cents: int, refunded_cents: int = 0
) -> dict[str, Any]:
    """
    A Polar order as its webhooks carry it, reduced to what the service reads (plus the shape around it).

    No real order: the id is made up per run and never reaches Polar.

    :param order_id: The made-up order id.
    :param account: The ledger account (``key:<id>``), as the checkout's metadata carries it.
    :param product_id: The pack's Polar product.
    :param net_cents: The order's amount before tax (the pack's card price).
    :param refunded_cents: The amount refunded so far.
    :return: The order.
    """
    return {
        "id": order_id,
        "status": "refunded" if refunded_cents else "paid",
        "paid": True,
        "currency": "usd",
        "net_amount": net_cents,
        "tax_amount": 0,
        "total_amount": net_cents,
        "refunded_amount": refunded_cents,
        "refunded_tax_amount": 0,
        "billing_reason": "purchase",
        "product_id": product_id,
        "checkout_id": None,
        "metadata": {"account": account},
        "customer": {"id": "e2e", "email": "e2e@example.invalid", "name": "Unfret e2e sweep"},
    }


def signed_webhook(
    secret: str, event_type: str, data: Mapping[str, Any], *, timestamp: int, msg_id: str | None = None
) -> tuple[bytes, dict[str, str]]:
    """
    A Standard Webhooks request as Polar sends it.

    :param secret: The ``whsec_…`` secret to sign with.
    :param event_type: E.g. ``order.paid``.
    :param data: The event's ``data``.
    :param timestamp: The ``webhook-timestamp`` (Unix seconds).
    :param msg_id: The ``webhook-id`` (random by default).
    :return: The body and headers.
    """
    body = json.dumps({"type": event_type, "data": dict(data)}).encode()
    msg_id = msg_id or f"msg_e2e_{secrets.token_hex(8)}"
    headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign_standard_webhook(secret, msg_id, timestamp, body),
        "content-type": "application/json",
    }
    return body, headers


def ssh_command(target: str, *remote: str) -> list[str]:
    """
    An ssh invocation that never prompts and never rewrites known_hosts (Kamal's Net::SSH must keep accepting it).

    :param target: ``user@host``.
    :param remote: The remote command's words, each quoted for the remote shell.
    :return: The argv.
    :raises E2EConfigError: For a target that could be read as an option.
    """
    if not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._-]*@[A-Za-z0-9.-]+", target):
        raise E2EConfigError(f"UNFRET_E2E_SSH must read like user@host, not {target!r}")
    return ["ssh", *SSH_OPTIONS, target, "--", " ".join(shlex.quote(word) for word in remote)]


def nonce_hits(text: str, nonce: str) -> int:
    """
    How often the run's marker appears.

    :param text: Logs or a database dump.
    :param nonce: The marker.
    :return: The count.
    """
    return text.count(nonce)
