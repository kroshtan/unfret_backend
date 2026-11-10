"""
The failed-settlement breaker: a bound on what failed payments can cost the hot wallet in fees.

The hot wallet pays the fee of every transaction it submits, landed or not. Verification makes a payment that
simulates fine and then fails on chain hard to build, but not impossible (the payer can race its own payment), so
failures are counted per paying wallet, per visitor network and in total over a rolling hour, and payments are
refused (503) while any count is at its limit. Kept in process memory; visitor addresses only as salted hashes.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from unfret.visitors import visitor_key

WINDOW_S = 3600.0


@dataclass
class SettlementBreaker:
    """Failed settlements per payer, per visitor and globally in a rolling window."""

    per_payer: int
    per_ip: int
    global_limit: int
    clock: Callable[[], float] = time.monotonic
    _payers: dict[str, deque[float]] = field(default_factory=dict)
    _ips: dict[str, deque[float]] = field(default_factory=dict)
    _all: deque[float] = field(default_factory=deque)

    def _trim(self, hits: deque[float], now: float) -> deque[float]:
        while hits and hits[0] <= now - WINDOW_S:
            hits.popleft()
        return hits

    def open_for(self, payer: str, ip: str) -> float | None:
        """
        Whether payments from this payer and visitor are refused right now.

        :param payer: The paying wallet (base58).
        :param ip: The visitor's address.
        :return: Seconds until the breaker closes again, or ``None`` if payments may go ahead.
        """
        now = self.clock()
        counts = (
            (self._trim(self._all, now), self.global_limit),
            (self._trim(self._payers.get(payer, deque()), now), self.per_payer),
            (self._trim(self._ips.get(visitor_key(ip), deque()), now), self.per_ip),
        )
        # Under the limit again once the oldest of the last ``limit`` failures leaves the window.
        waits = [hits[-limit] + WINDOW_S - now for hits, limit in counts if len(hits) >= limit]
        return max(waits) if waits else None

    def record_failure(self, payer: str, ip: str) -> None:
        """
        Count a settlement that cost a fee and brought nothing in.

        :param payer: The paying wallet.
        :param ip: The visitor's address.
        """
        now = self.clock()
        if len(self._payers) > 10_000:  # bound memory: drop the idle ones
            self._payers = {k: v for k, v in self._payers.items() if self._trim(v, now)}
            self._ips = {k: v for k, v in self._ips.items() if self._trim(v, now)}
        self._all.append(now)
        self._payers.setdefault(payer, deque()).append(now)
        self._ips.setdefault(visitor_key(ip), deque()).append(now)

    @property
    def global_failures(self) -> int:
        """
        Failed settlements in the last hour, all payers together.

        :return: The count.
        """
        return len(self._trim(self._all, self.clock()))
