"""The public price list, the unsigned purchase transaction (SOL or USDC) and its Solana Pay link."""

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
from veridex.credits import vat
from veridex.credits.packs import Pack, packs
from veridex.prices import LAMPORTS_PER_SOL, PriceBook, PriceUnavailableError, format_usd, usd_to_lamports
from veridex.shopper import LANGUAGES, copy_for
from veridex.x402.solana import MEMO_PROGRAM, associated_token_address, decimals, transfer_checked
from veridex.x402.wallet import create_ata_idempotent


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
    The older memo that names who gets the credits, still honoured by the scanner for transfers made by hand.

    :param beneficiary: The wallet to credit, or an API key's account (``key:<id>``).
    :return: ``veridex:credit:<pubkey>`` or ``veridex:credit:key:<id>``.
    """
    return f"veridex:credit:{beneficiary}"


def solana_pay_url(hot: Pubkey, amount: int, memo: str | None = None, mint: Pubkey | None = None) -> str:
    """
    A Solana Pay transfer request for a phone wallet: pay ``amount`` to the hot wallet, in SOL or a token.

    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units (lamports, or the token's base units with ``mint``).
    :param memo: The memo naming the purchase (``veridex:quote:<id>``, see :mod:`veridex.credits.vat`).
    :param mint: The token to pay in (``spl-token``); ``None`` for SOL.
    :return: The ``solana:`` URL.
    """
    places = 9 if mint is None else decimals(mint)
    url = f"solana:{hot}?amount={decimal_string(amount, places)}"
    if mint is not None:
        url += f"&spl-token={mint}"
    url += f"&label=Unfret&message={quote('Unfret credits')}"
    return f"{url}&memo={quote(memo)}" if memo else url


def purchase_transaction(
    payer: Pubkey,
    hot: Pubkey,
    amount: int,
    blockhash: str,
    memo: str,
    *,
    mint: Pubkey | None = None,
) -> bytes:
    """
    Build the unsigned transfer the buyer's wallet signs and sends.

    SOL: a System transfer of ``amount`` lamports to the hot wallet. A token (USDC): create the hot wallet's token
    account if it's missing (the buyer pays the rent) and ``TransferChecked`` ``amount`` from the buyer's. Either way
    with a memo naming the purchase (its quote: who to credit, and the VAT).

    :param payer: The paying wallet (also the fee payer).
    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units.
    :param blockhash: A recent blockhash.
    :param memo: The memo (``veridex:quote:<id>``).
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
    note = Instruction(MEMO_PROGRAM, memo.encode(), [])
    message = MessageV0.try_compile(payer, [*pay, note], [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction.populate(message, [Signature.default()]))


def pack_labels(pack: Pack) -> dict[str, str]:
    """
    How a pack is offered, per language: the card price as *the* price, crypto as the lower price.

    Never "a surcharge for cards" or "fees on top": EU rules forbid surcharging consumer card payments, while a
    discount for paying another way is fine.

    :param pack: The pack.
    :return: Language → e.g. ``"25 checks — $6.00 · pay with crypto: $5.00 + VAT where applicable"``.
    """
    labels = {}
    for language in LANGUAGES:
        text = copy_for(language)["pricing"]
        price, crypto = format_usd(pack.card_usd_micros), format_usd(pack.value_usd_micros)
        if language == "nl":
            price, crypto = price.replace(".", ","), crypto.replace(".", ",")
        offer = text["pack"].format(checks=pack.checks, price=price)
        labels[language] = f"{offer} · {text['crypto'].format(crypto=crypto)}"
    return labels


async def pricing(settings: Settings, hot: Pubkey | None, book: PriceBook, usdc: Pubkey) -> dict[str, Any]:
    """
    The public price list (``GET /v1/pricing``): the price of a check, the packs on both routes, and live amounts.

    Every dollar figure derives from ``price_per_check_usd`` (:mod:`veridex.credits.packs`). Per pack, ``price_usd``
    is the price (by card, before the provider's sales tax/VAT) and ``crypto_usd`` the lower price when paying with
    SOL or USDC (before VAT where it applies: ``vat``).

    :param settings: The service settings.
    :param hot: The hot wallet, or ``None`` while purchases are off.
    :param book: The price book.
    :param usdc: The USDC mint.
    :return: The contract's pricing document.
    """
    check = book.usd("analysis")
    quick, deep = book.usd("x402_quick"), book.usd("x402_deep")
    on_sale = packs(settings)
    document: dict[str, Any] = {
        "currency": "USD",
        "price_per_check_usd_micros": check,
        "price_per_check_usd": format_usd(check),
        "analysis_usd_micros": check,
        "analysis_usd": format_usd(check),
        "x402": {"quick_usd_micros": quick, "deep_usd_micros": deep},
        "packs": [
            {
                "checks": p.checks,
                "price_usd_micros": p.card_usd_micros,
                "price_usd": format_usd(p.card_usd_micros),
                "crypto_usd_micros": p.value_usd_micros,
                "crypto_usd": format_usd(p.value_usd_micros),
                "credited_usd_micros": p.value_usd_micros,
                "label": pack_labels(p),
            }
            for p in on_sale
        ],
        # Deprecated: the packs' crypto prices under their old name.
        "suggested_packs": [{"checks": p.checks, "usd_micros": p.value_usd_micros} for p in on_sale],
        "vat": vat.describe(settings),
        "hot_wallet": str(hot) if hot else None,
        "network": settings.solana_network,
        # USDC base units are micro-dollars: USDC amounts equal the dollar prices.
        "usdc": {
            "mint": str(usdc),
            "decimals": 6,
            "analysis_units": check,
            "x402": {"quick_units": quick, "deep_units": deep},
        },
        "analysis_lamports": None,
    }
    try:
        reading = await book.sol_usd()
        per_check = usd_to_lamports(check, reading.usd_per_sol)
        document["sol"] = {
            "usd_per_sol": str(reading.usd_per_sol),
            "quoted_at": reading.publish_time,
            "source": "pyth",
            "analysis_lamports": per_check,
            "x402": {
                "quick_lamports": usd_to_lamports(quick, reading.usd_per_sol),
                "deep_lamports": usd_to_lamports(deep, reading.usd_per_sol),
            },
        }
        document["analysis_lamports"] = per_check  # backward compatible: the current SOL price of a check
    except PriceUnavailableError as exc:
        document["sol"] = None
        document["sol_unavailable"] = str(exc)
    return document


__all__ = ["LAMPORTS_PER_SOL", "credit_memo", "pricing", "purchase_transaction", "solana_pay_url"]
