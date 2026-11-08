"""Visitor addresses as rate-limit keys: IPv6 folded to its /64 (one subscriber), salted and hashed, never stored."""

import hashlib
import ipaddress
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

_SALT = secrets.token_bytes(16)  # per process: keys can't be linked across restarts


def network_of(ip: str) -> str:
    """
    The address a limit should count: IPv4 as is, IPv6 as its /64, since one subscriber usually holds a whole /64.

    :param ip: The client address, as the server saw it.
    :return: The address or network, as text (unparseable input is returned unchanged).
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/64", strict=False))
    return str(address)


def visitor_key(ip: str) -> str:
    """
    A salted hash of the visitor's network, safe to keep in memory and to log.

    :param ip: The client address.
    :return: A short hex key.
    """
    return hashlib.blake2b(network_of(ip).encode(), key=_SALT, digest_size=12).hexdigest()


@dataclass
class RateLimit:
    """At most ``limit`` requests per visitor network in a sliding ``window_s``; in memory, keys hashed."""

    limit: int
    window_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def retry_after(self, ip: str) -> float | None:
        """
        Count a request, or say how long to wait.

        :param ip: The client address.
        :return: Seconds until another request is allowed, or ``None`` if this one is (and was counted).
        """
        now = self.clock()
        if len(self._hits) > 50_000:  # bound memory: drop idle visitors
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - self.window_s}
        hits = self._hits.setdefault(visitor_key(ip), deque())
        while hits and hits[0] <= now - self.window_s:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window_s - now
        hits.append(now)
        return None
