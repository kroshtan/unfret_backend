"""
Sign-In With Solana, basic: stateless nonces, the sign-in message, signature checks and session tokens.

Nothing here touches the database. A nonce carries its own expiry and an HMAC, so the server needn't remember it; a
replayed sign-in within those five minutes only yields another session for the same wallet, which the signature
already proves control of. Sessions are HMAC tokens over the address and an expiry.

The message is the Solana sign-in text that wallets produce for the Wallet Standard ``solana:signIn`` feature
(EIP-4361 adapted to Solana), so ``signIn`` and the ``signMessage`` fallback sign the same bytes.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from solders.pubkey import Pubkey
from solders.signature import Signature

STATEMENT = "Sign in to Veridex. This signature costs nothing and grants no spending rights."
_HEADER = " wants you to sign in with your Solana account:"
_EXP_DIGITS = 10
_RANDOM_HEX = 16
_MAC_HEX = 32
_MAX_MESSAGE_BYTES = 2048


class SignInError(ValueError):
    """A sign-in message, nonce, signature or session token was not acceptable."""


def _mac(secret: str, data: str) -> str:
    return hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def issue_nonce(secret: str, ttl_s: int, now: float | None = None) -> tuple[str, float]:
    """
    Make a stateless nonce: expiry (10 digits) + random hex + HMAC, alphanumeric as SIWS requires.

    :param secret: The session secret.
    :param ttl_s: Seconds the nonce stays valid.
    :param now: Current time (injectable for tests).
    :return: The nonce and its expiry timestamp.
    """
    expires = int((time.time() if now is None else now) + ttl_s)
    body = f"{expires:0{_EXP_DIGITS}d}{secrets.token_hex(_RANDOM_HEX // 2)}"
    return body + _mac(secret, "nonce:" + body)[:_MAC_HEX], float(expires)


def check_nonce(secret: str, nonce: str, now: float | None = None) -> None:
    """
    Verify a nonce's HMAC and expiry.

    :param secret: The session secret.
    :param nonce: The nonce from the signed message.
    :param now: Current time (injectable for tests).
    :raises SignInError: If it was not issued by this server or has expired.
    """
    split = _EXP_DIGITS + _RANDOM_HEX
    body, mac = nonce[:split], nonce[split:]
    if len(nonce) != split + _MAC_HEX or not hmac.compare_digest(mac, _mac(secret, "nonce:" + body)[:_MAC_HEX]):
        raise SignInError("Unknown nonce.")
    if int(body[:_EXP_DIGITS]) < (time.time() if now is None else now):
        raise SignInError("The sign-in request expired; start again.")


@dataclass(frozen=True)
class SignInFields:
    """The fields of a sign-in message."""

    domain: str
    address: str
    statement: str
    uri: str
    version: str
    chain_id: str
    nonce: str
    issued_at: str
    expiration_time: str


def message_text(fields: SignInFields) -> str:
    """
    Render the canonical sign-in text (the Wallet Standard ``createSignInMessageText`` layout).

    :param fields: The message fields.
    :return: The text to sign.
    """
    lines = [f"{fields.domain}{_HEADER}", fields.address, "", fields.statement, ""]
    lines += [
        f"URI: {fields.uri}",
        f"Version: {fields.version}",
        f"Chain ID: {fields.chain_id}",
        f"Nonce: {fields.nonce}",
        f"Issued At: {fields.issued_at}",
        f"Expiration Time: {fields.expiration_time}",
    ]
    return "\n".join(lines)


def parse_message(text: str) -> dict[str, str]:
    """
    Read the fields of a sign-in text, tolerating the optional ones a wallet may leave out or add.

    :param text: The signed text.
    :return: ``domain``, ``address`` and every ``Key: value`` line, keyed in lower snake case.
    :raises SignInError: If the text isn't a Solana sign-in message.
    """
    lines = text.split("\n")
    if len(lines) < 2 or not lines[0].endswith(_HEADER):  # noqa: PLR2004
        raise SignInError("Not a sign-in message.")
    fields = {"domain": lines[0][: -len(_HEADER)], "address": lines[1].strip()}
    for line in lines[2:]:
        key, sep, value = line.partition(": ")
        if sep and key.replace(" ", "").isalpha():
            fields[key.strip().lower().replace(" ", "_")] = value.strip()
    return fields


def verify_sign_in(
    secret: str, *, domain: str, address: str, signed_message: bytes, signature: bytes, now: float | None = None
) -> str:
    """
    Check a signed sign-in message and return the wallet address it proves control of.

    :param secret: The session secret (for the nonce).
    :param domain: The domain sign-ins must be bound to.
    :param address: The address the client claims (base58).
    :param signed_message: The exact bytes the wallet signed.
    :param signature: The 64-byte ed25519 signature.
    :param now: Current time (injectable for tests).
    :return: The verified address (base58, canonical).
    :raises SignInError: On any mismatch: domain, address, nonce, times or signature.
    """
    if len(signed_message) > _MAX_MESSAGE_BYTES:
        raise SignInError("Sign-in message too long.")
    try:
        pubkey = Pubkey.from_string(address)
        sig = Signature.from_bytes(signature)
        text = signed_message.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise SignInError("Malformed address, signature or message.") from exc
    if not sig.verify(pubkey, signed_message):
        raise SignInError("The signature does not match the wallet.")
    fields = parse_message(text)
    if fields["domain"] != domain:
        raise SignInError("The message is for another site.")
    if fields["address"] != str(pubkey):
        raise SignInError("The message is for another wallet.")
    if "nonce" not in fields:
        raise SignInError("The message has no nonce.")
    check_nonce(secret, fields["nonce"], now)
    current = time.time() if now is None else now
    try:
        expires = _parse_iso(fields["expiration_time"]) if "expiration_time" in fields else None
        not_before = _parse_iso(fields["not_before"]) if "not_before" in fields else None
    except ValueError as exc:
        raise SignInError("Malformed time in the sign-in message.") from exc
    if expires is not None and expires < current:
        raise SignInError("The sign-in message expired.")
    if not_before is not None and not_before > current:
        raise SignInError("The sign-in message is not valid yet.")
    return str(pubkey)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(secret: str, address: str, ttl_s: int, now: float | None = None) -> tuple[str, float]:
    """
    Make a session token for a verified wallet.

    :param secret: The session secret.
    :param address: The wallet address (base58).
    :param ttl_s: Seconds the session lasts.
    :param now: Current time (injectable for tests).
    :return: The token and its expiry timestamp.
    """
    expires = int((time.time() if now is None else now) + ttl_s)
    body = _b64(json.dumps({"a": address, "e": expires}, separators=(",", ":")).encode())
    return f"{body}.{_mac(secret, 'session:' + body)}", float(expires)


def check_token(secret: str, token: str, now: float | None = None) -> str:
    """
    Verify a session token and return its wallet address.

    :param secret: The session secret.
    :param token: The bearer token.
    :param now: Current time (injectable for tests).
    :return: The wallet address (base58).
    :raises SignInError: If the token is forged, malformed or expired.
    """
    body, _, mac = token.partition(".")
    if not body or not hmac.compare_digest(mac, _mac(secret, "session:" + body)):
        raise SignInError("Invalid session.")
    try:
        claims = json.loads(_unb64(body))
        address, expires = str(claims["a"]), int(claims["e"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SignInError("Invalid session.") from exc
    if expires < (time.time() if now is None else now):
        raise SignInError("Session expired; sign in again.")
    return address


def nonce_response(secret: str, *, domain: str, uri: str, chain_id: str, ttl_s: int) -> dict[str, str]:
    """
    Everything a client needs to build the sign-in message (``GET /v1/auth/nonce``).

    :param secret: The session secret.
    :param domain: The bound domain.
    :param uri: The site URI.
    :param chain_id: ``mainnet`` or ``devnet``.
    :param ttl_s: Nonce lifetime.
    :return: The sign-in input fields.
    """
    now = time.time()
    nonce, expires = issue_nonce(secret, ttl_s, now)
    return {
        "domain": domain,
        "uri": uri,
        "statement": STATEMENT,
        "nonce": nonce,
        "issued_at": _iso(now),
        "expiration_time": _iso(expires),
        "version": "1",
        "chain_id": chain_id,
    }
