"""Buying credits with USDC: the purchase transaction, Solana Pay link, limits, price list and transfer extraction."""

from typing import Any

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import StaticOracle
from veridex.config import Settings
from veridex.credits.pricing import pricing, purchase_transaction, solana_pay_url
from veridex.credits.scanner import credits_for, incoming_token_transfers
from veridex.prices import PriceBook
from veridex.x402.solana import ASSOCIATED_TOKEN_PROGRAM, TOKEN_PROGRAM, USDC_MINTS, associated_token_address

HOT = Keypair().pubkey()
BUYER = Keypair().pubkey()
USDC = USDC_MINTS["solana-devnet"]
HOT_ATA = str(associated_token_address(HOT, USDC))


MEMO = "veridex:quote:abcdefghijklmnopqrst"


def test_usdc_purchase_creates_the_account_if_needed_and_transfers_checked() -> None:
    raw = purchase_transaction(BUYER, HOT, 1_500_000, "11111111111111111111111111111111", MEMO, mint=USDC)
    message = VersionedTransaction.from_bytes(raw).message
    keys = message.account_keys
    programs = [keys[ix.program_id_index] for ix in message.instructions]
    assert keys[0] == BUYER  # the buyer pays the fee (and the account rent, if any)
    assert programs[:2] == [ASSOCIATED_TOKEN_PROGRAM, TOKEN_PROGRAM]
    transfer = message.instructions[1]
    accounts = [keys[i] for i in bytes(transfer.accounts)]
    assert accounts == [associated_token_address(BUYER, USDC), USDC, associated_token_address(HOT, USDC), BUYER]
    data = bytes(transfer.data)
    assert (data[0], int.from_bytes(data[1:9], "little"), data[9]) == (12, 1_500_000, 6)
    assert bytes(message.instructions[2].data) == MEMO.encode()


def test_solana_pay_link_names_the_token() -> None:
    url = solana_pay_url(HOT, 1_500_000, MEMO, USDC)
    assert url.startswith(f"solana:{HOT}?amount=1.5&spl-token={USDC}&label=Unfret")
    assert url.endswith("memo=veridex%3Aquote%3Aabcdefghijklmnopqrst")
    assert solana_pay_url(HOT, 15_000_000).startswith(f"solana:{HOT}?amount=0.015&label=")


async def test_price_list_shows_dollar_prices_exact_usdc_and_a_live_sol_quote() -> None:
    doc = await pricing(Settings(), HOT, PriceBook(Settings(), StaticOracle("100")), USDC)
    assert (doc["currency"], doc["price_per_check_usd_micros"], doc["price_per_check_usd"]) == (
        "USD",
        200_000,
        "$0.20",
    )
    assert (doc["analysis_usd_micros"], doc["analysis_usd"]) == (200_000, "$0.20")
    assert doc["x402"] == {"quick_usd_micros": 10_000, "deep_usd_micros": 200_000}
    assert "bonuses" not in doc
    first = doc["packs"][0]
    assert (first["checks"], first["price_usd"], first["crypto_usd"], first["credited_usd_micros"]) == (
        25,
        "$6.00",
        "$5.00",
        5_000_000,
    )
    assert first["label"]["en"] == "25 checks — $6.00 · pay with crypto: $5.00 + VAT where applicable"
    assert doc["suggested_packs"][0] == {"checks": 25, "usd_micros": 5_000_000}
    assert doc["vat"]["mode"] == "nl_under_threshold"
    assert doc["vat"]["eu_rates_pct"]["DE"] == "21"  # Dutch VAT for every EU buyer under the threshold
    usdc = doc["usdc"]
    assert (usdc["mint"], usdc["analysis_units"]) == (str(USDC), 200_000)
    assert usdc["x402"] == {"quick_units": 10_000, "deep_units": 200_000}
    sol = doc["sol"]
    assert (sol["usd_per_sol"], sol["source"], sol["analysis_lamports"]) == ("100", "pyth", 2_000_000)
    assert sol["x402"] == {"quick_lamports": 100_000, "deep_lamports": 2_000_000}
    assert doc["analysis_lamports"] == 2_000_000  # backward compatible: the current SOL price of a check

    stale = await pricing(Settings(), HOT, PriceBook(Settings(), StaticOracle(None)), USDC)
    assert stale["sol"] is None and "stale" in stale["sol_unavailable"]
    assert stale["usdc"]["analysis_units"] == 200_000  # USDC prices never depend on the oracle


def token_tx(*ixs: dict[str, Any], fee_payer: str | None = None, err: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"instructions": list(ixs)}
    if fee_payer:
        message["accountKeys"] = [{"pubkey": fee_payer}]
    return {"transaction": {"message": message}, "meta": {"err": err, "innerInstructions": []}}


def checked(amount: int, *, authority: str = str(BUYER), destination: str = HOT_ATA, mint: str = str(USDC)) -> dict:
    info = {"authority": authority, "destination": destination, "mint": mint, "tokenAmount": {"amount": str(amount)}}
    return {"program": "spl-token", "parsed": {"type": "transferChecked", "info": info}}


def test_incoming_usdc_is_attributed_to_the_signing_owner() -> None:
    plain = {
        "program": "spl-token",
        "parsed": {"type": "transfer", "info": {"authority": str(BUYER), "destination": HOT_ATA, "amount": "200000"}},
    }
    tx = token_tx(checked(1_500_000), plain)
    assert incoming_token_transfers(tx, str(HOT), str(USDC)) == {str(BUYER): 1_700_000}


def test_x402_settlements_swaps_other_mints_and_failures_are_not_purchases() -> None:
    hot = str(HOT)
    assert incoming_token_transfers(token_tx(checked(150_000), fee_payer=hot), hot, str(USDC)) == {}  # we paid the fee
    assert incoming_token_transfers(token_tx(checked(150_000, mint=str(Keypair().pubkey()))), hot, str(USDC)) == {}
    assert incoming_token_transfers(token_tx(checked(150_000, destination=str(HOT))), hot, str(USDC)) == {}
    assert incoming_token_transfers(token_tx(checked(150_000), err={"x": 1}), hot, str(USDC)) == {}
    assert incoming_token_transfers(token_tx(checked(150_000, authority=hot)), hot, str(USDC)) == {}


def test_a_usdc_purchase_memo_names_the_beneficiary() -> None:
    member = str(Keypair().pubkey())
    memo = {"program": "spl-memo", "parsed": f"veridex:credit:{member}"}
    vault = str(Keypair().pubkey())
    assert credits_for(token_tx(checked(1_500_000, authority=vault), memo), str(HOT), str(USDC)) == {member: 1_500_000}


def test_the_reference_rides_on_the_usdc_transfer_and_the_link() -> None:
    reference = Keypair().pubkey()
    raw = purchase_transaction(
        BUYER, HOT, 1_500_000, "11111111111111111111111111111111", MEMO, mint=USDC, reference=reference
    )
    message = VersionedTransaction.from_bytes(raw).message
    keys = message.account_keys
    transfer = message.instructions[1]
    accounts = [keys[i] for i in bytes(transfer.accounts)]
    # A trailing read-only, non-signing account: the Token program ignores it (as in Solana Pay).
    assert accounts == [
        associated_token_address(BUYER, USDC),
        USDC,
        associated_token_address(HOT, USDC),
        BUYER,
        reference,
    ]
    index = keys.index(reference)
    assert index >= len(keys) - message.header.num_readonly_unsigned_accounts  # read-only, not a signer
    assert bytes(message.instructions[2].data) == MEMO.encode()  # the memo stays, for wallets that keep it
    url = solana_pay_url(HOT, 1_500_000, MEMO, USDC, reference=str(reference))
    assert f"&spl-token={USDC}&reference={reference}&label=Unfret" in url and url.endswith(
        "memo=veridex%3Aquote%3Aabcdefghijklmnopqrst"
    )
