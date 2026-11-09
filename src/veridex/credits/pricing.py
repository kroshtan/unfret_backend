"""Credit prices and bonuses, the unsigned purchase transaction (SOL or USDC) and its Solana Pay link."""

from decimal import Decimal
from typing import Any
from urllib.parse import quote

from solders.hash import Hash
from solders.instruction import Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from veridex.config import Settings
from veridex.prices import (
    LAMPORTS_PER_SOL,
    Currency,
    PriceBook,
    PriceUnavailableError,
    format_usd,
    usd_to_lamports,
    usdc_to_lamports,
)
from veridex.x402.solana import MEMO_PROGRAM, associated_token_address, decimals, transfer_checked
from veridex.x402.wallet import create_ata_idempotent


def with_bonus(value: int, tiers: list[tuple[int, int]]) -> int:
    """
    The balance a purchase buys: its dollar value plus the best bonus tier that value reaches.

    :param value: The purchase's value in micro-dollars (SOL converted at crediting time).
    :param tiers: ``(minimum micro-dollars, bonus percent)`` pairs.
    :return: The micro-dollars credited.
    """
    pct = max((bonus for minimum, bonus in tiers if value >= minimum), default=0)
    return value + value * pct // 100


def decimal_string(amount: int, places: int) -> str:
    """
    Format base units as a plain decimal amount (no exponent), as Solana Pay expects.

    :param amount: Base units.
    :param places: The mint's decimals.
    :return: E.g. ``"0.015"``.
    """
    return format((Decimal(amount) / Decimal(10) ** places).normalize(), "f")


def sol_string(lamports: int) -> str:
    """
    Format lamports as a plain decimal SOL amount.

    :param lamports: The amount.
    :return: E.g. ``"0.015"``.
    """
    return decimal_string(lamports, 9)


def credit_memo(beneficiary: Pubkey | str) -> str:
    """
    The memo that names who gets the credits (the scanner reads it).

    :param beneficiary: The wallet to credit, or an API key's account (``key:<id>``).
    :return: ``veridex:credit:<pubkey>`` or ``veridex:credit:key:<id>``.
    """
    return f"veridex:credit:{beneficiary}"


def solana_pay_url(
    hot: Pubkey, amount: int, beneficiary: Pubkey | str | None = None, mint: Pubkey | None = None
) -> str:
    """
    A Solana Pay transfer request for a phone wallet: pay ``amount`` to the hot wallet, in SOL or a token.

    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units (lamports, or the token's base units with ``mint``).
    :param beneficiary: The account to credit (a wallet or ``key:<id>``), as a memo; without one the paying wallet
        is credited.
    :param mint: The token to pay in (``spl-token``); ``None`` for SOL.
    :return: The ``solana:`` URL.
    """
    places = 9 if mint is None else decimals(mint)
    url = f"solana:{hot}?amount={decimal_string(amount, places)}"
    if mint is not None:
        url += f"&spl-token={mint}"
    url += f"&label=Unfret&message={quote('Unfret credits')}"
    return f"{url}&memo={quote(credit_memo(beneficiary))}" if beneficiary else url


def purchase_transaction(
    payer: Pubkey,
    hot: Pubkey,
    amount: int,
    blockhash: str,
    beneficiary: Pubkey | str,
    *,
    mint: Pubkey | None = None,
) -> bytes:
    """
    Build the unsigned transfer the buyer's wallet signs and sends.

    SOL: a System transfer of ``amount`` lamports to the hot wallet. A token (USDC): create the hot wallet's token
    account if it's missing (the buyer pays the rent) and ``TransferChecked`` ``amount`` from the buyer's. Either way
    with a memo naming the wallet to credit.

    :param payer: The paying wallet (also the fee payer).
    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units.
    :param blockhash: A recent blockhash.
    :param beneficiary: The account that gets the credits (often the payer itself, or an API key's ``key:<id>``).
    :param mint: The token to pay in; ``None`` for SOL.
    :return: The serialised, unsigned versioned transaction.
    """
    if mint is None:
        pay = [transfer(TransferParams(from_pubkey=payer, to_pubkey=hot, lamports=amount))]
    else:
        pay = [
            create_ata_idempotent(payer, hot, mint),
            transfer_checked(
                associated_token_address(payer, mint), mint, associated_token_address(hot, mint), payer, amount
            ),
        ]
    memo = Instruction(MEMO_PROGRAM, credit_memo(beneficiary).encode(), [])
    message = MessageV0.try_compile(payer, [*pay, memo], [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction.populate(message, [Signature.default()]))


async def purchase_limits(settings: Settings, book: PriceBook, currency: Currency) -> tuple[int, int]:
    """
    The smallest and largest purchase in ``currency`` (base units): the dollar limits, converted for SOL.

    SOL that can't be priced right now raises :class:`~veridex.prices.PriceUnavailableError`.

    :param settings: The service settings.
    :param book: The price book.
    :param currency: ``SOL`` or ``USDC``.
    :return: ``(minimum, maximum)``.
    """
    if currency == "USDC":
        return settings.purchase_min_usd_micros, settings.purchase_max_usd_micros
    reading = await book.sol_usd()
    low = usd_to_lamports(settings.purchase_min_usd_micros, reading.usd_per_sol)  # rounded up: at least one check
    high = usdc_to_lamports(settings.purchase_max_usd_micros, reading.usd_per_sol)  # rounded down: at most the max
    return low, high


async def pricing(settings: Settings, hot: Pubkey | None, book: PriceBook, usdc: Pubkey) -> dict[str, Any]:
    """
    The public price list (``GET /v1/pricing``): canonical dollar prices, exact USDC amounts and a live SOL quote.

    :param settings: The service settings.
    :param hot: The hot wallet, or ``None`` while purchases are off.
    :param book: The price book.
    :param usdc: The USDC mint.
    :return: The contract's pricing document.
    """
    analysis = book.usd("analysis")
    quick, deep = book.usd("x402_quick"), book.usd("x402_deep")
    document: dict[str, Any] = {
        "currency": "USD",
        "analysis_usd_micros": analysis,
        "analysis_usd": format_usd(analysis),
        "x402": {"quick_usd_micros": quick, "deep_usd_micros": deep},
        "bonuses": [{"min_usd_micros": m, "bonus_pct": pct} for m, pct in sorted(settings.credit_bonus_tiers)],
        "suggested_packs": [{"checks": n, "usd_micros": n * analysis} for n in settings.suggested_packs],
        "purchase": {
            "min_usd_micros": settings.purchase_min_usd_micros,
            "max_usd_micros": settings.purchase_max_usd_micros,
        },
        "hot_wallet": str(hot) if hot else None,
        "network": settings.solana_network,
        "trial_checks": settings.trial_checks,
        # USDC base units are micro-dollars: USDC amounts equal the dollar prices.
        "usdc": {
            "mint": str(usdc),
            "decimals": 6,
            "analysis_units": analysis,
            "x402": {"quick_units": quick, "deep_units": deep},
            "purchase": {"min_units": settings.purchase_min_usd_micros, "max_units": settings.purchase_max_usd_micros},
        },
        "analysis_lamports": None,
    }
    try:
        reading = await book.sol_usd()
        low, high = await purchase_limits(settings, book, "SOL")
        per_check = usd_to_lamports(analysis, reading.usd_per_sol)
        document["sol"] = {
            "usd_per_sol": str(reading.usd_per_sol),
            "quoted_at": reading.publish_time,
            "source": "pyth",
            "analysis_lamports": per_check,
            "x402": {
                "quick_lamports": usd_to_lamports(quick, reading.usd_per_sol),
                "deep_lamports": usd_to_lamports(deep, reading.usd_per_sol),
            },
            "purchase": {"min_lamports": low, "max_lamports": high},
        }
        document["analysis_lamports"] = per_check  # backward compatible: the current SOL price of a check
    except PriceUnavailableError as exc:
        document["sol"] = None
        document["sol_unavailable"] = str(exc)
    return document


__all__ = ["LAMPORTS_PER_SOL", "pricing", "purchase_limits", "purchase_transaction", "solana_pay_url", "with_bonus"]
