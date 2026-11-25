"""The public price list, the unsigned purchase transaction (SOL or USDC) and its Solana Pay link."""

from decimal import Decimal
from typing import Any
from urllib.parse import quote

from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from unfret.config import Settings
from unfret.credits import topup as topups
from unfret.credits import vat
from unfret.prices import LAMPORTS_PER_SOL, PriceBook, PriceUnavailableError, format_usd, usd_to_lamports
from unfret.x402.solana import MEMO_PROGRAM, associated_token_address, decimals, transfer_checked
from unfret.x402.wallet import create_ata_idempotent


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
    :return: ``unfret:credit:<pubkey>`` or ``unfret:credit:key:<id>``.
    """
    return f"unfret:credit:{beneficiary}"


def solana_pay_url(
    hot: Pubkey, amount: int, memo: str | None = None, mint: Pubkey | None = None, *, reference: str | None = None
) -> str:
    """
    A Solana Pay transfer request for a phone wallet: pay ``amount`` to the hot wallet, in SOL or a token.

    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units (lamports, or the token's base units with ``mint``).
    :param memo: The memo naming the purchase (``unfret:quote:<id>``, see :mod:`unfret.credits.vat`).
    :param mint: The token to pay in (``spl-token``); ``None`` for SOL.
    :param reference: The quote's Solana Pay reference; wallets add it to the transfer as a read-only account.
    :return: The ``solana:`` URL.
    """
    places = 9 if mint is None else decimals(mint)
    url = f"solana:{hot}?amount={decimal_string(amount, places)}"
    if mint is not None:
        url += f"&spl-token={mint}"
    if reference is not None:
        url += f"&reference={reference}"
    url += f"&label=Unfret&message={quote('Unfret checks')}"
    return f"{url}&memo={quote(memo)}" if memo else url


def purchase_transaction(
    payer: Pubkey,
    hot: Pubkey,
    amount: int,
    blockhash: str,
    memo: str,
    *,
    mint: Pubkey | None = None,
    reference: Pubkey | None = None,
) -> bytes:
    """
    Build the unsigned transfer the buyer's wallet signs and sends.

    SOL: a System transfer of ``amount`` lamports to the hot wallet. A token (USDC): create the hot wallet's token
    account if it's missing (the buyer pays the rent) and ``TransferChecked`` ``amount`` from the buyer's. Either way
    with a memo naming the purchase (its quote: who to credit, and the VAT), and the quote's Solana Pay reference as
    an extra read-only account on the transfer (ignored by the System and Token programs), since some wallets drop the
    memo instruction.

    :param payer: The paying wallet (also the fee payer).
    :param hot: The receiving (hot) wallet.
    :param amount: The amount in base units.
    :param blockhash: A recent blockhash.
    :param memo: The memo (``unfret:quote:<id>``).
    :param mint: The token to pay in; ``None`` for SOL.
    :param reference: The quote's reference, added to the transfer instruction's accounts.
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
    if reference is not None:
        moved = pay[-1]
        extra = AccountMeta(reference, is_signer=False, is_writable=False)
        pay[-1] = Instruction(moved.program_id, moved.data, [*moved.accounts, extra])
    note = Instruction(MEMO_PROGRAM, memo.encode(), [])
    message = MessageV0.try_compile(payer, [*pay, note], [], Hash.from_string(blockhash))
    return bytes(VersionedTransaction.populate(message, [Signature.default()]))


def topup_document(settings: Settings) -> dict[str, Any]:
    """
    How top-ups are priced, for ``/v1/pricing``'s ``topup``: the formula, the bonus tiers and a few examples.

    :param settings: The service settings.
    :return: ``min_amount``, ``price_per_check`` per currency, ``bonus`` tiers, ``vat`` and ``examples``.
    """
    examples = []
    for currency in topups.CARD_CURRENCIES:
        for amount in (settings.topup_min_amount, Decimal(50), Decimal(100)):
            top = topups.topup(settings, amount, currency)
            examples.append(
                {
                    "currency": currency,
                    "amount": format(top.amount, "f"),
                    "checks": top.checks,
                    "bonus_checks": top.bonus_checks,
                    "label": topups.label(top),
                }
            )
    return {
        "min_amount": format(settings.topup_min_amount, "f"),
        "price_per_check": {c: format(topups.price_per_check(settings, c), "f") for c in topups.CARD_CURRENCIES},
        "formula": "checks = floor(amount before VAT / price per check), plus the bonus of the highest tier reached",
        "bonus": [{"from_amount": format(start, "f"), "pct": pct} for start, pct in topups.bonus_tiers(settings)],
        "vat": "added at checkout: by the payment provider for cards, at the buyer's country's rate for crypto",
        "quote_url": "/v1/pricing/topup",
        "examples": examples,
    }


async def pricing(settings: Settings, hot: Pubkey | None, book: PriceBook, usdc: Pubkey) -> dict[str, Any]:
    """
    The public price list (``GET /v1/pricing``): the price of a check, how top-ups are priced, and live amounts.

    Every dollar figure derives from ``price_per_check_usd``; a top-up's checks from the amount the buyer names
    (:mod:`unfret.credits.topup`), VAT added at checkout. ``packs`` is gone (an empty list, for older clients).

    :param settings: The service settings.
    :param hot: The hot wallet, or ``None`` while purchases are off.
    :param book: The price book.
    :param usdc: The USDC mint.
    :return: The contract's pricing document.
    """
    check = book.usd("analysis")
    quick, deep = book.usd("x402_quick"), book.usd("x402_deep")
    document: dict[str, Any] = {
        "currency": "USD",
        "price_per_check_usd_micros": check,
        "price_per_check_usd": format_usd(check),
        "analysis_usd_micros": check,
        "analysis_usd": format_usd(check),
        "x402": {"quick_usd_micros": quick, "deep_usd_micros": deep},
        "topup": topup_document(settings),
        # Deprecated: fixed packs are gone (a top-up of any amount from topup.min_amount replaces them).
        "packs": [],
        "suggested_packs": [],
        "card_tax_inclusive": {"usd": False, "eur": False},
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
