"""x402 in USDC and wSOL: dollar prices, USDC charged exactly, wSOL converted at quote time within a tolerance."""

from decimal import Decimal
from typing import Any

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import (
    FakeRpc,
    StaticOracle,
    configured,
    payment_header,
    payment_instructions,
    sign_payment,
)
from veridex.main import app
from veridex.prices import PriceBook
from veridex.x402.payment import PaymentError, decode_header
from veridex.x402.service import X402
from veridex.x402.solana import USDC_MINTS, WSOL_MINT, associated_token_address
from veridex.x402.wallet import check_sweep, create_ata_idempotent

HOT = Keypair()
COLD = Keypair().pubkey()
CLIENT = Keypair()
USDC = USDC_MINTS["solana-devnet"]
USDC_ATA = associated_token_address(HOT.pubkey(), USDC)


class TokenRpc(FakeRpc):
    """FakeRpc with a separate USDC account (``usdc``; ``None`` = it doesn't exist) and per-account simulation."""

    def __init__(self, usdc: int | None = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.usdc = usdc

    async def token_balance(self, account: Pubkey) -> int | None:
        return self.usdc if account == USDC_ATA else self.wsol

    async def simulate_accounts(
        self, raw_tx: bytes, addresses: list[Pubkey]
    ) -> tuple[Any, list[dict[str, Any] | None]]:
        self.simulated.append(raw_tx)
        self._accounts = addresses
        before = await self.token_balance(addresses[0]) or 0
        token = {"tokenAmount": {"amount": str(before + self.sim_delta)}}
        return self.sim_error, [
            {"lamports": 2_039_280, "data": {"parsed": {"info": token}}},
            {"lamports": self.sol - self.sim_debit, "data": ["", "base64"]},
        ]


def service(rpc: FakeRpc, usd_per_sol: str | None = "100") -> X402:
    settings = configured(HOT, COLD)
    return X402(settings, rpc=rpc, prices=PriceBook(settings, StaticOracle(usd_per_sol)))


async def test_both_are_offered_at_the_dollar_price() -> None:
    accepts = await service(TokenRpc()).accepts("deep")
    assert [a["asset"] for a in accepts] == [str(WSOL_MINT), str(USDC)]
    assert accepts[0]["amount"] == "1500000"  # $0.15 at $100/SOL
    assert accepts[1]["amount"] == "150000"  # exactly $0.15
    assert accepts[1]["payTo"] == str(HOT.pubkey())


async def test_usdc_is_withheld_until_its_account_exists() -> None:
    assert [a["asset"] for a in await service(TokenRpc(usdc=None)).accepts("quick")] == [str(WSOL_MINT)]


async def test_wsol_is_withheld_while_the_oracle_is_unavailable() -> None:
    assert [a["asset"] for a in await service(TokenRpc(), usd_per_sol=None).accepts("quick")] == [str(USDC)]


async def test_nothing_is_offered_without_oracle_or_usdc_account() -> None:
    assert await service(TokenRpc(usdc=None), usd_per_sol=None).accepts("quick") == []


def usdc_payment(x402: X402, accepts: list[dict[str, Any]], amount: int, **overrides: Any) -> str:
    accepted = {**accepts[1], "amount": str(amount), **overrides}
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), amount, mint=USDC, destination=USDC_ATA)
    return payment_header(sign_payment(ixs, HOT.pubkey(), [CLIENT]), accepted)


def wsol_payment(accepts: list[dict[str, Any]], amount: int) -> str:
    ixs = payment_instructions(CLIENT.pubkey(), HOT.pubkey(), amount)
    return payment_header(sign_payment(ixs, HOT.pubkey(), [CLIENT]), {**accepts[0], "amount": str(amount)})


@pytest.mark.parametrize(
    ("amount", "ok"), [(1_500_000, True), (1_470_000, True), (1_530_000, True), (1_460_000, False)]
)
async def test_wsol_amount_may_drift_within_the_tolerance(amount: int, ok: bool) -> None:
    x402 = service(TokenRpc())
    accepts = await x402.accepts("deep")
    header = wsol_payment(accepts, amount)
    if ok:
        pre = x402.precheck(header, "deep", accepts)
        assert (pre.mint, pre.amount) == (WSOL_MINT, amount)
    else:
        with pytest.raises(PaymentError, match="requirements_mismatch"):
            x402.precheck(header, "deep", accepts)


async def test_usdc_amount_stays_exact() -> None:
    x402 = service(TokenRpc())
    accepts = await x402.accepts("deep")
    with pytest.raises(PaymentError, match="requirements_mismatch"):
        x402.precheck(usdc_payment(x402, accepts, 149_999), "deep", accepts)


async def test_other_mints_and_pay_to_addresses_are_refused() -> None:
    x402 = service(TokenRpc())
    accepts = await x402.accepts("deep")
    for override in ({"asset": str(Keypair().pubkey())}, {"payTo": str(Keypair().pubkey())}):
        with pytest.raises(PaymentError, match="requirements_mismatch"):
            x402.precheck(usdc_payment(x402, accepts, 150_000, **override), "deep", accepts)


async def test_usdc_payment_settles_into_the_usdc_account() -> None:
    rpc = TokenRpc(usdc=0)
    rpc.sim_delta = 150_000
    x402 = service(rpc)
    accepts = await x402.accepts("deep")
    settlement = await x402.settle(x402.precheck(usdc_payment(x402, accepts, 150_000), "deep", accepts))
    assert (settlement.mint, settlement.amount) == (USDC, 150_000)
    assert rpc._accounts[0] == USDC_ATA  # the simulation looked at the USDC account, not wSOL's


async def test_usdc_payment_short_in_simulation_is_refused() -> None:
    rpc = TokenRpc(usdc=0)
    rpc.sim_delta = 140_000  # the transaction claims 150_000 but delivers less
    x402 = service(rpc)
    accepts = await x402.accepts("deep")
    with pytest.raises(PaymentError, match="wrong_amount"):
        await x402.settle(x402.precheck(usdc_payment(x402, accepts, 150_000), "deep", accepts))
    assert rpc.sent == []


async def test_sweep_job_creates_the_missing_usdc_account() -> None:
    rpc = TokenRpc(usdc=None, sol=20_000_000, wsol=0)
    x402 = service(rpc)
    assert await x402.sweep_once()
    (raw,) = rpc.sent
    tx = VersionedTransaction.from_bytes(raw)
    check_sweep(tx.message, HOT.pubkey(), COLD, USDC)
    assert USDC_ATA in tx.message.account_keys


async def test_both_accounts_present_means_nothing_to_maintain() -> None:
    rpc = TokenRpc(usdc=0, sol=20_000_000, wsol=0)
    assert await service(rpc).sweep_once() is None
    assert rpc.sent == []


def test_a_sweep_may_create_only_the_configured_usdc_account() -> None:
    other_mint = Keypair().pubkey()
    ixs = [
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey()),
        create_ata_idempotent(HOT.pubkey(), HOT.pubkey(), other_mint),
    ]
    message = MessageV0.try_compile(HOT.pubkey(), ixs, [], Hash.default())
    with pytest.raises(PermissionError):
        check_sweep(message, HOT.pubkey(), COLD, USDC)
    with pytest.raises(PermissionError):
        check_sweep(
            MessageV0.try_compile(
                HOT.pubkey(), ixs[:1] + [create_ata_idempotent(HOT.pubkey(), HOT.pubkey(), USDC)], [], Hash.default()
            ),
            HOT.pubkey(),
            COLD,
        )


@pytest.mark.parametrize(("usdc", "assets"), [(None, [WSOL_MINT]), (0, [WSOL_MINT, USDC])])
async def test_the_402_offers_usdc_once_its_account_exists(
    usdc: int | None, assets: list[Pubkey], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app, "x402", service(TokenRpc(usdc=usdc)))
    body = {"url": "https://shop.test/p/1", "page_content": "<h1>x</h1>", "tier": "quick", "policy": "balanced"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/check", json=body)
    assert response.status_code == 402
    offered = decode_header(response.headers["PAYMENT-REQUIRED"])["accepts"]
    assert [o["asset"] for o in offered] == [str(a) for a in assets]


def test_quote_tolerance_is_two_percent_by_default() -> None:
    assert configured(HOT, COLD).quote_tolerance_bps == 200
    assert Decimal(200) / 10_000 == Decimal("0.02")
