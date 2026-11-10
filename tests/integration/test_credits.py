import asyncio
import base64
from datetime import UTC, date, datetime
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from tests.integration.conftest import DROP_ALL, bearer
from tests.unit.x402_helpers import StaticOracle
from unfret.auth import get_credits
from unfret.config import settings
from unfret.credits import CreditsDoor, keys, siws, vat
from unfret.credits.scanner import CreditScanner
from unfret.main import app
from unfret.migrations import MIGRATIONS, migrate
from unfret.prices import PriceBook
from unfret.routes.check import _refund_as_credits
from unfret.x402.solana import USDC_MINTS, associated_token_address

HOT = Keypair()
PRICE = settings.analysis_price_usd_micros
# At $1,000/SOL one lamport is worth exactly one micro-dollar, which keeps the SOL scanner amounts readable.
SOL_BOOK = PriceBook(settings, StaticOracle("1000"))


def net(received: int) -> int:
    """What a transfer without a quote credits: a sale to an unknown country, Dutch VAT (21%) taken out."""
    return received * 10_000 // 12_100


def transfer(source: str, destination: str, lamports: int, *, inner: bool = False) -> dict[str, Any]:
    ix = {
        "program": "system",
        "parsed": {"type": "transfer", "info": {"source": source, "destination": destination, "lamports": lamports}},
    }
    if inner:
        return {
            "transaction": {"message": {"instructions": []}},
            "meta": {"err": None, "innerInstructions": [{"index": 0, "instructions": [ix]}]},
        }
    return {"transaction": {"message": {"instructions": [ix]}}, "meta": {"err": None, "innerInstructions": []}}


class ChainRpc:
    """A fake RPC over an ordered list of (signature, transaction), oldest first."""

    def __init__(self) -> None:
        self.chain: list[tuple[str, dict[str, Any] | None, Any]] = []
        self.history: list[dict[str, Any]] = []

    def add(self, signature: str, tx: dict[str, Any] | None, err: Any = None) -> None:
        self.chain.append((signature, tx, err))

    async def signature_page(
        self,
        address: Pubkey,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "finalized",
    ) -> list[dict[str, Any]]:
        if str(address) != str(HOT.pubkey()):
            return self.history[:limit]
        newest_first = [{"signature": s, "err": e, "blockTime": 1} for s, _, e in reversed(self.chain)]
        sigs = [e["signature"] for e in newest_first]
        start = sigs.index(before) + 1 if before else 0
        end = sigs.index(until) if until in sigs else len(sigs)
        return newest_first[start:end][:limit]

    async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
        return next((tx for s, tx, _ in self.chain if s == signature), None)

    async def latest_blockhash(self) -> str:
        return "11111111111111111111111111111111"


async def balance(pool: asyncpg.Pool, address: str) -> int | None:
    value = await pool.fetchval("SELECT balance_usd_micros FROM wallets WHERE pubkey = $1", address)
    return None if value is None else int(value)


# ── Sign-in ───────────────────────────────────────────────────────────────────


async def sign_in(client: httpx.AsyncClient, keypair: Keypair, *, domain: str | None = None) -> httpx.Response:
    fields = (await client.get("/v1/auth/nonce")).json()
    text = siws.message_text(
        siws.SignInFields(
            domain=domain or fields["domain"],
            address=str(keypair.pubkey()),
            statement=fields["statement"],
            uri=fields["uri"],
            version=fields["version"],
            nonce=fields["nonce"],
            issued_at=fields["issued_at"],
            expiration_time=fields["expiration_time"],
        )
    ).encode()
    return await client.post(
        "/v1/auth/verify",
        json={
            "address": str(keypair.pubkey()),
            "signed_message": base64.b64encode(text).decode(),
            "signature": base64.b64encode(bytes(keypair.sign_message(text))).decode(),
        },
    )


async def test_sign_in_issues_a_working_session(client: httpx.AsyncClient) -> None:
    keypair = Keypair()
    response = await sign_in(client, keypair)

    assert response.status_code == 200
    session = response.json()
    assert session["address"] == str(keypair.pubkey())
    credits = await client.get("/v1/credits", headers={"Authorization": f"Bearer {session['token']}"})
    assert credits.json() == {
        "address": str(keypair.pubkey()),
        "balance_usd_micros": 0,
        "checks_remaining": 0,
        "balance_lamports": 0,
    }


async def test_sign_in_for_another_site_is_refused(client: httpx.AsyncClient) -> None:
    response = await sign_in(client, Keypair(), domain="evil.example")
    assert response.status_code == 401
    assert "another site" in response.json()["detail"]


async def test_signature_by_another_key_is_refused(client: httpx.AsyncClient) -> None:
    fields = (await client.get("/v1/auth/nonce")).json()
    victim, attacker = Keypair(), Keypair()
    text = siws.message_text(siws.SignInFields(**{**fields, "address": str(victim.pubkey())})).encode()
    response = await client.post(
        "/v1/auth/verify",
        json={
            "address": str(victim.pubkey()),
            "signed_message": base64.b64encode(text).decode(),
            "signature": base64.b64encode(bytes(attacker.sign_message(text))).decode(),
        },
    )
    assert response.status_code == 401


async def test_sign_in_off_without_a_secret(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "session_secret", SecretStr(""))
    app.credits = None
    assert (await client.get("/v1/auth/nonce")).status_code == 503
    assert (await client.post("/v1/analyze", json={"page_content": "x"}, headers=bearer("x"))).status_code == 503


# ── No trial ──────────────────────────────────────────────────────────────────


async def test_a_new_wallet_starts_at_zero(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    rpc.history = [{"signature": f"s{i}", "blockTime": 1} for i in range(12)]  # a well-used wallet gets nothing either
    app.credits = CreditsDoor(settings, rpc, None)  # type: ignore[arg-type]
    keypair = Keypair()

    assert (await sign_in(client, keypair)).status_code == 200
    assert await balance(pool, str(keypair.pubkey())) is None
    assert "trial_checks" not in (await client.get("/v1/pricing")).json()


# ── Pricing and purchases ─────────────────────────────────────────────────────


async def test_pricing_without_purchases(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/pricing")).json()
    assert (body["currency"], body["analysis_usd_micros"]) == ("USD", PRICE)
    assert body["hot_wallet"] is None
    assert [(p["checks"], p["price_usd"], p["crypto_usd"]) for p in body["packs"]] == [
        (25, "$6.00", "$5.00"),
        (50, "$11.50", "$10.00"),
        (100, "$22.00", "$20.00"),
    ]
    assert body["fiat"]["min_usd_micros"] == 6_000_000  # the smallest pack by card
    assert body["vat"]["country_hint"] is None  # not through Cloudflare
    assert "no-store" in (await client.get("/v1/pricing")).headers["cache-control"]


async def test_purchases_off_until_the_scanner_runs(client: httpx.AsyncClient) -> None:
    body = {"address": str(Keypair().pubkey()), "checks": 25, "country": "NL"}
    response = await client.post("/v1/credits/purchase-tx", json=body)
    assert response.status_code == 503


@pytest.fixture
async def shop(client: httpx.AsyncClient, pool: asyncpg.Pool) -> ChainRpc:
    rpc = ChainRpc()
    door = CreditsDoor(settings, rpc, HOT.pubkey(), PriceBook(settings, StaticOracle("100")))  # type: ignore[arg-type]
    door.attach(pool)
    assert door.scanner is not None and door.usdc_scanner is not None
    await door.scanner.initialise()
    await door.usdc_scanner.initialise()
    app.credits = door
    return rpc


async def test_purchase_transaction_pays_the_pack_plus_vat(
    client: httpx.AsyncClient, shop: ChainRpc, pool: asyncpg.Pool
) -> None:
    buyer = Keypair()
    body = {"address": str(buyer.pubkey()), "checks": 25, "country": "DE"}
    response = await client.post("/v1/credits/purchase-tx", json=body)

    assert response.status_code == 200
    reply = response.json()
    assert reply["hot_wallet"] == str(HOT.pubkey())
    assert reply["price"] == {
        "country": "DE",
        "vat_rate_pct": "21",  # under the threshold every EU buyer pays the Dutch rate
        "net_usd_micros": 5_000_000,
        "vat_usd_micros": 1_050_000,
        "total_usd_micros": 6_050_000,
        "total_usd": "$6.05",
    }
    assert reply["amount"] == 60_500_000  # $6.05 at $100/SOL
    quote = await pool.fetchrow("SELECT * FROM vat_quotes")
    assert (quote["country"], quote["checks"], quote["beneficiary"]) == ("DE", 25, None)
    memo = f"unfret:quote:{quote['id']}"
    assert reply["quote_id"] == quote["id"]  # what the checkout polls
    reference = Pubkey.from_string(quote["reference"])  # the Solana Pay reference, in case the wallet drops the memo
    assert f"&reference={reference}&" in reply["solana_pay_url"]
    assert reply["solana_pay_url"].endswith(f"&memo=unfret%3Aquote%3A{quote['id']}")
    tx = VersionedTransaction.from_bytes(base64.b64decode(reply["transaction"]))
    keys = tx.message.account_keys
    assert keys[0] == buyer.pubkey() and HOT.pubkey() in keys
    assert bytes(tx.message.instructions[-1].data) == memo.encode()
    pay = tx.message.instructions[0]
    assert [keys[i] for i in bytes(pay.accounts)] == [buyer.pubkey(), HOT.pubkey(), reference]
    assert (await client.get("/v1/pricing")).json()["hot_wallet"] == str(HOT.pubkey())


async def test_buyers_outside_the_eu_pay_no_vat(client: httpx.AsyncClient, shop: ChainRpc) -> None:
    body = {"address": str(Keypair().pubkey()), "checks": 50, "country": "us", "currency": "USDC"}
    reply = (await client.post("/v1/credits/purchase-tx", json=body)).json()
    assert (reply["price"]["country"], reply["price"]["vat_usd_micros"], reply["amount"]) == ("US", 0, 10_000_000)


@pytest.mark.parametrize(("checks", "country"), [(10, "NL"), (26, "NL"), (25, "XX"), (25, "N1")])
async def test_only_packs_and_real_countries(
    client: httpx.AsyncClient, shop: ChainRpc, checks: int, country: str
) -> None:
    body = {"address": str(Keypair().pubkey()), "checks": checks, "country": country}
    assert (await client.post("/v1/credits/purchase-tx", json=body)).status_code in {400, 422}


async def test_amounts_are_gone(client: httpx.AsyncClient, shop: ChainRpc) -> None:
    body = {"address": str(Keypair().pubkey()), "lamports": 15_000_000}  # the old arbitrary-amount request
    assert (await client.post("/v1/credits/purchase-tx", json=body)).status_code == 422


async def test_cloudflares_country_counts_only_from_cloudflare(
    client: httpx.AsyncClient, shop: ChainRpc, pool: asyncpg.Pool
) -> None:
    from_cloudflare = {"X-Edge-Peer": "172.64.1.2", "CF-IPCountry": "FR"}
    assert (await client.get("/v1/pricing", headers=from_cloudflare)).json()["vat"]["country_hint"] == "FR"
    forged = {"X-Edge-Peer": "203.0.113.9", "CF-IPCountry": "FR"}  # someone hitting the box directly
    assert (await client.get("/v1/pricing", headers=forged)).json()["vat"]["country_hint"] is None

    body = {"address": str(Keypair().pubkey()), "checks": 25, "country": "BE"}
    await client.post("/v1/credits/purchase-tx", json=body, headers=from_cloudflare)
    await client.post("/v1/credits/purchase-tx", json={**body, "country": "FR"}, headers=from_cloudflare)
    await client.post("/v1/credits/purchase-tx", json=body, headers=forged)
    rows = await pool.fetch("SELECT country, country_mismatch FROM vat_quotes ORDER BY created_at")
    assert [(r["country"], r["country_mismatch"]) for r in rows] == [("BE", True), ("FR", False), ("BE", False)]


async def test_solana_pay_qr(client: httpx.AsyncClient, shop: ChainRpc, pool: asyncpg.Pool) -> None:
    response = await client.get("/v1/credits/solana-pay.svg", params={"checks": 25, "country": "NL"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert (response.headers["x-unfret-total-usd"], response.headers["x-unfret-vat-rate"]) == ("$6.05", "21")
    assert response.text.startswith("<svg")
    quote_id = await pool.fetchval("SELECT id FROM vat_quotes")
    assert response.headers["x-unfret-quote"] == quote_id  # the page reads it (same origin) to poll the quote
    reference = await pool.fetchval("SELECT reference FROM vat_quotes")
    pay_url = response.headers["x-unfret-pay-url"]  # for a wallet app on the same phone
    assert pay_url.startswith("solana:") and f"reference={reference}" in pay_url
    # Loaded by the page as an <img>: a standalone SVG only renders there with the SVG namespace.
    assert 'xmlns="http://www.w3.org/2000/svg"' in response.text.split(">", 1)[0]


# ── Scanner ───────────────────────────────────────────────────────────────────


async def test_scanner_credits_incoming_transfers_exactly_once(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    rpc.add("funding", transfer("owner", str(HOT.pubkey()), 15_000_000))  # before the scanner: not credits
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()

    hot, cold = str(HOT.pubkey()), "cold"
    rpc.add("buy", transfer("alice", hot, 10_000_000))
    rpc.add("big", transfer("bob", hot, 100_000_000))  # $100 at $1,000/SOL
    rpc.add("smart", transfer("vault", hot, 3_000_000, inner=True))  # via a program
    rpc.add("sweep", transfer(hot, cold, 90_000_000))  # outgoing
    rpc.add("failed", None, err={"InstructionError": [0, "Custom"]})
    rpc.add(
        "x402",
        {
            "transaction": {
                "message": {
                    "instructions": [{"program": "spl-token", "parsed": {"type": "transferChecked", "info": {}}}]
                }
            },
            "meta": {"err": None},
        },
    )

    assert await scanner.scan_once() == 3
    assert await scanner.scan_once() == 0  # nothing new
    restarted = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    assert await restarted.scan_once() == 0  # the cursor survives restarts

    assert await balance(pool, "alice") == net(10_000_000)  # $10 at $1,000/SOL, VAT included
    assert await balance(pool, "bob") == net(100_000_000)
    assert await balance(pool, "vault") == net(3_000_000)
    assert await balance(pool, "owner") is None
    assert await balance(pool, hot) is None


async def test_scanner_skips_zero_value_transfers(pool: asyncpg.Pool) -> None:
    # Regression: one 0-lamport transfer to the hot wallet (costs the sender a fee) stalled the scanner for good.
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    hot = str(HOT.pubkey())
    rpc.add("zero", transfer("griefer", hot, 0))
    rpc.add("buy", transfer("alice", hot, 5_000_000))

    assert await scanner.scan_once() == 1
    assert await balance(pool, "griefer") is None
    assert await balance(pool, "alice") == net(5_000_000)
    assert await scanner.scan_once() == 0  # the cursor moved past both


async def test_scanner_pages_through_a_backlog(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("unfret.credits.scanner._PAGE", 3)
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    for i in range(8):
        rpc.add(f"t{i}", transfer("carol", str(HOT.pubkey()), 1_000_000))

    assert await scanner.scan_once() == 8
    assert await balance(pool, "carol") == 8 * net(1_000_000)


async def test_scanner_waits_for_unretrievable_transactions(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("t1", transfer("dave", str(HOT.pubkey()), 1_000_000))
    rpc.chain.append(("t2", None, None))  # listed but not retrievable yet
    rpc.add("t3", transfer("dave", str(HOT.pubkey()), 2_000_000))

    assert await scanner.scan_once() == 1
    rpc.chain[1] = ("t2", transfer("dave", str(HOT.pubkey()), 4_000_000), None)
    assert await scanner.scan_once() == 2
    assert await balance(pool, "dave") == net(1_000_000) + net(4_000_000) + net(2_000_000)


async def test_two_scanners_never_double_credit(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    one = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await one.initialise()
    two = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    for i in range(5):
        rpc.add(f"t{i}", transfer("erin", str(HOT.pubkey()), 1_000_000))

    await asyncio.gather(one.scan_once(), two.scan_once())
    await one.scan_once()
    assert await balance(pool, "erin") == 5 * net(1_000_000)
    assert await pool.fetchval("SELECT count(*) FROM crypto_sales") == 5  # one sales record each, never twice


# ── x402 refunds ─────────────────────────────────────────────────────────────


async def test_x402_refund_credits_the_payer(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    class Req:
        app = app

    assert await _refund_as_credits(Req(), "payer", 100_000) == 100_000  # type: ignore[arg-type]
    assert await balance(pool, "payer") == 100_000
    door = get_credits(Req())  # type: ignore[arg-type]
    assert door.sign_in_enabled


# ── Beneficiary memos (smart wallets) ─────────────────────────────────────────


def with_memos(tx: dict[str, Any], *memos: str) -> dict[str, Any]:
    tx["transaction"]["message"]["instructions"].extend({"program": "spl-memo", "parsed": m} for m in memos)
    return tx


async def test_vault_paid_purchase_credits_the_memo_beneficiary(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    member = str(Keypair().pubkey())
    vault_payment = transfer("vault-pda", str(HOT.pubkey()), 2_000_000, inner=True)  # paid by a program (CPI)
    rpc.add("vault", with_memos(vault_payment, f"unfret:credit:{member}"))

    assert await scanner.scan_once() == 1
    assert await balance(pool, member) == net(2_000_000)
    assert await balance(pool, "vault-pda") is None


@pytest.mark.parametrize(
    "memos",
    [("unfret:credit:garbage",), (f"unfret:credit:{Keypair().pubkey()}", f"unfret:credit:{Keypair().pubkey()}")],
)
async def test_bad_or_ambiguous_memos_credit_the_source(pool: asyncpg.Pool, memos: tuple[str, ...]) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("t", with_memos(transfer("frank", str(HOT.pubkey()), 3_000_000), *memos))

    await scanner.scan_once()
    assert await balance(pool, "frank") == net(3_000_000)


async def test_purchase_quote_names_the_beneficiary(
    client: httpx.AsyncClient, shop: ChainRpc, pool: asyncpg.Pool
) -> None:
    vault, member = Keypair().pubkey(), Keypair().pubkey()
    body = {"address": str(vault), "checks": 25, "country": "NL", "beneficiary": str(member)}
    response = await client.post("/v1/credits/purchase-tx", json=body)

    assert response.status_code == 200
    message = VersionedTransaction.from_bytes(base64.b64decode(response.json()["transaction"])).message
    assert message.account_keys[0] == vault
    assert await pool.fetchval("SELECT beneficiary FROM vat_quotes") == str(member)
    bad = await client.post("/v1/credits/purchase-tx", json={**body, "beneficiary": "nope"})
    assert bad.status_code == 400


# ── VAT: quotes, sales records ────────────────────────────────────────────────


async def quoted(pool: asyncpg.Pool, country: str, *, beneficiary: str | None = None, edge: str | None = None) -> str:
    quote = vat.price_quote(settings, 25, 5_000_000, country, edge=edge, beneficiary=beneficiary)
    await vat.save_quote(pool, quote)
    return quote.memo


async def test_a_quoted_purchase_credits_the_net_and_records_the_sale(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    hot, member = str(HOT.pubkey()), str(Keypair().pubkey())
    rpc.add("nl", with_memos(transfer("anna", hot, 6_050_000), await quoted(pool, "NL")))  # $6.05 = $5 + 21%
    rpc.add("us", with_memos(transfer("bill", hot, 5_000_000), await quoted(pool, "US")))
    rpc.add("for", with_memos(transfer("vault", hot, 6_050_000), await quoted(pool, "DE", beneficiary=member)))
    rpc.add("odd", with_memos(transfer("cleo", hot, 6_050_000), await quoted(pool, "BE", edge="NL")))
    rpc.add("hand", transfer("dirk", hot, 6_050_000))  # sent by hand, without a quote

    assert await scanner.scan_once() == 5
    assert await balance(pool, "anna") == 5_000_000  # 25 checks
    assert await balance(pool, "bill") == 5_000_000
    assert await balance(pool, member) == 5_000_000 and await balance(pool, "vault") is None
    assert await balance(pool, "dirk") == 5_000_000  # unknown country: the Dutch rate, conservatively
    rows = await pool.fetch("SELECT * FROM crypto_sales ORDER BY id")
    assert [(r["country"], r["country_mismatch"], r["vat_rate_bps"], r["vat_usd_micros"]) for r in rows] == [
        ("NL", False, 2100, 1_050_000),
        ("US", False, 0, 0),
        ("DE", False, 2100, 1_050_000),
        ("BE", True, 2100, 1_050_000),
        ("unknown", False, 2100, 1_050_000),
    ]
    assert [(r["signature"], r["asset"], r["amount"]) for r in rows][0] == ("nl", "SOL", 6_050_000)
    # Nothing that identifies a person: no IP, no wallet column (the transaction on chain shows who paid).
    columns = {
        r["column_name"]
        for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'crypto_sales'"
        )
    }
    assert not columns & {"ip", "address", "wallet", "pubkey", "account", "beneficiary", "email"}


async def test_the_scanner_marks_only_the_credited_quote_paid_once(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    hot = str(HOT.pubkey())
    paid, unpaid, dust = [await quoted(pool, "NL") for _ in range(3)]
    paid_id = paid.removeprefix(vat.QUOTE_MEMO_PREFIX)
    rpc.add("first", with_memos(transfer("anna", hot, 6_050_000), paid))
    rpc.add("zero", with_memos(transfer("bill", hot, 0), dust))  # credits nothing: not paid
    assert await scanner.scan_once() == 1
    rows = {r["id"]: r["paid_at"] for r in await pool.fetch("SELECT id, paid_at FROM vat_quotes")}
    first = rows.pop(paid_id)
    assert first is not None and set(rows.values()) == {None}
    assert unpaid.removeprefix(vat.QUOTE_MEMO_PREFIX) in rows

    rpc.add("again", with_memos(transfer("anna", hot, 6_050_000), paid))  # paying the same quote twice
    assert await scanner.scan_once() == 1
    assert await scanner.scan_once() == 0
    assert await pool.fetchval("SELECT paid_at FROM vat_quotes WHERE id = $1", paid_id) == first  # set once
    assert await balance(pool, "anna") == 10_000_000


def with_accounts(tx: dict[str, Any], *accounts: str) -> dict[str, Any]:
    """Name ``accounts`` among the transaction's account keys (as a wallet adds a Solana Pay reference)."""
    named = [{"pubkey": a, "signer": False, "writable": False, "source": "transaction"} for a in accounts]
    tx["transaction"]["message"]["accountKeys"] = named
    return tx


async def key_quote(pool: asyncpg.Pool) -> tuple[vat.Quote, str]:
    issued = await keys.create(pool)
    quote = vat.price_quote(settings, 25, 5_000_000, "NL", edge=None, beneficiary=f"key:{issued.key_id}")
    await vat.save_quote(pool, quote)
    assert quote.reference is not None
    return quote, f"key:{issued.key_id}"


async def test_a_reference_without_the_memo_credits_the_quotes_key(pool: asyncpg.Pool) -> None:
    # What Phantom sent on devnet: compute-budget instructions and the transfer, the memo gone.
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    quote, account = await key_quote(pool)
    hot = str(HOT.pubkey())
    assert quote.reference is not None
    rpc.add("phantom", with_accounts(transfer("payer", hot, 6_050_000), "payer", hot, quote.reference))

    assert await scanner.scan_once() == 1
    assert await balance(pool, account) == 5_000_000  # 25 checks, VAT taken out at the quote's rate
    assert await balance(pool, "payer") is None
    assert await balance(pool, quote.reference) is None  # the reference is never credited
    assert await pool.fetchval("SELECT paid_at FROM vat_quotes WHERE id = $1", quote.id) is not None
    assert await pool.fetchval("SELECT country FROM crypto_sales") == "NL"


async def test_a_reference_and_the_memo_credit_once(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    quote, account = await key_quote(pool)
    hot = str(HOT.pubkey())
    assert quote.reference is not None
    both = with_memos(with_accounts(transfer("payer", hot, 6_050_000), "payer", hot, quote.reference), quote.memo)
    rpc.add("both", both)

    assert await scanner.scan_once() == 1
    assert await scanner.scan_once() == 0
    assert await balance(pool, account) == 5_000_000
    assert await balance(pool, "payer") is None and await balance(pool, quote.reference) is None
    assert await pool.fetchval("SELECT count(*) FROM crypto_sales") == 1


async def test_an_unknown_reference_falls_back_to_the_memo_then_the_sender(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    quote, account = await key_quote(pool)
    hot, stranger = str(HOT.pubkey()), str(Keypair().pubkey())
    rpc.add("memo", with_memos(with_accounts(transfer("anna", hot, 6_050_000), "anna", hot, stranger), quote.memo))
    rpc.add("bare", with_accounts(transfer("bill", hot, 6_050_000), "bill", hot, stranger))

    assert await scanner.scan_once() == 2
    assert await balance(pool, account) == 5_000_000  # the memo's quote
    assert await balance(pool, "anna") is None
    assert await balance(pool, "bill") == net(6_050_000)  # no quote at all: the sender, as before
    assert await balance(pool, stranger) is None


async def test_a_quote_shows_whether_it_is_paid_and_nothing_else(
    client: httpx.AsyncClient, pool: asyncpg.Pool
) -> None:
    quote = vat.price_quote(settings, 50, 10_000_000, "DE", edge=None, beneficiary=str(Keypair().pubkey()))
    await vat.save_quote(pool, quote)
    response = await client.get(f"/v1/credits/quotes/{quote.id}")
    assert response.status_code == 200
    assert response.json() == {"paid": False, "checks": 50, "invites": []}  # no beneficiary, country or price
    assert response.headers["cache-control"] == "no-store"

    async with pool.acquire() as conn:
        await vat.mark_paid(conn, quote.id)
    assert (await client.get(f"/v1/credits/quotes/{quote.id}")).json() == {"paid": True, "checks": 50, "invites": []}

    for unknown in ("a" * 20, "A" * 20, "short", "1" * 20, "a" * 41):
        reply = await client.get(f"/v1/credits/quotes/{unknown}")
        assert reply.status_code == 404, unknown
        assert reply.headers["cache-control"] == "no-store"


async def test_migration_14_adds_an_unset_reference_and_paid_at(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, MIGRATIONS[:13])
    await pool.execute(
        "INSERT INTO vat_quotes (id, checks, country, country_mismatch, vat_rate_bps, net_usd_micros, "
        "vat_usd_micros) VALUES ('aaaaaaaaaaaaaaaaaaaa', 25, 'NL', false, 2100, 5000000, 1050000)"
    )
    assert 14 in await migrate(pool)
    column = await pool.fetchrow(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'vat_quotes' AND column_name = 'paid_at'"
    )
    assert (column["data_type"], column["is_nullable"]) == ("timestamp with time zone", "YES")
    assert await pool.fetchval("SELECT paid_at FROM vat_quotes") is None
    assert await pool.fetchval("SELECT reference FROM vat_quotes") is None  # older quotes go by their memo
    assert (await vat.get_quote(pool, "aaaaaaaaaaaaaaaaaaaa")) is not None  # the quote still reads


async def test_per_country_mode_charges_each_eu_countrys_own_rate(
    client: httpx.AsyncClient, shop: ChainRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "vat_mode", "per_country")
    body = {"address": str(Keypair().pubkey()), "checks": 25, "country": "HU", "currency": "USDC"}
    reply = (await client.post("/v1/credits/purchase-tx", json=body)).json()
    assert (reply["price"]["vat_rate_pct"], reply["amount"]) == ("27", 6_350_000)
    assert (await client.get("/v1/pricing")).json()["vat"]["eu_rates_pct"]["FI"] == "25.5"


async def test_quarterly_sales_export_and_retention(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    sale = vat.Sale("NL", False, 2100, 5_000_000, 1_050_000)
    async with pool.acquire() as conn:
        await vat.record_sale(conn, sale, "USDC", 6_050_000, signature="sig1", today=date(2026, 11, 9))
        await vat.record_sale(conn, sale, "SOL", 60_500_000, signature="old", today=date(2014, 1, 2))
    admin = {"Authorization": "Bearer admin-secret"}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "admin_token", SecretStr("admin-secret"))
        response = await client.get("/v1/admin/sales/2026-Q4", headers=admin)
        assert (await client.get("/v1/admin/sales/2026-Q5", headers=admin)).status_code == 400
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0].startswith("date,country,country_mismatch,net_usd,vat_rate_pct,vat_usd,gross_usd")
    assert lines[1] == "2026-11-09,NL,no,5,21,1.05,6.05,USDC,6050000,sig1"
    assert len(lines) == 2

    await vat.purge(pool, settings, now=datetime(2026, 11, 9, tzinfo=UTC))
    assert await pool.fetchval("SELECT count(*) FROM crypto_sales") == 1  # ten years kept, older deleted


async def test_first_start_waits_for_funding_to_finalize(pool: asyncpg.Pool) -> None:
    class Finalizing(ChainRpc):
        """The newest transaction is confirmed but only shows up as finalized after a few polls."""

        polls = 0

        async def signature_page(self, address: Pubkey, **kwargs: Any) -> list[dict[str, Any]]:
            if kwargs.get("commitment") == "finalized" and kwargs.get("limit") == 50:
                self.polls += 1
                if self.polls < 3:
                    return []
            return await super().signature_page(address, **kwargs)

    rpc = Finalizing()
    rpc.add("seed", transfer("owner", str(HOT.pubkey()), 15_000_000))  # funded seconds before the first start
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()

    assert await pool.fetchval("SELECT signature FROM scan_cursor") == "seed"
    assert await scanner.scan_once() == 0
    assert await balance(pool, "owner") is None


# ── USDC purchases ────────────────────────────────────────────────────────────


USDC = USDC_MINTS["solana-devnet"]
HOT_USDC = associated_token_address(HOT.pubkey(), USDC)


class UsdcChainRpc(ChainRpc):
    """The same fake chain, served as the hot wallet's USDC account's history."""

    async def signature_page(self, address: Pubkey, **kwargs: Any) -> list[dict[str, Any]]:
        return await super().signature_page(HOT.pubkey() if address == HOT_USDC else address, **kwargs)


def usdc_transfer(authority: str, units: int, *, fee_payer: str | None = None) -> dict[str, Any]:
    info = {
        "authority": authority,
        "destination": str(HOT_USDC),
        "mint": str(USDC),
        "tokenAmount": {"amount": str(units)},
    }
    message: dict[str, Any] = {
        "instructions": [{"program": "spl-token", "parsed": {"type": "transferChecked", "info": info}}]
    }
    if fee_payer:
        message["accountKeys"] = [{"pubkey": fee_payer}]
    return {"transaction": {"message": message}, "meta": {"err": None, "innerInstructions": []}}


def usdc_scanner(rpc: ChainRpc, pool: asyncpg.Pool, oracle: StaticOracle) -> CreditScanner:
    book = PriceBook(settings, oracle)
    return CreditScanner(rpc, pool, HOT.pubkey(), settings, book=book, usdc_mint=USDC)  # type: ignore[arg-type]


async def test_usdc_purchases_are_credited_exactly_once_at_face_value(pool: asyncpg.Pool) -> None:
    rpc = UsdcChainRpc()
    scanner = usdc_scanner(rpc, pool, StaticOracle("100"))
    await scanner.initialise()
    assert scanner.name.startswith("credits-usdc:")  # its own cursor, next to the SOL scanner's

    rpc.add("buy", usdc_transfer("alice", 1_500_000))  # $1.50
    rpc.add("big", usdc_transfer("bob", 6_000_000))  # $6
    rpc.add("x402", usdc_transfer("carol", 150_000, fee_payer=str(HOT.pubkey())))  # a settlement we paid for

    assert await scanner.scan_once() == 2
    assert await scanner.scan_once() == 0
    assert await usdc_scanner(rpc, pool, StaticOracle("100")).scan_once() == 0  # the cursor survives restarts
    assert await balance(pool, "alice") == net(1_500_000)
    assert await balance(pool, "bob") == net(6_000_000)
    assert await balance(pool, "carol") is None


async def test_sol_purchases_wait_for_a_fresh_price_rather_than_guess(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    oracle = StaticOracle(None)
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=PriceBook(settings, oracle))  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("buy", transfer("alice", str(HOT.pubkey()), 10_000_000))

    assert await scanner.scan_once() == 0  # stale oracle: not credited, the cursor stays before it
    assert await balance(pool, "alice") is None
    oracle.value = "150"
    assert await scanner.scan_once() == 1
    assert await balance(pool, "alice") == net(1_500_000)  # 0.01 SOL at $150/SOL = $1.50


# ── Dollar balances (migration 5) ────────────────────────────────────────────


async def test_migration_5_keeps_every_wallets_number_of_checks(pool: asyncpg.Pool) -> None:
    await pool.execute(DROP_ALL)
    await migrate(pool, MIGRATIONS[:4])
    await pool.execute("INSERT INTO wallets (pubkey, balance_lamports) VALUES ('a', 4500000), ('b', 1499999)")

    assert await migrate(pool) == [v for v, _, _ in MIGRATIONS[4:]]
    assert await balance(pool, "a") == 450_000  # 3 checks before (1.5M lamports each), 3 checks now ($0.15 each)
    assert await balance(pool, "b") == 149_999


# ── API keys as beneficiaries ─────────────────────────────────────────────────


async def test_a_memo_naming_an_api_key_tops_up_the_key(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    issued = await keys.create(pool)
    rpc.add("t", with_memos(transfer("payer", str(HOT.pubkey()), 2_000_000), f"unfret:credit:key:{issued.key_id}"))

    assert await scanner.scan_once() == 1
    assert await balance(pool, f"key:{issued.key_id}") == net(2_000_000)
    assert await balance(pool, "payer") is None


async def test_a_memo_naming_an_unknown_key_credits_the_payer(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("t", with_memos(transfer("payer", str(HOT.pubkey()), 2_000_000), "unfret:credit:key:aaaaaaaaaa"))

    assert await scanner.scan_once() == 1
    assert await balance(pool, "payer") == net(2_000_000)  # never stranded on a key nobody holds
    assert await balance(pool, "key:aaaaaaaaaa") is None


async def test_purchase_transaction_can_top_up_an_api_key(
    client: httpx.AsyncClient, shop: ChainRpc, pool: asyncpg.Pool
) -> None:
    issued = await keys.create(pool)
    payer = Keypair().pubkey()
    pack = {"address": str(payer), "checks": 25, "country": "NL"}
    for named in (issued.key_id, issued.key):  # the id, or the whole key (only the id is used)
        response = await client.post("/v1/credits/purchase-tx", json={**pack, "key_id": named})
        assert response.status_code == 200
        assert issued.key.split("_")[2] not in response.text  # the secret never goes into a transaction
    beneficiaries = {r["beneficiary"] for r in await pool.fetch("SELECT beneficiary FROM vat_quotes")}
    assert beneficiaries == {f"key:{issued.key_id}"}
    unknown = await client.post("/v1/credits/purchase-tx", json={**pack, "key_id": "bbbbbbbbbb"})
    assert unknown.status_code == 404
    qr = await client.get(f"/v1/credits/solana-pay.svg?checks=25&country=NL&key_id={issued.key_id}")
    assert qr.status_code == 200
