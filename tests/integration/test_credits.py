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
from veridex.auth import get_credits
from veridex.config import settings
from veridex.credits import CreditsDoor, siws
from veridex.credits.scanner import CreditScanner
from veridex.main import app
from veridex.routes.check import _refund_as_credits

HOT = Keypair()
PRICE = settings.analysis_price_lamports


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
    value = await pool.fetchval("SELECT balance_lamports FROM wallets WHERE pubkey = $1", address)
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
            chain_id=fields["chain_id"],
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
    assert credits.json() == {"address": str(keypair.pubkey()), "balance_lamports": 0, "checks_remaining": 0}


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
    assert body["analysis_lamports"] == PRICE
    assert body["hot_wallet"] is None
    assert body["suggested_packs"][0] == {"checks": 10, "lamports": 10 * PRICE}
    assert body["bonuses"] == [
        {"min_lamports": 50_000_000, "bonus_pct": 10},
        {"min_lamports": 250_000_000, "bonus_pct": 20},
    ]


async def test_purchases_off_until_the_scanner_runs(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/credits/purchase-tx", json={"address": str(Keypair().pubkey()), "lamports": PRICE}
    )
    assert response.status_code == 503


@pytest.fixture
async def shop(client: httpx.AsyncClient, pool: asyncpg.Pool) -> ChainRpc:
    rpc = ChainRpc()
    door = CreditsDoor(settings, rpc, HOT.pubkey())  # type: ignore[arg-type]
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
    assert body["solana_pay_url"] == f"solana:{HOT.pubkey()}?amount=0.015&label=Veridex&message=Veridex%20credits"
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
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), settings.credit_bonus_tiers)  # type: ignore[arg-type]
    await scanner.initialise()

    hot, cold = str(HOT.pubkey()), "cold"
    rpc.add("buy", transfer("alice", hot, 10_000_000))
    rpc.add("big", transfer("bob", hot, 100_000_000))  # ≥0.05 SOL: +10%
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
    restarted = CreditScanner(rpc, pool, HOT.pubkey(), settings.credit_bonus_tiers)  # type: ignore[arg-type]
    assert await restarted.scan_once() == 0  # the cursor survives restarts

    assert await balance(pool, "alice") == 10_000_000
    assert await balance(pool, "bob") == 110_000_000
    assert await balance(pool, "vault") == 3_000_000
    assert await balance(pool, "owner") is None
    assert await balance(pool, hot) is None


async def test_scanner_pages_through_a_backlog(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("veridex.credits.scanner._PAGE", 3)
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [])  # type: ignore[arg-type]
    await scanner.initialise()
    for i in range(8):
        rpc.add(f"t{i}", transfer("carol", str(HOT.pubkey()), 1_000_000))

    assert await scanner.scan_once() == 8
    assert await balance(pool, "carol") == 8_000_000


async def test_scanner_waits_for_unretrievable_transactions(pool: asyncpg.Pool) -> None:
    rpc = ChainRpc()
    scanner = CreditScanner(rpc, pool, HOT.pubkey(), [])  # type: ignore[arg-type]
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
    one = CreditScanner(rpc, pool, HOT.pubkey(), [])  # type: ignore[arg-type]
    await one.initialise()
    two = CreditScanner(rpc, pool, HOT.pubkey(), [])  # type: ignore[arg-type]
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
