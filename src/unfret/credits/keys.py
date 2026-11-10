"""
API keys: an account without a wallet. Pay money, get a key; lose the key, use the recovery code.

A key is ``ufk_<id>_<secret>`` and its recovery code ``ufr_<id>_<secret>``: the id (10 base32 characters) is public
and names the account (``key:<id>`` in the ledger, and in purchase memos), the secrets are 256 random bits each. Both
are shown once, at creation or recovery, and only their SHA-256 hashes are stored (at 256 bits a plain hash is as
good as any slow hash: there is nothing to brute-force). Recovery rotates both secrets and keeps the id, so the
balance stays and the old key and code stop working at once.

Nothing else is stored about a key: no email, no IP, no purchase history. A new key starts empty, like a new
wallet, and is topped up like one: by a purchase whose memo names it, or through a fiat provider.
"""

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import asyncpg

from unfret.credits import ledger
from unfret.visitors import DailyLimit

type Executor = asyncpg.Pool | asyncpg.Connection

KEY_PREFIX = "ufk_"
RECOVERY_PREFIX = "ufr_"
ACCOUNT_PREFIX = "key:"
_ID_LEN = 10
_ID_RE = re.compile(r"^[a-z2-7]{10}$")
_SECRET_RE = re.compile(r"^[a-z2-7]{52}$")


class ApiKeyError(ValueError):
    """An API key or recovery code was malformed or unknown."""


def _b32(n: int) -> str:
    return base64.b32encode(secrets.token_bytes(n)).decode().rstrip("=").lower()


def _hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def account_of(key_id: str) -> str:
    """
    The ledger account of a key.

    :param key_id: The key's public id.
    :return: ``key:<id>``.
    """
    return ACCOUNT_PREFIX + key_id


def is_key_id(value: str) -> bool:
    """Whether ``value`` is a well-formed key id."""
    return bool(_ID_RE.fullmatch(value))


def is_key_account(account: str) -> bool:
    """Whether a ledger account is an API key's (``key:<id>``) rather than a wallet's."""
    return account.startswith(ACCOUNT_PREFIX) and is_key_id(account.removeprefix(ACCOUNT_PREFIX))


def parse(value: str, prefix: str = KEY_PREFIX) -> tuple[str, str]:
    """
    Split a key (or, with ``RECOVERY_PREFIX``, a recovery code) into its id and the full string to hash.

    :param value: What the caller sent.
    :param prefix: ``KEY_PREFIX`` or ``RECOVERY_PREFIX``.
    :return: ``(id, value)``.
    :raises ApiKeyError: If it isn't one.
    """
    value = value.strip()
    head, _, rest = value.partition("_")
    key_id, _, secret = rest.partition("_")
    if head + "_" != prefix or not _ID_RE.fullmatch(key_id) or not _SECRET_RE.fullmatch(secret):
        raise ApiKeyError("Not an API key." if prefix == KEY_PREFIX else "Not a recovery code.")
    return key_id, value


def key_id_from(value: str) -> str:
    """
    The id in a key id or a full key, so a top-up form can take either (the secret is never sent for a top-up).

    :param value: A key id or a key.
    :return: The id (:func:`parse` raises :class:`ApiKeyError` if it is neither).
    """
    value = value.strip()
    if _ID_RE.fullmatch(value):
        return value
    if value.startswith(ACCOUNT_PREFIX) and is_key_account(value):
        return value.removeprefix(ACCOUNT_PREFIX)
    return parse(value)[0]


@dataclass(frozen=True)
class IssuedKey:
    """A key and its recovery code, as handed to the caller exactly once."""

    key_id: str
    key: str
    recovery_code: str

    @property
    def account(self) -> str:
        """The ledger account."""
        return account_of(self.key_id)


def _secrets_for(key_id: str) -> tuple[str, str]:
    return f"{KEY_PREFIX}{key_id}_{_b32(32)}", f"{RECOVERY_PREFIX}{key_id}_{_b32(32)}"


async def create(db: Executor) -> IssuedKey:
    """
    Create an empty key (balance 0).

    :param db: A pool or connection.
    :return: The key and recovery code (not stored anywhere in plain).
    """
    while True:
        key_id = _b32(8)[:_ID_LEN]
        key, recovery = _secrets_for(key_id)
        inserted = await db.fetchval(
            "INSERT INTO api_keys (id, key_hash, recovery_hash) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO NOTHING RETURNING TRUE",
            key_id,
            _hash(key),
            _hash(recovery),
        )
        if inserted:
            return IssuedKey(key_id, key, recovery)


async def exists(db: Executor, key_id: str) -> bool:
    """
    Whether a key id is in use.

    :param db: A pool or connection.
    :param key_id: The id.
    :return: ``True`` if it is.
    """
    return bool(await db.fetchval("SELECT TRUE FROM api_keys WHERE id = $1", key_id))


async def authenticate(db: Executor, value: str) -> str:
    """
    Check an API key.

    :param db: A pool or connection.
    :param value: The key, as sent.
    :return: Its ledger account (``key:<id>``).
    :raises ApiKeyError: If it is malformed, unknown or was rotated away.
    """
    key_id, key = parse(value)
    stored = await db.fetchval("SELECT key_hash FROM api_keys WHERE id = $1", key_id)
    if stored is None or not hmac.compare_digest(bytes(stored), _hash(key)):
        raise ApiKeyError("Unknown or revoked API key.")
    return account_of(key_id)


async def recover(db: Executor, recovery_code: str) -> IssuedKey:
    """
    Rotate a key with its recovery code: a new key and a new code, the same id and balance; the old ones stop working.

    :param db: A pool or connection.
    :param recovery_code: The recovery code, as sent.
    :return: The new key and recovery code.
    :raises ApiKeyError: If the code is malformed or not (any longer) valid.
    """
    key_id, code = parse(recovery_code, RECOVERY_PREFIX)
    key, recovery = _secrets_for(key_id)
    # Compare-and-swap on the old hash: two recoveries racing with the same code can't both succeed.
    rotated = await db.fetchval(
        "UPDATE api_keys SET key_hash = $3, recovery_hash = $4, rotated_on = CURRENT_DATE "
        "WHERE id = $1 AND recovery_hash = $2 RETURNING TRUE",
        key_id,
        _hash(code),
        _hash(key),
        _hash(recovery),
    )
    if not rotated:
        raise ApiKeyError("Unknown or already used recovery code.")
    return IssuedKey(key_id, key, recovery)


async def balance(db: Executor, key_id: str) -> int:
    """
    A key's balance.

    :param db: A pool or connection.
    :param key_id: The id.
    :return: Micro-dollars (0 for a key never topped up).
    """
    return await ledger.balance(db, account_of(key_id)) or 0


# ── Creation guard: a small proof of work plus a per-network limit ───────────────────────────────────────────────


def _leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte:
            return bits + 8 - byte.bit_length()
        bits += 8
    return bits


def pow_ok(challenge: str, solution: str, bits: int) -> bool:
    """
    Whether ``SHA-256(challenge + ":" + solution)`` starts with ``bits`` zero bits.

    :param challenge: The challenge string.
    :param solution: The caller's answer (at most 32 characters).
    :param bits: The difficulty.
    :return: ``True`` if the work was done.
    """
    if not 0 < len(solution) <= 32:
        return False
    return _leading_zero_bits(hashlib.sha256(f"{challenge}:{solution}".encode()).digest()) >= bits


@dataclass
class KeyGuard:
    """
    What stands between a visitor and a new key: a proof of work, and per-network limits.

    Challenges are stateless (expiry + random + difficulty + HMAC) and each is accepted once. The limits count key
    creations and recovery attempts per visitor network. In process memory only; visitor addresses are salted hashes.
    """

    secret: str
    bits: int
    ttl_s: int
    creations: DailyLimit
    recoveries: DailyLimit
    clock: Callable[[], float] = time.time
    _used: dict[str, float] = field(default_factory=dict)

    def _now(self) -> float:
        return self.clock()

    def _mac(self, body: str) -> str:
        return hmac.new(self.secret.encode(), ("pow:" + body).encode(), hashlib.sha256).hexdigest()[:32]

    def challenge(self) -> tuple[str, int, float]:
        """
        Issue a challenge.

        :return: The challenge string, its difficulty in bits and its expiry.
        """
        expires = int(self._now() + self.ttl_s)
        body = f"{expires}.{secrets.token_hex(8)}.{self.bits}"
        return f"{body}.{self._mac(body)}", self.bits, float(expires)

    def redeem(self, challenge: str, solution: str) -> bool:
        """
        Accept a solved challenge once: authentic, unexpired, unused and solved at its own difficulty.

        :param challenge: The challenge as issued.
        :param solution: The caller's answer.
        :return: ``True`` if it is accepted (and now used up).
        """
        body, _, mac = challenge.rpartition(".")
        parts = body.split(".")
        if len(parts) != 3 or not hmac.compare_digest(mac, self._mac(body)):
            return False
        try:
            expires, bits = int(parts[0]), int(parts[2])
        except ValueError:
            return False
        now = self._now()
        if expires < now or challenge in self._used or not pow_ok(challenge, solution, bits):
            return False
        if len(self._used) > 10_000:
            self._used = {c: e for c, e in self._used.items() if e >= now}
        self._used[challenge] = expires
        return True
