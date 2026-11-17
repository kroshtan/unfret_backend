"""Build real x402 payment transactions with solders for the tests."""

import base64
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import SecretStr
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from unfret.config import Settings
from unfret.prices import PriceBook, PriceUnavailableError, PythSolUsd, SolUsd
from unfret.x402.payment import encode_header, requirements
from unfret.x402.service import X402
from unfret.x402.solana import (
    CAIP2,
    MEMO_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
    SolanaRpc,
    associated_token_address,
    usdc_mint,
)

NETWORK = CAIP2["solana-devnet"]
BLOCKHASH = str(Hash.default())
CHECK_USD = Decimal("0.15")  # the price these tests are written for (the default is $0.20)


def transfer_checked(source: Pubkey, mint: Pubkey, destination: Pubkey, authority: Pubkey, amount: int) -> Instruction:
    data = bytes([12]) + amount.to_bytes(8, "little") + bytes([9])
    return Instruction(
        TOKEN_PROGRAM,
        data,
        [
            AccountMeta(source, is_signer=False, is_writable=True),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(destination, is_signer=False, is_writable=True),
            AccountMeta(authority, is_signer=True, is_writable=False),
        ],
    )


def memo(text: bytes = b"0123456789abcdef0123456789abcdef") -> Instruction:
    return Instruction(MEMO_PROGRAM, text, [])


def payment_instructions(
    client: Pubkey,
    pay_to: Pubkey,
    amount: int,
    *,
    mint: Pubkey = WSOL_MINT,
    destination: Pubkey | None = None,
    cu_price: int = 1_000,
) -> list[Instruction]:
    return [
        set_compute_unit_limit(200_000),
        set_compute_unit_price(cu_price),
        transfer_checked(
            associated_token_address(client),
            mint,
            destination or associated_token_address(pay_to),
            client,
            amount,
        ),
        memo(),
    ]


def sign_payment(
    instructions: list[Instruction], fee_payer: Pubkey, signers: list[Keypair], *, sign: bool = True
) -> VersionedTransaction:
    message = MessageV0.try_compile(fee_payer, instructions, [], Hash.default())
    by_key = {kp.pubkey(): kp for kp in signers}
    signatures = [Signature.default()]
    # Signatures go in the message's signer order, which compile sorts, not in the order the signers were given.
    for key in message.account_keys[1 : message.header.num_required_signatures]:
        kp = by_key.get(key)
        signatures.append(kp.sign_message(to_bytes_versioned(message)) if kp and sign else Signature.default())
    return VersionedTransaction.populate(message, signatures)


def payment_header(tx: VersionedTransaction, accepted: dict[str, Any]) -> str:
    return encode_header(
        {"x402Version": 2, "accepted": accepted, "payload": {"transaction": base64.b64encode(bytes(tx)).decode()}}
    )


def expected(hot: Pubkey, amount: int = 100_000) -> dict[str, Any]:
    """Requirements as the service issues them: the hot wallet is both payTo and fee payer."""
    return requirements(network=NETWORK, amount=amount, pay_to=hot, fee_payer=hot)


def configured(hot: Keypair, cold: Pubkey, **overrides: Any) -> Settings:
    # The x402 tests' arithmetic is written for $0.15 a deep check; test_prices pins the real default.
    overrides.setdefault("price_per_check_usd", CHECK_USD)
    return Settings(hot_wallet_key=SecretStr(str(hot)), cold_address=str(cold), **overrides)


ORACLE_NOW = 1_800_000_000


class StaticOracle(PythSolUsd):
    """A fixed SOL/USD price (``None``: the oracle refuses, as when stale)."""

    def __init__(self, usd_per_sol: str | None) -> None:
        self.value = usd_per_sol

    async def read(self, now: float | None = None) -> SolUsd:
        if self.value is None:
            raise PriceUnavailableError("oracle price is stale (99 s old)")
        return SolUsd(Decimal(self.value), Decimal("0.05"), ORACLE_NOW)


def make_x402(settings: Settings, rpc: SolanaRpc, usd_per_sol: str | None = "100") -> X402:
    """An X402 service with a fixed SOL price: at $100/SOL, $0.01 = 100,000 lamports and $0.15 = 1,500,000."""
    return X402(settings, rpc=rpc, prices=PriceBook(settings, StaticOracle(usd_per_sol)))


VAULT_PROGRAM = Pubkey.from_string("SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf")  # Squads v4's program id


def cpi_payment_instructions(
    member: Pubkey, pay_to: Pubkey, amount: int, *, cu_price: int = 1_000
) -> list[Instruction]:
    """A smart-wallet payment: the member signs, a vault program pays from its vault by CPI (Squads-style)."""
    vault = Pubkey.find_program_address([b"vault", bytes(member)], VAULT_PROGRAM)[0]
    execute = Instruction(
        VAULT_PROGRAM,
        amount.to_bytes(8, "little") + bytes([9]),
        [
            AccountMeta(member, is_signer=True, is_writable=False),
            AccountMeta(vault, is_signer=False, is_writable=False),
            AccountMeta(associated_token_address(vault), is_signer=False, is_writable=True),
            AccountMeta(WSOL_MINT, is_signer=False, is_writable=False),
            AccountMeta(associated_token_address(pay_to), is_signer=False, is_writable=True),
            AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ],
    )
    return [set_compute_unit_limit(200_000), set_compute_unit_price(cu_price), execute, memo()]


class FakeRpc(SolanaRpc):
    """
    Stands in for the Solana RPC. ``sim_delta``/``sim_debit`` set what a simulation shows for the hot wallet's
    wSOL account and SOL; ``landed_delta`` what a settled transaction's metadata shows (defaults to ``sim_delta``).
    """

    def __init__(self, sol: int = 50_000_000, wsol: int | None = 0) -> None:
        super().__init__("http://rpc.invalid")
        self.sol, self.wsol = sol, wsol
        self.sim_delta = 100_000
        self.sim_debit = 0
        self.sim_error: Any = None
        self.sim_logs: list[str] = []
        self.sim_inner: list[int] | None = None
        self.failed: set[str] = set()
        self.unfinalized: set[str] = set()  # confirmed only: listed at "confirmed", not at "finalized" commitment
        self.statuses: dict[str, dict[str, Any]] = {}  # signature → getSignatureStatuses entry
        self.landed_delta: int | None = None
        self.landed_fee = 10_000
        self.sent: list[bytes] = []
        self.simulated: list[bytes] = []
        self.fail_with: Exception | None = None
        self.signatures: list[str] = []
        self.transactions: dict[str, dict[str, Any]] = {}
        self._accounts: list[Pubkey] = []

    async def get_balance(self, address: Pubkey) -> int:
        return self.sol

    async def token_balance(self, account: Pubkey) -> int | None:
        return self.wsol

    async def rent_exempt_minimum(self, size: int) -> int:
        return 2_039_280

    async def latest_blockhash(self) -> str:
        return BLOCKHASH

    async def balances(self, owner: Pubkey, token_accounts: list[Pubkey]) -> tuple[int, list[int | None]]:
        return await self.get_balance(owner), [await self.token_balance(a) for a in token_accounts]

    async def signature_status(self, signature: str) -> dict[str, Any] | None:
        return self.statuses.get(signature)

    async def recent_signatures(self, address: Pubkey, limit: int = 20) -> list[str]:
        return self.signatures

    async def signature_page(
        self,
        address: Pubkey,
        *,
        before: str | None = None,
        until: str | None = None,
        limit: int = 1000,
        commitment: str = "finalized",
    ) -> list[dict[str, Any]]:
        """``signatures`` (newest first) as ``getSignaturesForAddress`` pages them; ``failed`` ones carry an error."""
        assert commitment in {"confirmed", "finalized"}
        page = [s for s in self.signatures if commitment == "confirmed" or s not in self.unfinalized]
        if until in page:
            page = page[: page.index(until)]
        if before in page:
            page = page[page.index(before) + 1 :]
        return [{"signature": s, "err": {"x": 1} if s in self.failed else None} for s in page[:limit]]

    async def simulate_accounts(
        self, raw_tx: bytes, addresses: list[Pubkey]
    ) -> tuple[Any, list[dict[str, Any] | None], list[int], list[str]]:
        self.simulated.append(raw_tx)
        self._accounts = addresses
        token = {"tokenAmount": {"amount": str((self.wsol or 0) + self.sim_delta)}}
        return (
            self.sim_error,
            [
                {"lamports": 2_039_280, "data": {"parsed": {"info": token}}},
                {"lamports": self.sol - self.sim_debit, "data": ["", "base64"]},
            ],
            self.inner_programs(raw_tx),
            self.sim_logs,
        )

    def inner_programs(self, raw_tx: bytes) -> list[int]:
        """What a CPI payment invokes: the token program, by index (``sim_inner`` overrides it)."""
        if self.sim_inner is not None:
            return self.sim_inner
        keys = list(VersionedTransaction.from_bytes(raw_tx).message.account_keys)
        return [keys.index(TOKEN_PROGRAM)] if TOKEN_PROGRAM in keys else []

    async def parsed_transaction(self, signature: str, commitment: str = "confirmed") -> dict[str, Any] | None:
        if signature in self.transactions or not self._accounts:
            return self.transactions.get(signature)
        account, hot = (str(a) for a in self._accounts)
        delta = self.sim_delta if self.landed_delta is None else self.landed_delta
        before = self.wsol or 0
        return {
            "transaction": {"message": {"accountKeys": [{"pubkey": hot}, {"pubkey": account}]}},
            "meta": {
                "fee": self.landed_fee,
                "preBalances": [self.sol, 2_039_280],
                "postBalances": [self.sol - self.landed_fee, 2_039_280],
                "preTokenBalances": [{"accountIndex": 1, "uiTokenAmount": {"amount": str(before)}}],
                "postTokenBalances": [{"accountIndex": 1, "uiTokenAmount": {"amount": str(before + delta)}}],
            },
        }

    async def simulate(self, raw_tx: bytes) -> None:
        self.simulated.append(raw_tx)

    async def send_and_confirm(self, raw_tx: bytes, timeout_s: float) -> str:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(raw_tx)
        return str(VersionedTransaction.from_bytes(raw_tx).signatures[0])


FIXTURES = Path(__file__).parents[1] / "fixtures" / "x402"
# Dev's hot wallet and its first cold address, from the real devnet transactions in tests/fixtures/x402.
DEV_HOT = Pubkey.from_string("7gQLXKARDkzNRFJy51WLy8T65kmcKAMUUn6eKhhUtZht")
DEV_FORMER_COLD = Pubkey.from_string("EE6XVrpkxrxDx6T9TRyFwrEB2D35kWJzHBgMn4jwzeJe")
DEVNET_USDC = usdc_mint("solana-devnet")


def devnet_tx(name: str, hot: Pubkey | None = None, replace: dict[str, str] | None = None) -> dict[str, Any]:
    """
    A real devnet transaction (``getTransaction``, ``jsonParsed``) of dev's hot wallet, from tests/fixtures/x402.

    ``sol_sweep_to_former_cold``: unwrap, recreate the wSOL account, 0.1469 SOL to EE6X… (signature 51h53MNU…).
    ``usdc_sweep_to_former_cold``: create EE6X's USDC account (idempotent), 5.01 USDC into it (3EPiHdDc…).
    Both from 15 Nov 2026, when EE6X… was dev's cold address. With ``hot``, dev's hot wallet and its token accounts
    are swapped for that wallet's (the tests hold its key, not dev's); ``replace`` swaps any other address.
    """
    text = (FIXTURES / f"{name}.json").read_text()
    swaps = dict(replace or {})
    if hot is not None:
        swaps |= {
            str(DEV_HOT): str(hot),
            str(associated_token_address(DEV_HOT)): str(associated_token_address(hot)),
            str(associated_token_address(DEV_HOT, DEVNET_USDC)): str(associated_token_address(hot, DEVNET_USDC)),
        }
    for old, new in swaps.items():
        text = text.replace(old, new)
    tx: dict[str, Any] = json.loads(text)
    return tx
