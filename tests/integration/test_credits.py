import asyncio
import base64
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from tests.integration.conftest import bearer
from tests.unit.x402_helpers import StaticOracle
from veridex.auth import get_credits
from veridex.config import settings
from veridex.credits import CreditsDoor, ledger, siws
from veridex.credits.scanner import CreditScanner
from veridex.main import app
from veridex.migrations import MIGRATIONS, migrate
from veridex.prices import PriceBook
from veridex.routes.check import _refund_as_credits
from veridex.x402.solana import USDC_MINTS, associated_token_address

HOT = Keypair()
PRICE = settings.analysis_price_usd_micros
# At $1,000/SOL one lamport is worth exactly one micro-dollar, which keeps the SOL scanner amounts readable.
SOL_BOOK = PriceBook(settings, StaticOracle("1000"))


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


# ── Free trial ────────────────────────────────────────────────────────────────


@pytest.fixture
def chain(client: httpx.AsyncClient) -> ChainRpc:
    rpc = ChainRpc()
    app.credits = CreditsDoor(settings, rpc, None)  # type: ignore[arg-type]
    return rpc


async def test_trial_for_a_wallet_with_history(client: httpx.AsyncClient, pool: asyncpg.Pool, chain: ChainRpc) -> None:
    chain.history = [{"signature": f"s{i}", "blockTime": 1} for i in range(12)]
    keypair = Keypair()

    await sign_in(client, keypair)
    await sign_in(client, keypair)  # the second sign-in grants nothing more

    assert await balance(pool, str(keypair.pubkey())) == settings.trial_checks * PRICE


async def test_no_trial_for_a_fresh_wallet(client: httpx.AsyncClient, pool: asyncpg.Pool, chain: ChainRpc) -> None:
    chain.history = [{"signature": "s1", "blockTime": 2_000_000_000}]
    keypair = Keypair()

    assert (await sign_in(client, keypair)).status_code == 200
    assert await balance(pool, str(keypair.pubkey())) is None  # no row: it may still qualify later


async def test_trial_limited_per_visitor(
    client: httpx.AsyncClient, pool: asyncpg.Pool, chain: ChainRpc, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain.history = [{"signature": f"s{i}", "blockTime": 1} for i in range(12)]
    door = get_credits_from_app()
    door.trials.per_ip_per_day = 1
    first, second = Keypair(), Keypair()

    await sign_in(client, first)
    await sign_in(client, second)

    assert await balance(pool, str(first.pubkey())) == settings.trial_checks * PRICE
    assert await balance(pool, str(second.pubkey())) is None


def get_credits_from_app() -> CreditsDoor:
    door: CreditsDoor = app.credits
    return door


# ── Pricing and purchases ─────────────────────────────────────────────────────


async def test_pricing_without_purchases(client: httpx.AsyncClient) -> None:
    body = (await client.get("/v1/pricing")).json()
    assert (body["currency"], body["analysis_usd_micros"]) == ("USD", PRICE)
    assert body["hot_wallet"] is None
    assert body["suggested_packs"][0] == {"checks": 10, "usd_micros": 10 * PRICE}
    assert body["bonuses"] == [
        {"min_usd_micros": 5_000_000, "bonus_pct": 10},
        {"min_usd_micros": 25_000_000, "bonus_pct": 20},
    ]


async def test_purchases_off_until_the_scanner_runs(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/credits/purchase-tx", json={"address": str(Keypair().pubkey()), "lamports": PRICE}
    )
    assert response.status_code == 503


@pytest.fixture
async def shop(client: httpx.AsyncClient, pool: asyncpg.Pool) -> ChainRpc:
    rpc = ChainRpc()
    door = CreditsDoor(settings, rpc, HOT.pubkey(), PriceBook(settings, StaticOracle("100")))  # type: ignore[arg-type]
    door.attach(pool)
    assert door.scanner is not None
    await door.scanner.initialise()
    app.credits = door
    return rpc


async def test_purchase_transaction_pays_the_hot_wallet(client: httpx.AsyncClient, shop: ChainRpc) -> None:
    buyer = Keypair()
    response = await client.post(
        "/v1/credits/purchase-tx", json={"address": str(buyer.pubkey()), "lamports": 15_000_000}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["hot_wallet"] == str(HOT.pubkey())
    expected = f"solana:{HOT.pubkey()}?amount=0.015&label=Veridex&message=Veridex%20credits"
    assert body["solana_pay_url"] == f"{expected}&memo=veridex%3Acredit%3A{buyer.pubkey()}"
    tx = VersionedTransaction.from_bytes(base64.b64decode(body["transaction"]))
    keys = tx.message.account_keys
    assert keys[0] == buyer.pubkey() and HOT.pubkey() in keys
    assert (await client.get("/v1/pricing")).json()["hot_wallet"] == str(HOT.pubkey())


@pytest.mark.parametrize("lamports", [1, 10**13])
async def test_purchase_amount_bounds(client: httpx.AsyncClient, shop: ChainRpc, lamports: int) -> None:
    response = await client.post(
        "/v1/credits/purchase-tx", json={"address": str(Keypair().pubkey()), "lamports": lamports}
    )
    assert response.status_code == 400


async def test_solana_pay_qr(client: httpx.AsyncClient, shop: ChainRpc) -> None:
    response = await client.get("/v1/credits/solana-pay.svg", params={"lamports": 15_000_000})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.startswith("<svg")


# ── Scanner ───────────────────────────────────────────────────────────────────


async def test_scanner_credits_incoming_transfers_exactly_once(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    rpc.add("funding", transfer("owner", str(HOT.pubkey()), 15_000_000))  # before the scanner: not credits
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings.credit_bonus_tiers, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()

    hot, cold = str(HOT.pubkey()), "cold"
    rpc.add("buy", transfer("alice", hot, 10_000_000))
    rpc.add("big", transfer("bob", hot, 100_000_000))  # $100 at $1,000/SOL: the ≥$25 tier, +20%
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
    restarted = CreditScanner(rpc, pool, HOT.pubkey(), settings.credit_bonus_tiers, book=SOL_BOOK)  # type: ignore[arg-type]
    assert await restarted.scan_once() == 0  # the cursor survives restarts

    assert await balance(pool, "alice") == 11_000_000  # $10 at $1,000/SOL: the ≥$5 tier, +10%
    assert await balance(pool, "bob") == 120_000_000
    assert await balance(pool, "vault") == 3_000_000
    assert await balance(pool, "owner") is None
    assert await balance(pool, hot) is None


async def test_scanner_skips_zero_value_transfers(pool: asyncpg.Pool) -> None:
    # Regression: one 0-lamport transfer to the hot wallet (costs the sender a fee) stalled the scanner for good.
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings.credit_bonus_tiers, book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    hot = str(HOT.pubkey())
    rpc.add("zero", transfer("griefer", hot, 0))
    rpc.add("buy", transfer("alice", hot, 5_000_000))

    assert await scanner.scan_once() == 1
    assert await balance(pool, "griefer") is None
    assert await balance(pool, "alice") == 5_500_000  # $5: reaches the ≥$5 tier, +10%
    assert await scanner.scan_once() == 0  # the cursor moved past both


async def test_scanner_pages_through_a_backlog(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.credits.scanner._PAGE", 3)
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    for i in range(8):
        rpc.add(f"t{i}", transfer("carol", str(HOT.pubkey()), 1_000_000))

    assert await scanner.scan_once() == 8
    assert await balance(pool, "carol") == 8_000_000


async def test_scanner_waits_for_unretrievable_transactions(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("t1", transfer("dave", str(HOT.pubkey()), 1_000_000))
    rpc.chain.append(("t2", None, None))  # listed but not retrievable yet
    rpc.add("t3", transfer("dave", str(HOT.pubkey()), 2_000_000))

    assert await scanner.scan_once() == 1
    rpc.chain[1] = ("t2", transfer("dave", str(HOT.pubkey()), 4_000_000), None)
    assert await scanner.scan_once() == 2
    assert await balance(pool, "dave") == 7_000_000


async def test_two_scanners_never_double_credit(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    one = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
    await one.initialise()
    two = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
    for i in range(5):
        rpc.add(f"t{i}", transfer("erin", str(HOT.pubkey()), 1_000_000))

    await asyncio.gather(one.scan_once(), two.scan_once())
    await one.scan_once()
    assert await balance(pool, "erin") == 5_000_000


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
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    member = str(Keypair().pubkey())
    vault_payment = transfer("vault-pda", str(HOT.pubkey()), 2_000_000, inner=True)  # paid by a program (CPI)
    rpc.add("vault", with_memos(vault_payment, f"veridex:credit:{member}"))

    assert await scanner.scan_once() == 1
    assert await balance(pool, member) == 2_000_000
    assert await balance(pool, "vault-pda") is None


@pytest.mark.parametrize(
    "memos",
    [("veridex:credit:garbage",), (f"veridex:credit:{Keypair().pubkey()}", f"veridex:credit:{Keypair().pubkey()}")],
)
async def test_bad_or_ambiguous_memos_credit_the_source(pool: asyncpg.Pool, memos: tuple[str, ...]) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("t", with_memos(transfer("frank", str(HOT.pubkey()), 3_000_000), *memos))

    await scanner.scan_once()
    assert await balance(pool, "frank") == 3_000_000


async def test_purchase_transaction_names_the_beneficiary(client: httpx.AsyncClient, shop: ChainRpc) -> None:
    vault, member = Keypair().pubkey(), Keypair().pubkey()
    body = {"address": str(vault), "lamports": 15_000_000, "beneficiary": str(member)}
    response = await client.post("/v1/credits/purchase-tx", json=body)

    assert response.status_code == 200
    message = VersionedTransaction.from_bytes(base64.b64decode(response.json()["transaction"])).message
    assert message.account_keys[0] == vault
    assert bytes(message.instructions[1].data) == f"veridex:credit:{member}".encode()
    assert response.json()["solana_pay_url"].endswith(f"&memo=veridex%3Acredit%3A{member}")
    bad = await client.post("/v1/credits/purchase-tx", json={**body, "beneficiary": "nope"})
    assert bad.status_code == 400


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
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [], book=SOL_BOOK)  # type: ignore[arg-type]
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
    tiers = settings.credit_bonus_tiers
    return CreditScanner(rpc, pool, HOT.pubkey(), tiers, book=book, usdc_mint=USDC)  # type: ignore[arg-type]


async def test_usdc_purchases_are_credited_exactly_once_at_face_value(pool: asyncpg.Pool) -> None:
    rpc = UsdcChainRpc()
    scanner = usdc_scanner(rpc, pool, StaticOracle("100"))
    await scanner.initialise()
    assert scanner.name.startswith("credits-usdc:")  # its own cursor, next to the SOL scanner's

    rpc.add("buy", usdc_transfer("alice", 1_500_000))  # $1.50
    rpc.add("big", usdc_transfer("bob", 6_000_000))  # $6: reaches the ≥$5 tier, +10%
    rpc.add("x402", usdc_transfer("carol", 150_000, fee_payer=str(HOT.pubkey())))  # a settlement we paid for

    assert await scanner.scan_once() == 2
    assert await scanner.scan_once() == 0
    assert await usdc_scanner(rpc, pool, StaticOracle("100")).scan_once() == 0  # the cursor survives restarts
    assert await balance(pool, "alice") == 1_500_000
    assert await balance(pool, "bob") == 6_600_000
    assert await balance(pool, "carol") is None


async def test_sol_purchases_wait_for_a_fresh_price_rather_than_guess(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    oracle = StaticOracle(None)
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [], book=PriceBook(settings, oracle))  # type: ignore[arg-type]
    await scanner.initialise()
    rpc.add("buy", transfer("alice", str(HOT.pubkey()), 10_000_000))

    assert await scanner.scan_once() == 0  # stale oracle: not credited, the cursor stays before it
    assert await balance(pool, "alice") is None
    oracle.value = "150"
    assert await scanner.scan_once() == 1
    assert await balance(pool, "alice") == 1_500_000  # 0.01 SOL at $150/SOL = $1.50


# ── Dollar balances and the trial flag (migration 5) ─────────────────────────


async def test_a_purchase_or_a_memo_doesnt_use_up_the_trial(pool: asyncpg.Pool) -> None:
    # Regression: a row used to mean "trial spent", so 1 lamport with a memo naming someone cancelled their trial.
    await ledger.credit(pool, "wallet", 100)
    assert not await ledger.trial_used(pool, "wallet")
    assert await ledger.grant_trial(pool, "wallet", 750_000)
    assert await ledger.trial_used(pool, "wallet")
    assert not await ledger.grant_trial(pool, "wallet", 750_000)  # at most once
    assert await balance(pool, "wallet") == 750_100


async def test_migration_5_keeps_every_wallets_number_of_checks(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DROP TABLE IF EXISTS wallets, scan_cursor, fact_cache, usage_counters, price_observations, shop_links, "
        "shops, schema_migrations"
    )
    await migrate(pool, MIGRATIONS[:4])
    await pool.execute("INSERT INTO wallets (pubkey, balance_lamports) VALUES ('a', 4500000), ('b', 1499999)")

    assert await migrate(pool) == [v for v, _, _ in MIGRATIONS[4:]]
    assert await balance(pool, "a") == 450_000  # 3 checks before (1.5M lamports each), 3 checks now ($0.15 each)
    assert await balance(pool, "b") == 149_999
    assert await ledger.trial_used(pool, "a")  # existing rows had their trial
