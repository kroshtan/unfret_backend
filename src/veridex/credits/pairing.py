"""
Sign in with a phone wallet: pair a desktop extension with a sign-in made in the phone wallet's in-app browser.

The extension asks for a pairing and shows its connect URL as a QR code plus a short confirmation code. The phone
opens the URL in its wallet's browser, signs the ordinary SIWS message (whose statement names the confirmation code),
and the server attaches the resulting session to the pairing. The extension, polling with the pairing id, receives
the session exactly once. No relay, no on-chain transaction, no third party.

Everything is in process memory: pairings live five minutes, are bounded in number, and are keyed by a hash of their
id so a lookup's timing reveals nothing about other ids. Creation is rate-limited per visitor address, kept as salted
hashes like the trial limiter. Nothing is written anywhere.
"""

import hashlib
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

# Unambiguous characters for a code people compare by eye: no 0/O, 1/I/L, 5/S, 2/Z, 8/B.
_CODE_ALPHABET = "34679ACDEFGHJKMNPQRTUVWXY"
_GRACE_S = 600  # expired pairings answer "expired" (not "unknown") for this long after their expiry
_WINDOW_S = 600


class PairState(Enum):
    """What a poll finds."""

    PENDING = "pending"
    READY = "ready"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class PairingError(ValueError):
    """A pairing can't be used for this sign-in."""

    def __init__(self, kind: str, message: str) -> None:
        """
        Describe the failure.

        :param kind: ``unknown``, ``expired``, ``bound`` (already signed in) or ``mismatch`` (wrong code).
        :param message: A human-readable reason.
        """
        super().__init__(message)
        self.kind = kind


@dataclass
class Pairing:
    """One pending pairing."""

    code: str
    expires: float
    token: str | None = None
    address: str | None = None
    session_expires: float | None = None


def new_code() -> str:
    """
    A short confirmation code such as ``K7-4Q``.

    :return: Two pairs of unambiguous characters joined by a dash.
    """
    chars = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    return f"{chars[:2]}-{chars[2:]}"


def pairing_statement(statement: str, code: str) -> str:
    """
    The sign-in statement for a pairing: the usual statement plus the device's confirmation code.

    :param statement: The ordinary sign-in statement.
    :param code: The pairing's confirmation code.
    :return: The statement the phone signs.
    """
    return f"{statement} For the device showing {code}."


def _key(pair_id: str) -> str:
    return hashlib.sha256(pair_id.encode()).hexdigest()


@dataclass
class PairingStore:
    """Pending pairings, in memory, with a per-visitor creation limit."""

    ttl_s: int = 300
    max_pairings: int = 10_000
    per_ip: int = 10  # creations per visitor address per 10 minutes
    clock: Callable[[], float] = time.time
    _pairings: dict[str, Pairing] = field(default_factory=dict)
    _salt: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def _sweep(self, now: float) -> None:
        self._pairings = {k: p for k, p in self._pairings.items() if p.expires + _GRACE_S > now}
        self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > now - _WINDOW_S}

    def _allow(self, ip: str, now: float) -> bool:
        key = hashlib.blake2b(ip.encode(), key=self._salt, digest_size=16).hexdigest()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - _WINDOW_S:
            hits.popleft()
        if len(hits) >= self.per_ip:
            return False
        hits.append(now)
        return True

    def create(self, ip: str) -> tuple[str, Pairing] | None:
        """
        Start a pairing.

        :param ip: The requesting visitor's address (for the rate limit; only a salted hash is kept).
        :return: The pairing id (128 random bits, URL-safe) and the pairing, or ``None`` when rate-limited or full.
        """
        now = self.clock()
        self._sweep(now)
        if len(self._pairings) >= self.max_pairings or not self._allow(ip, now):
            return None
        pair_id = secrets.token_urlsafe(16)
        pairing = Pairing(code=new_code(), expires=now + self.ttl_s)
        self._pairings[_key(pair_id)] = pairing
        return pair_id, pairing

    def info(self, pair_id: str) -> Pairing:
        """
        Look a pairing up for the phone page (its code and expiry).

        :param pair_id: The pairing id from the connect URL.
        :return: The pairing.
        :raises PairingError: If it's unknown, expired or already signed in.
        """
        pairing = self._pairings.get(_key(pair_id))
        if pairing is None:
            raise PairingError("unknown", "Unknown pairing.")
        if pairing.expires <= self.clock():
            raise PairingError("expired", "This pairing expired; start again on your computer.")
        if pairing.token is not None:
            raise PairingError("bound", "This pairing is already signed in.")
        return pairing

    def bind(self, pair_id: str, code: str, token: str, address: str, session_expires: float) -> None:
        """
        Attach a verified session to a pairing, once.

        :param pair_id: The pairing id.
        :param code: The confirmation code named in the signed statement.
        :param token: The session token.
        :param address: The signed-in wallet.
        :param session_expires: The session's expiry timestamp.
        :raises PairingError: If the pairing is unusable or the code doesn't match.
        """
        pairing = self.info(pair_id)
        if not secrets.compare_digest(pairing.code, code):
            raise PairingError("mismatch", "The confirmation code doesn't match this pairing.")
        pairing.token, pairing.address, pairing.session_expires = token, address, session_expires

    def poll(self, pair_id: str) -> tuple[PairState, Pairing | None]:
        """
        What the extension finds when it polls. A ready session is handed over once, then the pairing is gone.

        :param pair_id: The pairing id.
        :return: The state, and the pairing when it's ready.
        """
        now = self.clock()
        key = _key(pair_id)
        pairing = self._pairings.get(key)
        if pairing is None:
            return PairState.UNKNOWN, None
        if pairing.token is not None:
            del self._pairings[key]
            return PairState.READY, pairing
        if pairing.expires <= now:
            return PairState.EXPIRED, None
        return PairState.PENDING, None
