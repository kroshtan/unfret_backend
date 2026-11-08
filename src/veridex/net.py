"""Outbound HTTP helpers that are safe to point at user-supplied URLs."""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

_USER_AGENT = "Mozilla/5.0 (compatible; Veridex/1.0)"
_DEFAULT_TIMEOUT = 10.0  # seconds
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL points at a non-public address or uses a disallowed scheme."""


def hostname(url: str) -> str:
    """
    Return the lowercase hostname of a URL without a leading ``www.``.

    Bare domains (``example.com/path``) are accepted as well.

    :param url: A full URL or bare domain string.
    :return: Hostname such as ``"example.com"``, or ``""`` if none can be parsed.
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").removeprefix("www.")


async def _assert_public(url: str) -> str:
    """
    Ensure ``url`` is http(s) and every address its host resolves to is public.

    This blocks server-side request forgery against loopback, private-network,
    link-local (cloud metadata) and other reserved ranges.

    :param url: Absolute URL to validate.
    :return: One of the validated addresses, for the caller to connect to.
    :raises UnsafeURLError: If the scheme or any resolved address is not allowed.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError(f"Unsupported URL: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Cannot resolve host {parsed.hostname!r}") from exc
    addresses = [ipaddress.ip_address(sockaddr[0]) for *_, sockaddr in infos]
    if not addresses or not all(address.is_global for address in addresses):
        raise UnsafeURLError(f"Host {parsed.hostname!r} resolves to a non-public address")
    return str(addresses[0])


def _pinned_request(client: httpx.AsyncClient, url: str, address: str) -> httpx.Request:
    """
    Build a GET for ``url`` that connects to ``address`` instead of resolving the host again.

    Resolving twice would let a hostile DNS server answer with a public address for the check and a private
    one for the connection (DNS rebinding). The original host still goes in the ``Host`` header and in TLS SNI,
    so virtual hosting and certificate verification work as usual.

    :param client: The client the request will be sent with.
    :param url: The validated URL.
    :param address: A validated address of the URL's host.
    :return: The request, ready to send.
    """
    target = httpx.URL(url)
    host_header = target.host if target.port is None else f"{target.host}:{target.port}"
    return client.build_request(
        "GET",
        target.copy_with(host=address),
        headers={"Host": host_header},
        extensions={"sni_hostname": target.host},
    )


async def fetch_public(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """
    GET a public URL, validating the target of every redirect hop.

    :param url: Absolute http(s) URL to fetch.
    :param timeout: Per-request timeout in seconds.
    :param max_bytes: Maximum response body size; larger bodies are rejected.
    :param transport: Optional httpx transport (used to stub the network in tests).
    :return: The response body.
    :raises ValueError: If the body exceeds ``max_bytes``, or (as :class:`UnsafeURLError`)
        if any hop targets a non-public address. Network errors and non-2xx responses
        raise :class:`httpx.HTTPError`.
    :raises httpx.TooManyRedirects: If the redirect chain is too long.
    """
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _USER_AGENT}, transport=transport) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            address = await _assert_public(url)
            response = await client.send(_pinned_request(client, url, address), stream=True)
            try:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ValueError(f"Response from {url} exceeds {max_bytes} bytes")
                return bytes(body)
            finally:
                await response.aclose()
    raise httpx.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects fetching {url}")
