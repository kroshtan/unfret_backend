"""
Ownership proofs: the public origin signed by each ``payTo`` key, for x402 directories (x402scan) to verify.

x402scan marks an origin's resources as verified when one of the origin's ``ownershipProofs`` is a signature of the
origin string by the ``payTo`` address of the resource's payment option. The signed message is the origin exactly as
a browser's ``URL.origin`` gives it: ``https://unfret.net``, no trailing slash, no path. Per network:

* Solana: an ed25519 signature of the origin's UTF-8 bytes by the ``payTo`` key, base58 (x402scan verifies it with
  ``nacl.sign.detached.verify`` against the base58-decoded address);
* EVM: an EIP-191 ``personal_sign`` signature of the origin, ``0x``-prefixed hex (x402scan recovers the signer with
  viem's ``recoverMessageAddress`` and compares it with ``payTo``).

Sources: x402scan's ``docs/DISCOVERY.md`` (where the proofs go) and ``apps/scan/src/lib/ownership-proof.ts`` (how
they are verified); ``@agentcash/discovery``, which x402scan uses to read them from the OpenAPI document
(``x-agentcash-provenance.ownershipProofs`` first, then ``x-discovery.ownershipProofs``).

The server signs with the hot keys it already holds, in process: only the origin of ``public_base_url``, checked by
:func:`check_origin`, is ever signed this way. A changed key gives a changed proof at the next start; without a key
(x402 off) there is no proof.
"""

import re
from collections.abc import Iterable
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

# scheme://host[:port], as URL.origin serialises it (lower-case host, no default port, no trailing slash).
_ORIGIN = re.compile(r"https?://(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*|\[[0-9a-f:.]+\])(?::[1-9][0-9]{0,4})?")
_DEFAULT_PORTS = {"http": 80, "https": 443}
# An origin is far too short to parse as a Solana transaction message (its first bytes would ask for over a hundred
# signatures and accounts), so a proof can never double as a transaction signature.
_MAX_ORIGIN = 300

# The top-level OpenAPI extensions that carry the proofs: the agentcash discovery spec's own, then x402scan's
# documented one. Both are written, so a reader of either finds them.
OPENAPI_KEYS = ("x-agentcash-provenance", "x-discovery")


def origin_of(url: str) -> str:
    """
    The origin of a URL, serialised as a browser's ``URL.origin`` (the string x402scan verifies the proofs against).

    :param url: An ``http`` or ``https`` URL, e.g. the public base URL (a path or trailing slash is dropped).
    :return: ``scheme://host[:port]``, with the port only when it isn't the scheme's default.
    :raises ValueError: If the URL isn't ``http``/``https`` with a host, or has credentials in it.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        raise ValueError("not an http(s) URL with a host")
    if ":" in host:  # IPv6
        host = f"[{host}]"
    port = parts.port
    return f"{scheme}://{host}" if port in (None, _DEFAULT_PORTS[scheme]) else f"{scheme}://{host}:{port}"


def check_origin(origin: str) -> str:
    """
    Make sure a string is a bare origin before a hot key signs it, so the proof can't sign anything else.

    :param origin: The string to sign.
    :return: The origin, unchanged.
    :raises PermissionError: If it is anything but ``scheme://host[:port]`` in canonical form.
    """
    if len(origin) > _MAX_ORIGIN or not _ORIGIN.fullmatch(origin):
        raise PermissionError("refusing to sign: only the public origin is signed as an ownership proof")
    return origin


@runtime_checkable
class OwnershipProver(Protocol):
    """A payment service that can prove its ``payTo`` owns an origin."""

    def ownership_proof(self, origin: str) -> str | None:
        """
        The origin signed by the service's ``payTo`` key.

        :param origin: The origin (from :func:`origin_of`).
        :return: The proof, or None while the service holds no key (off).
        """


def ownership_proofs(public_base_url: str, services: Iterable[object]) -> list[str]:
    """
    One proof per ``payTo`` the services hold a key for: the Solana hot wallet's, and Base's once it signs.

    TODO(base hot wallet): once Base pays to a server-held hot key (``UNFRET_X402_BASE_HOT_KEY``), give ``BaseX402``
    an ``ownership_proof(origin)`` that returns None unless Base is enabled with that key, and else
    ``self.hot.ownership_proof(origin)``, where ``BaseHotWallet.ownership_proof`` is
    ``self._account.sign_message(encode_defunct(text=check_origin(origin))).signature.to_0x_hex()`` (EIP-191, from
    ``eth_account.messages``). It is then picked up here with no other change.

    :param public_base_url: The public base URL (its origin is what is signed).
    :param services: The payment services (any that isn't an :class:`OwnershipProver` is skipped).
    :return: The proofs, in the services' order (empty when none holds a key, or the base URL has no usable origin).
    """
    try:
        origin = check_origin(origin_of(public_base_url))
    except ValueError, PermissionError:  # a misconfigured base URL: no proof rather than a broken discovery document
        return []
    return [
        proof
        for service in services
        if isinstance(service, OwnershipProver) and (proof := service.ownership_proof(origin))
    ]
