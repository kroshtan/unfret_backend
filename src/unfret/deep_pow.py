"""
A proof of work for a free deep check: the only thing that stands between an anonymous client and an LLM call.

Free deep checks need no pass and carry no identity, so nothing can be counted per person or device. Instead each
one costs the client a little work: ``GET /v1/analyze/challenge`` issues a stateless, signed challenge (expiry, a
random nonce, the difficulty, an HMAC with the session secret); the client finds a ``solution`` with
``SHA-256(challenge + ":" + solution)`` starting with ``bits`` zero bits (the same puzzle as a new pass's,
:func:`unfret.credits.keys.pow_ok`) and sends both with the check. Each challenge is accepted once, within
``deep_pow_ttl_s``, and only at the difficulty it was issued with (it is part of what the HMAC covers), so it can't be
lowered. ``deep_pow_bits`` sets the cost: every extra bit doubles the work.

The challenge names nothing: no client, network, page or time of day beyond its expiry. What is remembered (the
challenges already used, until they expire) is in this process's memory only.
"""

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from unfret.credits.keys import pow_ok

_MAX_USED = 50_000


@dataclass
class DeepChallenges:
    """Issues and redeems the challenges of free deep checks."""

    secret: str
    bits: int
    ttl_s: int
    clock: Callable[[], float] = time.time
    _used: dict[str, float] = field(default_factory=dict)

    def _mac(self, body: str) -> str:
        return hmac.new(self.secret.encode(), ("deep-pow:" + body).encode(), hashlib.sha256).hexdigest()[:32]

    def issue(self) -> tuple[str, int, float]:
        """
        A new challenge.

        :return: The challenge string, its difficulty in bits and its expiry (Unix seconds).
        """
        expires = int(self.clock() + self.ttl_s)
        body = f"{expires}.{secrets.token_hex(8)}.{self.bits}"
        return f"{body}.{self._mac(body)}", self.bits, float(expires)

    def redeem(self, challenge: str, solution: str) -> bool:
        """
        Accept a solved challenge once: authentic, unexpired, unused, at least today's difficulty, and solved.

        :param challenge: The challenge as issued.
        :param solution: The client's answer (at most 32 characters).
        :return: ``True`` if it is accepted (and now used up).
        """
        body, _, mac = challenge.rpartition(".")
        parts = body.split(".")
        if len(parts) != 3 or not self.secret or not hmac.compare_digest(mac, self._mac(body)):
            return False
        try:
            expires, bits = int(parts[0]), int(parts[2])
        except ValueError:
            return False
        now = self.clock()
        if bits < self.bits or expires < now or challenge in self._used or not pow_ok(challenge, solution, bits):
            return False
        if len(self._used) >= _MAX_USED:
            self._used = {c: e for c, e in self._used.items() if e >= now}
        self._used[challenge] = expires
        return True
