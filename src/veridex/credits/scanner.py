"""
The credits scanner: credit wallets for SOL they send to the hot wallet.

It walks the hot wallet's finalized transactions oldest first from its cursor and, for each, credits incoming System
transfers (top-level and inner instructions, so smart wallets paying through a program count too) and moves the
cursor, in one database transaction. The cursor moves by compare-and-swap, so a transfer is credited exactly once
even if two scanners run or one restarts mid-way.

Who is credited: the beneficiary named by a ``veridex:credit:<pubkey>`` memo when the transaction carries exactly one
well-formed such memo (``/v1/credits/purchase-tx`` and the Solana Pay link add it), so a smart-wallet vault can buy
credits for the key that signs in; otherwise the transfer's source. Anyone may fund anyone, so a memo grants nothing
the payer couldn't give anyway.

Everything else touching the hot wallet is ignored: sweeps to cold and fee payments (the hot wallet is the source),
x402 settlements (wSOL into its token account, not a System transfer to it) and failed transactions. Any other SOL
sent to the hot wallet, a manual top-up included, counts as a purchase by its sender.
"""

import asyncio
from typing import Any

import asyncpg
import structlog
from solders.pubkey import Pubkey

from veridex.credits import ledger
from veridex.credits.pricing import with_bonus
from veridex.x402.solana import SolanaRpc

logger = structlog.get_logger("veridex")

_PAGE = 1000
_TRANSFERS = {"transfer", "transferWithSeed"}
MEMO_PREFIX = "veridex:credit:"
# How long the first start waits for the newest confirmed transaction to finalize (see initialise()).
_FINALITY_WAIT_S = 90


def _instructions(tx: dict[str, Any]) -> list[dict[str, Any]]:
    instructions = list(tx["transaction"]["message"]["instructions"])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(inner.get("instructions", []))
    return instructions


def credit_beneficiary(tx: dict[str, Any]) -> str | None:
    """
    The wallet a ``veridex:credit:<pubkey>`` memo names, if the transaction carries exactly one well-formed such memo.

    :param tx: The transaction in ``jsonParsed`` encoding.
    :return: The beneficiary (base58), or ``None`` (no memo, several, or a malformed one).
    """
    memos = [
        ix["parsed"]
        for ix in _instructions(tx)
        if ix.get("program") == "spl-memo"
        and isinstance(ix.get("parsed"), str)
        and ix["parsed"].startswith(MEMO_PREFIX)
    ]
    if len(memos) != 1:
        return None
    try:
        return str(Pubkey.from_string(memos[0].removeprefix(MEMO_PREFIX).strip()))
    except ValueError:
        return None


def incoming_transfers(tx: dict[str, Any], hot: str) -> dict[str, int]:
    """
    Sum the System transfers to ``hot`` from other accounts in a successful transaction, by sender.

    :param tx: The transaction in ``jsonParsed`` encoding.
    :param hot: The hot wallet (base58).
    :return: Lamports received per sending wallet (for ``transferWithSeed``, the base that signed).
    """
    if (tx.get("meta") or {}).get("err") is not None:
        return {}
    totals: dict[str, int] = {}
    for ix in _instructions(tx):
        parsed = ix.get("parsed")
        if ix.get("program") != "system" or not isinstance(parsed, dict) or parsed.get("type") not in _TRANSFERS:
            continue
        info = parsed.get("info", {})
        sender = info.get("sourceBase") or info.get("source")
        if info.get("destination") != hot or not sender or sender == hot or info.get("source") == hot:
            continue
        totals[sender] = totals.get(sender, 0) + int(info["lamports"])
    return totals


def credits_for(tx: dict[str, Any], hot: str) -> dict[str, int]:
    """
    Who gets credited for a transaction's incoming transfers: the memo's beneficiary, or else each sender.

    :param tx: The transaction in ``jsonParsed`` encoding.
    :param hot: The hot wallet (base58).
    :return: Lamports to credit per wallet (before any bonus).
    """
    received = incoming_transfers(tx, hot)
    beneficiary = credit_beneficiary(tx)
    if received and beneficiary and beneficiary != hot:
        return {beneficiary: sum(received.values())}
    return received


class CreditScanner:
    """Credits incoming SOL transfers to the hot wallet, exactly once each."""

    def __init__(self, rpc: SolanaRpc, pool: asyncpg.Pool, hot: Pubkey, bonus_tiers: list[tuple[int, int]]) -> None:
        """
        Scan one hot wallet.

        :param rpc: The Solana RPC client.
        :param pool: The database pool.
        :param hot: The hot wallet.
        :param bonus_tiers: ``(minimum lamports, bonus percent)`` pairs applied per purchase.
        """
        self.rpc, self.pool, self.hot, self.bonus_tiers = rpc, pool, hot, bonus_tiers
        self.name = f"credits:{hot}"
        self.ready = False

    async def initialise(self) -> None:
        """
        Make sure a cursor exists.

        The first run starts after everything already visible (or at the very beginning, for a wallet without any
        transactions): earlier transfers, like funding the hot wallet, are not credits. "Visible" means confirmed,
        and the start waits until that transaction is finalized (the scan only reads finalized ones), so funding
        sent seconds before the first start isn't credited either; if it never finalizes (a dropped fork), the newest
        finalized transaction is the start instead.
        """
        if await ledger.get_cursor(self.pool, self.name) is None:
            start = await self._start_signature()
            await self.pool.execute(
                "INSERT INTO scan_cursor (name, signature) VALUES ($1, $2) ON CONFLICT (name) DO NOTHING",
                self.name,
                start,
            )
            logger.info("credits_scanner_started", from_signature=start or "(beginning)")
        self.ready = True

    async def _start_signature(self) -> str:
        visible = await self.rpc.signature_page(self.hot, limit=1, commitment="confirmed")
        if not visible:
            return ""
        target = visible[0]["signature"]
        for _ in range(_FINALITY_WAIT_S):
            finalized = await self.rpc.signature_page(self.hot, limit=50, commitment="finalized")
            if any(entry["signature"] == target for entry in finalized):
                return str(target)
            await asyncio.sleep(1)
        newest = await self.rpc.signature_page(self.hot, limit=1, commitment="finalized")
        return str(newest[0]["signature"]) if newest else ""

    async def _pending(self, cursor: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        before: str | None = None
        while True:
            page = await self.rpc.signature_page(self.hot, before=before, until=cursor or None, limit=_PAGE)
            entries.extend(page)
            if len(page) < _PAGE:
                return list(reversed(entries))  # oldest first
            before = page[-1]["signature"]

    async def scan_once(self) -> int:
        """
        Process every finalized transaction since the cursor.

        :return: The number of credits made.
        """
        if not self.ready:
            await self.initialise()
        cursor = await ledger.get_cursor(self.pool, self.name)
        assert cursor is not None  # noqa: S101 - initialise() guarantees a row
        made = 0
        for entry in await self._pending(cursor):
            signature = entry["signature"]
            received: dict[str, int] = {}
            if entry.get("err") is None:
                tx = await self.rpc.parsed_transaction(signature, commitment="finalized")
                if tx is None:
                    break  # not retrievable yet; never skip past it, try again next round
                received = credits_for(tx, str(self.hot))
            async with self.pool.acquire() as conn, conn.transaction():
                current = await conn.fetchval(
                    "SELECT signature FROM scan_cursor WHERE name = $1 FOR UPDATE", self.name
                )
                if current != cursor:
                    logger.warning("credits_scanner_raced", expected=cursor, found=current)
                    return made  # another scanner moved on; it credits from here
                for sender, lamports in received.items():
                    await ledger.credit(conn, sender, with_bonus(lamports, self.bonus_tiers))
                await ledger.set_cursor(conn, self.name, signature)
            for lamports in received.values():
                # Amounts only: which wallet bought is on chain already and needn't be in our logs.
                logger.info("credits_purchased", lamports=lamports, credited=with_bonus(lamports, self.bonus_tiers))
                made += 1
            cursor = signature
        return made
