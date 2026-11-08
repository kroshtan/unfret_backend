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
from veridex.prices import LAMPORTS_PER_SOL, Currency, PriceBook, PriceUnavailableError
from veridex.x402.solana import MEMO_PROGRAM, associated_token_address, decimals, transfer_checked
from veridex.x402.wallet import create_ata_idempotent


def with_bonus(lamports: int, tiers: list[tuple[int, int]]) -> int:
    """
    The balance a purchase buys: its SOL value plus the best bonus tier that value reaches.

    :param lamports: The amount paid, or for USDC its converted SOL value.
    :param tiers: ``(minimum lamports, bonus percent)`` pairs.
    :return: The lamports credited.
    """
    pct = max((bonus for minimum, bonus in tiers if lamports >= minimum), default=0)
    return lamports + lamports * pct // 100


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


def credit_memo(beneficiary: Pubkey) -> str:
    """
    The memo that names who gets the credits (the scanner reads it).

    :param beneficiary: The wallet to credit.
    :return: ``veridex:credit:<pubkey>``.
    """
    return f"veridex:credit:{beneficiary}"


def solana_pay_url(hot: Pubkey, amount: int, beneficiary: Pubkey | None = None, mint: Pubkey | None = None) -> str:
    """
    A Solana Pay transfer request for a phone wallet: pay ``amount`` to the hot wallet, in SOL or a token.

    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units (lamports, or the token's base units with ``mint``).
    :param beneficiary: The wallet to credit, as a memo; without one the paying wallet is credited.
    :param mint: The token to pay in (``spl-token``); ``None`` for SOL.
    :return: The ``solana:`` URL.
    """
    places = 9 if mint is None else decimals(mint)
    url = f"solana:{hot}?amount={decimal_string(amount, places)}"
    if mint is not None:
        url += f"&spl-token={mint}"
    url += f"&label=Veridex&message={quote('Veridex credits')}"
    return f"{url}&memo={quote(credit_memo(beneficiary))}" if beneficiary else url


def purchase_transaction(  # noqa: PLR0913
    payer: Pubkey, hot: Pubkey, amount: int, blockhash: str, beneficiary: Pubkey, *, mint: Pubkey | None = None
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
    :param beneficiary: The wallet that gets the credits (often the payer itself).
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
    The smallest and largest purchase in ``currency`` (base units): the SOL limits, converted for USDC.

    USDC that can't be priced right now raises :class:`~veridex.prices.PriceUnavailableError`.

    :param settings: The service settings.
    :param book: The price book.
    :param currency: ``SOL`` or ``USDC``.
    :return: ``(minimum, maximum)``.
    """
    if currency == "SOL":
        return settings.purchase_min_lamports, settings.purchase_max_lamports
    per_check_usdc = await book.price("analysis", "USDC")
    per_check = book.lamports("analysis")
    return (
        -(-settings.purchase_min_lamports * per_check_usdc // per_check),  # rounded up: never below one check
        settings.purchase_max_lamports * per_check_usdc // per_check,
    )


async def pricing(settings: Settings, hot: Pubkey | None, book: PriceBook, usdc: Pubkey) -> dict[str, Any]:
    """
    The public price list (``GET /v1/pricing``): canonical SOL prices plus a live USDC quote.

    :param settings: The service settings.
    :param hot: The hot wallet, or ``None`` while purchases are off.
    :param book: The price book.
    :param usdc: The USDC mint.
    :return: The contract's pricing document.
    """
    price = book.lamports("analysis")
    document: dict[str, Any] = {
        "analysis_lamports": price,
        "x402": {"quick_lamports": book.lamports("x402_quick"), "deep_lamports": book.lamports("x402_deep")},
        "bonuses": [{"min_lamports": m, "bonus_pct": pct} for m, pct in sorted(settings.credit_bonus_tiers)],
        "suggested_packs": [{"checks": n, "lamports": n * price} for n in settings.suggested_packs],
        "purchase": {"min_lamports": settings.purchase_min_lamports, "max_lamports": settings.purchase_max_lamports},
        "hot_wallet": str(hot) if hot else None,
        "network": settings.solana_network,
        "trial_checks": settings.trial_checks,
        "price_mode": settings.price_mode,
    }
    try:
        reading = await book.sol_usd() if settings.price_mode == "sol" else None
        low, high = await purchase_limits(settings, book, "USDC")
        document["usdc"] = {
            "mint": str(usdc),
            "decimals": 6,
            "analysis_units": await book.price("analysis", "USDC"),
            "x402": {
                "quick_units": await book.price("x402_quick", "USDC"),
                "deep_units": await book.price("x402_deep", "USDC"),
            },
            "purchase": {"min_units": low, "max_units": high},
            "sol_usd": str(reading.usd_per_sol) if reading else None,
            "quoted_at": reading.publish_time if reading else None,
            "source": "pyth" if reading else "fixed",
        }
    except PriceUnavailableError as exc:
        document["usdc"] = None
        document["usdc_unavailable"] = str(exc)
    return document


__all__ = ["LAMPORTS_PER_SOL", "pricing", "purchase_limits", "purchase_transaction", "solana_pay_url", "with_bonus"]
