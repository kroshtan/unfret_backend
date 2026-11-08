"""USDC→SOL swaps of the hot wallet: verified by simulation before signing, and recognised by the outflow audit."""

import base64
from typing import Any

import httpx
import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import StaticOracle, configured
from veridex.prices import PriceBook
from veridex.x402.service import X402
from veridex.x402.solana import USDC_MINTS, WSOL_MINT, SolanaRpc, associated_token_address
from veridex.x402.swap import SYSTEM_PROGRAM, JupiterClient, SwapError, VerifiedSwap, verify_swap
from veridex.x402.wallet import HotWallet, is_swap_into_sol, unexpected_outflows

HOT = Keypair()
COLD = Keypair().pubkey()
USDC = USDC_MINTS["solana-mainnet"]
USDC_ATA = associated_token_address(HOT.pubkey(), USDC)
WSOL_ATA = associated_token_address(HOT.pubkey())
DEX = Pubkey.from_string("JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4")  # Jupiter's aggregator program
POOL = Keypair().pubkey()


def swap_tx(*, cu_price: int = 250_000, extra_signer: Pubkey | None = None, program: Pubkey = DEX) -> bytes:
    """A Jupiter-like swap: compute budget, one aggregator instruction over the hot wallet's accounts and a pool."""
    accounts = [
        AccountMeta(HOT.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(USDC_ATA, is_signer=False, is_writable=True),
        AccountMeta(WSOL_ATA, is_signer=False, is_writable=True),
        AccountMeta(POOL, is_signer=False, is_writable=True),
    ]
    if extra_signer:
        accounts.append(AccountMeta(extra_signer, is_signer=True, is_writable=False))
    ixs = [
        set_compute_unit_limit(400_000),
        set_compute_unit_price(cu_price),
        Instruction(program, b"\x01route", accounts),
    ]
    message = MessageV0.try_compile(HOT.pubkey(), ixs, [], Hash.default())
    sigs = message.header.num_required_signatures
    return bytes(VersionedTransaction.populate(message, [Signature.default()] * sigs))


def token(holder: Pubkey, token_mint: Pubkey, amount: int, **overrides: Any) -> dict[str, Any]:
    info = {
        "owner": str(holder),
        "mint": str(token_mint),
        "state": "initialized",
        "tokenAmount": {"amount": str(amount)},
    }
    return {
        "lamports": 2_039_280,
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"info": {**info, **overrides}}},
    }


class SwapRpc(SolanaRpc):
    """Balances before, and the post-state a simulation shows (configurable per test)."""

    def __init__(self, *, sol: int = 20_000_000, usdc: int = 5_000_000, wsol: int = 0) -> None:
        super().__init__("http://rpc.invalid")
        self.sol, self.usdc, self.wsol = sol, usdc, wsol
        self.usdc_spent, self.wsol_got, self.sol_debit = usdc, 49_900_000, 105_000
        self.usdc_overrides: dict[str, Any] = {}
        self.wsol_overrides: dict[str, Any] = {}
        self.hot_owner = SYSTEM_PROGRAM
        self.sent: list[bytes] = []

    async def get_balance(self, address: Pubkey) -> int:
        return self.sol

    async def token_balance(self, account: Pubkey) -> int | None:
        return {USDC_ATA: self.usdc, WSOL_ATA: self.wsol}.get(account)

    async def simulate_accounts(
        self, raw_tx: bytes, addresses: list[Pubkey]
    ) -> tuple[Any, list[dict[str, Any] | None], list[int]]:
        assert addresses == [HOT.pubkey(), USDC_ATA, WSOL_ATA]
        return (
            None,
            [
                {"lamports": self.sol - self.sol_debit, "owner": self.hot_owner, "data": ["", "base64"]},
                token(HOT.pubkey(), USDC, self.usdc - self.usdc_spent, **self.usdc_overrides),
                token(HOT.pubkey(), WSOL_MINT, self.wsol + self.wsol_got, **self.wsol_overrides),
            ],
            [],
        )

    async def send_and_confirm(self, raw_tx: bytes, timeout_s: float) -> str:
        self.sent.append(raw_tx)
        return str(VersionedTransaction.from_bytes(raw_tx).signatures[0])


async def verify(rpc: SwapRpc, raw: bytes | None = None, *, min_out: int = 49_401_000) -> VerifiedSwap:
    return await verify_swap(
        raw or swap_tx(),
        rpc=rpc,
        hot=HOT.pubkey(),
        usdc_mint=USDC,
        amount_in=5_000_000,
        min_out=min_out,
        max_compute_units=1_400_000,
        max_priority_lamports=100_000,
    )


async def test_a_good_route_is_verified_and_signed_by_the_hot_wallet_alone() -> None:
    swap = await verify(SwapRpc())
    raw = HotWallet(HOT, COLD, USDC).sign(swap)
    tx = VersionedTransaction.from_bytes(raw)
    assert tx.signatures[0].verify(HOT.pubkey(), to_bytes_versioned(tx.message))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"wsol_got": 49_000_000}, "below the minimum"),  # slippage beyond the bound
        ({"usdc_spent": 6_000_000}, "wrong USDC amount"),  # takes more USDC than agreed
        ({"usdc_spent": 4_000_000}, "wrong USDC amount"),
        ({"sol_debit": 2_000_000}, "more than the fee"),  # an extra SOL transfer out
        ({"wsol_got": 0}, "below the minimum"),  # the output goes elsewhere
        ({"usdc_overrides": {"delegate": str(Keypair().pubkey())}}, "delegate"),  # Approve
        ({"wsol_overrides": {"closeAuthority": str(Keypair().pubkey())}}, "close authority"),  # SetAuthority
        ({"usdc_overrides": {"owner": str(Keypair().pubkey())}}, "changed hands"),  # SetAuthority (owner)
        ({"hot_owner": str(DEX)}, "reassign"),  # Assign of the hot wallet itself
    ],
)
async def test_unsafe_swaps_are_refused(change: dict[str, Any], reason: str) -> None:
    rpc = SwapRpc()
    for key, value in change.items():
        setattr(rpc, key, value)
    with pytest.raises(SwapError, match=reason):
        await verify(rpc)


async def test_a_second_signer_or_a_high_priority_fee_is_refused() -> None:
    with pytest.raises(SwapError, match="only signer"):
        await verify(SwapRpc(), swap_tx(extra_signer=Keypair().pubkey()))
    with pytest.raises(SwapError, match="priority"):
        await verify(SwapRpc(), swap_tx(cu_price=300_000))  # 400k units × 0.3 lamports > 100_000


async def test_only_jupiters_aggregator_may_run_at_top_level() -> None:
    # The balances would look right, but an unknown program holding the hot wallet's signature could do anything.
    with pytest.raises(SwapError, match="program not allowed"):
        await verify(SwapRpc(), swap_tx(program=Keypair().pubkey()))


def test_a_swap_cannot_be_forged_around_verification() -> None:
    message = VersionedTransaction.from_bytes(swap_tx()).message
    assert isinstance(message, MessageV0)
    with pytest.raises(TypeError):
        VerifiedSwap(message=message, amount_in=1, min_out=1, _token=object())
    with pytest.raises(PermissionError):
        HotWallet(HOT, COLD, USDC).sign(message)  # type: ignore[arg-type]


# ── The swap job ─────────────────────────────────────────────────────────────


def jupiter(out_amount: int, calls: list[str]) -> JupiterClient:
    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/quote"):
            assert request.url.params["inputMint"] == str(USDC)
            assert request.url.params["outputMint"] == str(WSOL_MINT)
            return httpx.Response(200, json={"inAmount": "5000000", "outAmount": str(out_amount)})
        body = httpx.Request("POST", "http://x", content=request.content).read()
        assert b'"wrapAndUnwrapSol":false' in body.replace(b" ", b"")
        assert str(WSOL_ATA).encode() in body  # output into the hot wallet's own wSOL account
        return httpx.Response(200, json={"swapTransaction": base64.b64encode(swap_tx()).decode()})

    return JupiterClient("https://jup.test/swap/v1", transport=httpx.MockTransport(handle))


def mainnet_service(rpc: SwapRpc, out_amount: int, calls: list[str], usd_per_sol: str | None = "100") -> X402:
    settings = configured(HOT, COLD, solana_network="solana-mainnet", x402_allow_mainnet=True)
    x402 = X402(settings, rpc=rpc, prices=PriceBook(settings, StaticOracle(usd_per_sol)))
    x402.swapper = jupiter(out_amount, calls)
    return x402


async def test_due_usdc_is_swapped_into_the_wsol_account() -> None:
    rpc, calls = SwapRpc(), list[str]()
    x402 = mainnet_service(rpc, 49_900_000, calls)  # 5 USDC at $100 = 50_000_000 lamports by the oracle
    assert await x402.swap_once()
    assert calls == ["/swap/v1/quote", "/swap/v1/swap"]
    assert len(rpc.sent) == 1


async def test_a_quote_below_the_oracle_floor_is_not_taken() -> None:
    rpc, calls = SwapRpc(), list[str]()
    x402 = mainnet_service(rpc, 40_000_000, calls)  # 20% under the oracle: someone's pool, not the market
    for _ in range(3):
        with pytest.raises(SwapError, match="oracle floor"):
            await x402.swap_once()
    assert rpc.sent == [] and calls == ["/swap/v1/quote"] * 3
    assert "swap_failing" in (await x402.health()).alerts


async def test_no_swap_without_an_oracle_price_below_the_threshold_or_on_devnet() -> None:
    calls: list[str] = []
    with pytest.raises(SwapError):
        await mainnet_service(SwapRpc(), 49_900_000, calls, usd_per_sol=None).swap_once()
    assert await mainnet_service(SwapRpc(usdc=1_000_000), 49_900_000, calls).swap_once() is None  # below 5 USDC
    devnet = X402(configured(HOT, COLD), rpc=SwapRpc())
    assert devnet.swaps_enabled is False and await devnet.swap_once() is None
    assert calls == []


# ── The outflow audit ────────────────────────────────────────────────────────


def landed(
    *, usdc_delta: int, wsol_delta: int, fee: int = 5_000, sol_extra: int = 0, foreign: bool = False
) -> dict[str, Any]:
    hot = str(HOT.pubkey())
    destination = str(Keypair().pubkey()) if foreign else str(POOL)
    transfer = {
        "program": "spl-token",
        "parsed": {
            "type": "transferChecked",
            "info": {"authority": hot, "destination": destination, "mint": str(USDC)},
        },
    }

    def balance(index: int, mint: Pubkey, amount: int) -> dict[str, Any]:
        return {"accountIndex": index, "mint": str(mint), "owner": hot, "uiTokenAmount": {"amount": str(amount)}}

    return {
        "transaction": {"message": {"accountKeys": [{"pubkey": hot, "signer": True}], "instructions": [transfer]}},
        "meta": {
            "err": None,
            "fee": fee,
            "preBalances": [20_000_000],
            "postBalances": [20_000_000 - fee - sol_extra],
            "preTokenBalances": [balance(1, USDC, 5_000_000), balance(2, WSOL_MINT, 0)],
            "postTokenBalances": [balance(1, USDC, 5_000_000 + usdc_delta), balance(2, WSOL_MINT, wsol_delta)],
            "innerInstructions": [],
        },
    }


def test_our_swaps_are_not_outflows() -> None:
    tx = landed(usdc_delta=-5_000_000, wsol_delta=49_900_000)
    assert is_swap_into_sol(tx, HOT.pubkey(), USDC, 10 * 0.5)
    assert unexpected_outflows(tx, HOT.pubkey(), COLD, USDC, 10 * 0.5) == []


@pytest.mark.parametrize(
    "tx",
    [
        landed(usdc_delta=-5_000_000, wsol_delta=0, foreign=True),  # USDC drained to a foreign account
        landed(usdc_delta=-5_000_000, wsol_delta=49_900_000, sol_extra=1_000_000),  # SOL lost besides the fee
        landed(usdc_delta=-5_000_000, wsol_delta=1_000),  # a "swap" at a ruinous rate
    ],
)
def test_fake_swaps_are_flagged(tx: dict[str, Any]) -> None:
    assert unexpected_outflows(tx, HOT.pubkey(), COLD, USDC, 10 * 0.5)
