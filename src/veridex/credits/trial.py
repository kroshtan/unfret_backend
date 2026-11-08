"""
The free trial: only for wallets with real on-chain history, a few per visitor address per day, and a daily cap.

Fresh keypairs cost nothing, so the history check does the real work; the per-IP limit and the global cap bound what
farming with aged wallets can cost. Both limits live in process memory only: visitor addresses are kept as salted
hashes (the salt is random per process) and nothing is written anywhere.
"""

import hashlib
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from solders.pubkey import Pubkey

from veridex.visitors import network_of
from veridex.x402.solana import SolanaRpc

DAY = 24 * 3600


async def has_history(rpc: SolanaRpc, address: str, *, min_age_days: int, min_signatures: int, now: float) -> bool:
    """
    Whether a wallet looks used: at least ``min_signatures`` transactions, or a first one ``min_age_days`` ago.

    :param rpc: The Solana RPC client.
    :param address: The wallet (base58).
    :param min_age_days: Minimum age of the oldest transaction.
    :param min_signatures: Minimum number of transactions.
    :param now: Current time.
    :return: ``True`` if the wallet qualifies.
    """
    page = await rpc.signature_page(Pubkey.from_string(address), limit=1000, commitment="finalized")
    if len(page) >= min_signatures:
        return True
    oldest = next((e.get("blockTime") for e in reversed(page) if e.get("blockTime")), None)
    return oldest is not None and now - float(oldest) >= min_age_days * DAY


@dataclass
class TrialLimiter:
    """Trials per visitor address (sliding 24 hours) and in total per UTC day."""

    per_ip_per_day: int
    daily_cap: int
    clock: Callable[[], float] = time.time
    _salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _day: str = ""
    _day_count: int = 0

    def _key(self, ip: str) -> str:
        return hashlib.blake2b(network_of(ip).encode(), key=self._salt, digest_size=16).hexdigest()

    def allow(self, ip: str) -> bool:
        """
        Decide whether ``ip`` may receive a trial now, and count it if so.

        :param ip: The visitor's address.
        :return: ``True`` if a trial may be granted.
        """
        now = self.clock()
        today = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._day_count = today, 0
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - DAY}
        hits = self._hits.setdefault(self._key(ip), deque())
        while hits and hits[0] <= now - DAY:
            hits.popleft()
        if self._day_count >= self.daily_cap or len(hits) >= self.per_ip_per_day:
            return False
        hits.append(now)
        self._day_count += 1
        return True
