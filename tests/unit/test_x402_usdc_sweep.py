"""USDC is swept to the cold wallet as USDC: never swapped, and nothing but that one shape is signed or tolerated."""

from typing import Any

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import BLOCKHASH, FakeRpc, StaticOracle, configured
from veridex.prices import PriceBook
from veridex.x402.service import X402
from veridex.x402.solana import (
    ASSOCIATED_TOKEN_PROGRAM,
    TOKEN_PROGRAM,
    USDC_MINTS,
    WSOL_MINT,
    associated_token_address,
    transfer_checked,
)
from veridex.x402.wallet import (
    HotWallet,
    build_sweep,
    build_usdc_sweep,
    check_sweep,
    create_ata_idempotent,
    unexpected_outflows,
)

HOT = Keypair()
COLD = Keypair().pubkey()
ELSEWHERE = Keypair().pubkey()
USDC = USDC_MINTS["solana-devnet"]
HOT_USDC = associated_token_address(HOT.pubkey(), USDC)
COLD_USDC = associated_token_address(COLD, USDC)
WALLET = HotWallet(HOT, COLD, USDC)


class UsdcRpc(FakeRpc):
    """FakeRpc with the hot wallet's USDC account (``usdc``) and the cold address's (``cold_usdc``; None = missing)."""

    def __init__(self, usdc: int | None = 0, cold_usdc: int | None = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.usdc, self.cold_usdc = usdc, cold_usdc

    async def token_balance(self, account: Pubkey) -> int | None:
        if account == HOT_USDC:
            return self.usdc
        if account == COLD_USDC:
            return self.cold_usdc
        return self.wsol


def service(rpc: FakeRpc, **overrides: Any) -> X402:
    settings = configured(HOT, COLD, **overrides)
    return X402(settings, rpc=rpc, prices=PriceBook(settings, StaticOracle("100")))


def sent_instructions(raw: bytes) -> list[tuple[Pubkey, list[Pubkey], bytes]]:
    message = VersionedTransaction.from_bytes(raw).message
    keys = message.account_keys
    return [
        (keys[ix.program_id_index], [keys[i] for i in bytes(ix.accounts)], bytes(ix.data))
        for ix in message.instructions
    ]


def test_a_usdc_sweep_creates_the_cold_account_and_moves_usdc_into_it() -> None:
    sweep = build_usdc_sweep(HOT.pubkey(), COLD, USDC, amount=7_250_000, blockhash=BLOCKHASH)
    check_sweep(sweep.message, HOT.pubkey(), COLD, USDC)
    assert sweep.usdc_to_cold == 7_250_000 and sweep.to_cold == 0
    (create, move) = sent_instructions(WALLET.sign(sweep))
    assert create[0] == ASSOCIATED_TOKEN_PROGRAM and create[1][:4] == [HOT.pubkey(), COLD_USDC, COLD, USDC]
    assert move[0] == TOKEN_PROGRAM and move[1] == [HOT_USDC, USDC, COLD_USDC, HOT.pubkey()]
    assert move[2][0] == 12 and int.from_bytes(move[2][1:9], "little") == 7_250_000 and move[2][9] == 6


def test_a_usdc_sweep_needs_a_positive_amount() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_usdc_sweep(HOT.pubkey(), COLD, USDC, amount=0, blockhash=BLOCKHASH)


def compile_message(*instructions: Any) -> MessageV0:
    return MessageV0.try_compile(HOT.pubkey(), list(instructions), [], Hash.default())


@pytest.mark.parametrize(
    "instructions",
    [
        # into another owner's USDC account
        [transfer_checked(HOT_USDC, USDC, associated_token_address(ELSEWHERE, USDC), HOT.pubkey(), 1_000)],
        # creating someone else's account first
        [
            create_ata_idempotent(HOT.pubkey(), ELSEWHERE, USDC),
            transfer_checked(HOT_USDC, USDC, associated_token_address(ELSEWHERE, USDC), HOT.pubkey(), 1_000),
        ],
        # another mint into cold's account for it
        [
            transfer_checked(
                associated_token_address(HOT.pubkey(), WSOL_MINT),
                WSOL_MINT,
                associated_token_address(COLD, WSOL_MINT),
                HOT.pubkey(),
                1_000,
            )
        ],
        # a zero transfer
        [transfer_checked(HOT_USDC, USDC, COLD_USDC, HOT.pubkey(), 0)],
        # the cold-account create alone, or twice the transfer
        [create_ata_idempotent(HOT.pubkey(), COLD, USDC)],
        [
            transfer_checked(HOT_USDC, USDC, COLD_USDC, HOT.pubkey(), 1),
            transfer_checked(HOT_USDC, USDC, COLD_USDC, HOT.pubkey(), 2),
        ],
    ],
    ids=["other_owner", "create_other_owner", "other_mint", "zero", "create_only", "twice"],
)
def test_check_sweep_refuses_any_other_usdc_shape(instructions: list[Any]) -> None:
    with pytest.raises(PermissionError):
        check_sweep(compile_message(*instructions), HOT.pubkey(), COLD, USDC)


def test_usdc_sweeps_are_refused_without_a_usdc_mint() -> None:
    sweep = build_usdc_sweep(HOT.pubkey(), COLD, USDC, amount=1_000, blockhash=BLOCKHASH)
    with pytest.raises(PermissionError):
        check_sweep(sweep.message, HOT.pubkey(), COLD)


def test_the_hot_wallet_refuses_a_usdc_sweep_to_a_non_cold_account() -> None:
    # A Sweep built for another "cold" address is re-checked against the wallet's own cold address at signing.
    sweep = build_usdc_sweep(HOT.pubkey(), ELSEWHERE, USDC, amount=1_000, blockhash=BLOCKHASH)
    with pytest.raises(PermissionError):
        WALLET.sign(sweep)
    sol_sweep = build_sweep(HOT.pubkey(), ELSEWHERE, ata_exists=True, to_cold=1_000, blockhash=BLOCKHASH)
    with pytest.raises(PermissionError):
        WALLET.sign(sol_sweep)


def test_the_hot_wallet_signs_nothing_else() -> None:
    for item in (object(), compile_message(transfer_checked(HOT_USDC, USDC, COLD_USDC, HOT.pubkey(), 5)), b"raw"):
        with pytest.raises(PermissionError, match="only verified payments and sweeps to cold"):
            WALLET.sign(item)  # type: ignore[arg-type]


async def test_the_job_sweeps_usdc_once_above_the_threshold() -> None:
    rpc = UsdcRpc(usdc=6_000_000, cold_usdc=None)
    x402 = service(rpc)
    assert await x402.sweep_usdc_once()
    (raw,) = rpc.sent
    tx = VersionedTransaction.from_bytes(raw)
    check_sweep(tx.message, HOT.pubkey(), COLD, USDC)
    moves = [ix for ix in sent_instructions(raw) if ix[0] == TOKEN_PROGRAM]
    assert int.from_bytes(moves[0][2][1:9], "little") == 6_000_000
    assert rpc.simulated == [raw]  # simulated before sending


@pytest.mark.parametrize("network", ["solana-devnet", "solana-mainnet"])
async def test_mainnet_and_devnet_sweep_alike(network: str) -> None:
    rpc = UsdcRpc(usdc=5_000_000)
    rpc_usdc = USDC_MINTS[network]
    rpc.usdc = None  # the account for the configured network's mint is read below
    x402 = service(rpc, solana_network=network, x402_allow_mainnet=True)

    async def token_balance(account: Pubkey) -> int | None:
        return 5_000_000 if account == associated_token_address(HOT.pubkey(), rpc_usdc) else 0

    rpc.token_balance = token_balance  # type: ignore[method-assign]
    assert x402.usdc_mint == rpc_usdc
    assert await x402.sweep_usdc_once()
    check_sweep(VersionedTransaction.from_bytes(rpc.sent[0]).message, HOT.pubkey(), COLD, rpc_usdc)


async def test_no_sweep_below_the_threshold_until_dust_is_old() -> None:
    rpc = UsdcRpc(usdc=1_000_000)
    x402 = service(rpc)
    assert await x402.sweep_usdc_once() is None
    x402._last_usdc_sweep -= x402.settings.usdc_sweep_max_interval_s + 1
    assert await x402.sweep_usdc_once()
    rpc.sent.clear()
    rpc.usdc = 100_000  # below the dust floor: never worth the fee
    x402._last_usdc_sweep -= x402.settings.usdc_sweep_max_interval_s + 1
    assert await x402.sweep_usdc_once() is None
    assert rpc.sent == []


async def test_no_sweep_without_usdc_or_its_account() -> None:
    for usdc in (None, 0):
        rpc = UsdcRpc(usdc=usdc)
        assert await service(rpc).sweep_usdc_once() is None
        assert rpc.sent == []


async def test_the_rent_for_cold_s_account_must_leave_the_fee_reserve() -> None:
    settings = configured(HOT, COLD)
    # Enough SOL for the fee buffer and reserve, but not also the rent of a new account: wait.
    short = settings.x402_min_fee_lamports + 10_000 + 1_000_000
    rpc = UsdcRpc(usdc=6_000_000, cold_usdc=None, sol=short)
    assert await service(rpc).sweep_usdc_once() is None
    assert rpc.sent == []
    rpc.cold_usdc = 0  # once the account exists there's no rent to pay
    assert await service(rpc).sweep_usdc_once()


async def test_usdc_piling_up_is_an_alert() -> None:
    x402 = service(UsdcRpc(usdc=16_000_000))
    assert "usdc_above_threshold" in (await x402.health()).alerts
    assert "usdc_above_threshold" not in (await service(UsdcRpc(usdc=6_000_000)).health()).alerts


def parsed(*instructions: dict[str, Any], inner: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    keys = [{"pubkey": str(HOT.pubkey()), "signer": True, "writable": True}]
    return {
        "transaction": {"message": {"accountKeys": keys, "instructions": list(instructions)}},
        "meta": {"err": None, "innerInstructions": [{"instructions": inner or []}]},
    }


def token_transfer(destination: Pubkey, mint: Pubkey = USDC, kind: str = "transferChecked") -> dict[str, Any]:
    info = {"authority": str(HOT.pubkey()), "source": str(HOT_USDC), "destination": str(destination)}
    if kind == "transferChecked":
        info["mint"] = str(mint)
    return {"program": "spl-token", "parsed": {"type": kind, "info": info}}


def test_our_usdc_sweep_is_not_an_outflow() -> None:
    rent = {
        "program": "system",
        "parsed": {"type": "createAccount", "info": {"source": str(HOT.pubkey()), "newAccount": str(COLD_USDC)}},
    }
    tx = parsed(token_transfer(COLD_USDC), inner=[rent])
    assert unexpected_outflows(tx, HOT.pubkey(), COLD, USDC) == []


@pytest.mark.parametrize(
    "tx",
    [
        parsed(token_transfer(associated_token_address(ELSEWHERE, USDC))),  # USDC to another owner
        parsed(token_transfer(COLD_USDC, kind="transfer")),  # unchecked: we never sign one
        parsed(token_transfer(COLD_USDC, mint=Keypair().pubkey())),  # another mint's "cold" account
        # A swap: USDC into a pool, wSOL back. The product never swaps, so this is an outflow now.
        parsed(
            token_transfer(Keypair().pubkey()),
            inner=[
                {
                    "program": "spl-token",
                    "parsed": {
                        "type": "transferChecked",
                        "info": {
                            "authority": str(Keypair().pubkey()),
                            "destination": str(associated_token_address(HOT.pubkey())),
                            "mint": str(WSOL_MINT),
                        },
                    },
                }
            ],
        ),
    ],
    ids=["other_owner", "unchecked", "other_mint", "swap"],
)
def test_other_token_transfers_by_the_hot_wallet_are_flagged(tx: dict[str, Any]) -> None:
    assert unexpected_outflows(tx, HOT.pubkey(), COLD, USDC)
