"""Visitor addresses as rate-limit keys: IPv6 folded to its /64 (one subscriber), salted and hashed, never stored."""

import hashlib
import ipaddress
import secrets

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
