"""Build real x402 payment transactions with solders for the tests."""

import base64
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

from veridex.config import Settings
from veridex.x402.payment import encode_header, requirements
from veridex.x402.solana import CAIP2, MEMO_PROGRAM, TOKEN_PROGRAM, WSOL_MINT, SolanaRpc, associated_token_address

NETWORK = CAIP2["solana-devnet"]
BLOCKHASH = str(Hash.default())


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
    signatures = [Signature.default()]
    signatures += [kp.sign_message(to_bytes_versioned(message)) if sign else Signature.default() for kp in signers]
    return VersionedTransaction.populate(message, signatures)


def payment_header(tx: VersionedTransaction, accepted: dict[str, Any]) -> str:
    return encode_header(
        {"x402Version": 2, "accepted": accepted, "payload": {"transaction": base64.b64encode(bytes(tx)).decode()}}
    )


def expected(hot: Pubkey, amount: int = 100_000) -> dict[str, Any]:
    """Requirements as the service issues them: the hot wallet is both payTo and fee payer."""
    return requirements(network=NETWORK, amount=amount, pay_to=hot, fee_payer=hot)


def configured(hot: Keypair, cold: Pubkey, **overrides: Any) -> Settings:
    return Settings(hot_wallet_key=SecretStr(str(hot)), cold_address=str(cold), **overrides)


class FakeRpc(SolanaRpc):
    def __init__(self, sol: int = 50_000_000, wsol: int | None = 0) -> None:
        super().__init__("http://rpc.invalid")
        self.sol, self.wsol = sol, wsol
        self.sent: list[bytes] = []
        self.simulated: list[bytes] = []
        self.fail_with: Exception | None = None
        self.signatures: list[str] = []
        self.transactions: dict[str, dict[str, Any]] = {}

    async def get_balance(self, address: Pubkey) -> int:
        return self.sol

    async def token_balance(self, account: Pubkey) -> int | None:
        return self.wsol

    async def rent_exempt_minimum(self, size: int) -> int:
        return 2_039_280

    async def latest_blockhash(self) -> str:
        return BLOCKHASH

    async def recent_signatures(self, address: Pubkey, limit: int = 20) -> list[str]:
        return self.signatures

    async def parsed_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.transactions.get(signature)

    async def simulate(self, raw_tx: bytes) -> None:
        self.simulated.append(raw_tx)

    async def send_and_confirm(self, raw_tx: bytes, timeout_s: float) -> str:
        if self.fail_with:
            raise self.fail_with
        self.sent.append(raw_tx)
        return str(VersionedTransaction.from_bytes(raw_tx).signatures[0])
