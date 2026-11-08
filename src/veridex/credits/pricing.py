"""Credit prices and bonuses, the unsigned purchase transaction and its Solana Pay link."""

from decimal import Decimal
from typing import Any
from urllib.parse import quote

from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from veridex.config import Settings

LAMPORTS_PER_SOL = 1_000_000_000


def with_bonus(lamports: int, tiers: list[tuple[int, int]]) -> int:
    """
    The balance a purchase buys: the amount paid plus the best bonus tier it reaches.

    :param lamports: The amount paid.
    :param tiers: ``(minimum lamports, bonus percent)`` pairs.
    :return: The lamports credited.
    """
    pct = max((bonus for minimum, bonus in tiers if lamports >= minimum), default=0)
    return lamports + lamports * pct // 100


def sol_string(lamports: int) -> str:
    """
    Format lamports as a plain decimal SOL amount (no exponent), as Solana Pay expects.

    :param lamports: The amount.
    :return: E.g. ``"0.015"``.
    """
    value = (Decimal(lamports) / LAMPORTS_PER_SOL).normalize()
    return format(value, "f")


def solana_pay_url(hot: Pubkey, lamports: int) -> str:
    """
    A Solana Pay transfer request for a phone wallet: pay ``lamports`` to the hot wallet.

    :param hot: The receiving (hot) wallet.
    :param lamports: The amount.
    :return: The ``solana:`` URL.
    """
    return f"solana:{hot}?amount={sol_string(lamports)}&label=Veridex&message={quote('Veridex credits')}"


def purchase_transaction(payer: Pubkey, hot: Pubkey, lamports: int, blockhash: str) -> bytes:
    """
    Build the unsigned transfer the buyer's wallet signs and sends: ``lamports`` from ``payer`` to the hot wallet.

    :param payer: The buying wallet (also the fee payer).
    :param hot: The receiving (hot) wallet.
    :param lamports: The amount.
    :param blockhash: A recent blockhash.
    :return: The serialised, unsigned versioned transaction.
    """
    ix = transfer(TransferParams(from_pubkey=payer, to_pubkey=hot, lamports=lamports))
    message = MessageV0.try_compile(payer, [ix], [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction.populate(message, [Signature.default()]))


def pricing(settings: Settings, hot: Pubkey | None) -> dict[str, Any]:
    """
    The public price list (``GET /v1/pricing``).

    :param settings: The service settings.
    :param hot: The hot wallet, or ``None`` while purchases are off.
    :return: The contract's pricing document.
    """
    price = settings.analysis_price_lamports
    return {
        "analysis_lamports": price,
        "x402": {
            "quick_lamports": settings.x402_price_quick_lamports,
            "deep_lamports": settings.x402_price_deep_lamports,
        },
        "bonuses": [{"min_lamports": m, "bonus_pct": pct} for m, pct in sorted(settings.credit_bonus_tiers)],
        "suggested_packs": [{"checks": n, "lamports": n * price} for n in settings.suggested_packs],
        "hot_wallet": str(hot) if hot else None,
        "network": settings.solana_network,
        "trial_checks": settings.trial_checks,
    }
