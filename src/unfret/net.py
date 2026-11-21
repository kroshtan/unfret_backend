"""Outbound HTTP helpers that are safe to point at user-supplied URLs."""

import asyncio
import ipaddress
import socket
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

_USER_AGENT = "Mozilla/5.0 (compatible; Unfret/1.0)"
_DEFAULT_TIMEOUT = 10.0  # seconds
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_MAX_REDIRECTS = 5
# Read of a refusal's body: enough for a bot wall's markers, never a whole error page.
_REFUSAL_HEAD_BYTES = 32 * 1024
# Only encodings whose decoding can be stopped at the size limit: zlib takes an output cap, httpx's own decoders (and
# zstd's or brotli's, should those libraries be installed) expand a whole chunk at once, so a few kilobytes of zstd
# could fill the memory before the size check sees them.
_ACCEPT_ENCODING = "gzip, deflate"
# IPv6 prefixes that carry an IPv4 address in their last 32 bits (NAT64, IPv4-compatible): judged by that address.
_EMBEDDED_IPV4 = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
    ipaddress.IPv6Network("::/96"),
)


class UnsafeURLError(ValueError):
    """Raised when a URL points at a non-public address or uses a disallowed scheme."""


class UnresolvableHostError(UnsafeURLError):
    """Raised when a URL's host doesn't resolve at all (a dead or mistyped domain), not to a private address."""


class RefusedError(httpx.HTTPStatusError):
    """A non-2xx answer, with the start of its body: a bot wall often says who it is only there."""

    def __init__(self, response: httpx.Response, head: bytes) -> None:
        """
        Describe the refusal (without the URL, which the caller may not want in a log).

        :param response: The answer.
        :param head: The first bytes of its decoded body (empty when it couldn't be read).
        """
        super().__init__(f"HTTP {response.status_code}", request=response.request, response=response)
        self.head = head.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class FetchedPage:
    """A fetched body and the address it came from, after any redirects (a short link's destination)."""

    body: bytes
    url: str


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
    :raises UnresolvableHostError: If the host doesn't resolve.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURLError(f"Unsupported URL: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnresolvableHostError(f"Cannot resolve host {parsed.hostname!r}") from exc
    addresses = [ipaddress.ip_address(sockaddr[0]) for *_, sockaddr in infos]
    if not addresses or not all(_is_public(address) for address in addresses):
        raise UnsafeURLError(f"Host {parsed.hostname!r} resolves to a non-public address")
    return str(addresses[0])


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Whether an address is globally reachable, judging IPv6 forms of an IPv4 address by that IPv4 address.

    :param address: A resolved address.
    :return: ``True`` for a public address.
    """
    if isinstance(address, ipaddress.IPv6Address) and any(address in net for net in _EMBEDDED_IPV4):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF).is_global
    return address.is_global


def _pinned_request(
    client: httpx.AsyncClient, url: str, address: str, extra: dict[str, str] | None = None
) -> httpx.Request:
    """
    Build a GET for ``url`` that connects to ``address`` instead of resolving the host again.

    Resolving twice would let a hostile DNS server answer with a public address for the check and a private
    one for the connection (DNS rebinding). The original host still goes in the ``Host`` header and in TLS SNI,
    so virtual hosting and certificate verification work as usual.

    :param client: The client the request will be sent with.
    :param url: The validated URL.
    :param address: A validated address of the URL's host.
    :param extra: More request headers (a ``Range``); never ``Host`` or ``Accept-Encoding``.
    :return: The request, ready to send.
    """
    target = httpx.URL(url)
    host_header = target.host if target.port is None else f"{target.host}:{target.port}"
    return client.build_request(
        "GET",
        target.copy_with(host=address),
        headers=(extra or {}) | {"Host": host_header, "Accept-Encoding": _ACCEPT_ENCODING},
        extensions={"sni_hostname": target.host},
    )


async def fetch_public(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    total_timeout: float = 25.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> bytes:
    """
    GET a public URL, validating the target of every redirect hop.

    Raises ``ValueError`` if the body exceeds ``max_bytes`` (``UnsafeURLError`` if a hop targets a non-public
    address), ``httpx.HTTPError`` on network errors and non-2xx answers (:class:`RefusedError`, with the start of
    the body, for 4xx and 5xx; ``httpx.TooManyRedirects`` for a long redirect chain), and ``TimeoutError`` once
    ``total_timeout`` has passed.

    :param url: Absolute http(s) URL to fetch.
    :param timeout: Per-request timeout in seconds (each read, connect, …).
    :param total_timeout: The whole fetch, redirects and a slowly trickling body included.
    :param max_bytes: Maximum response body size; larger bodies are rejected.
    :param transport: Optional httpx transport (used to stub the network in tests).
    :param headers: More request headers, sent on every hop (a ``Range`` for one record of a large file).
    :return: The response body.
    """
    page = await fetch_public_page(
        url, timeout=timeout, total_timeout=total_timeout, max_bytes=max_bytes, transport=transport, headers=headers
    )
    return page.body


async def fetch_public_page(
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    total_timeout: float = 25.0,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> FetchedPage:
    """
    Like :func:`fetch_public`, and also say where the redirects ended: the page's own address behind a short link.

    Every hop goes through the same check of its address, within the same redirect limit.

    :param url: Absolute http(s) URL to fetch.
    :param timeout: Per-request timeout in seconds (each read, connect, …).
    :param total_timeout: The whole fetch, redirects and a slowly trickling body included.
    :param max_bytes: Maximum response body size; larger bodies are rejected.
    :param transport: Optional httpx transport (used to stub the network in tests).
    :param headers: More request headers, sent on every hop.
    :return: The response body and the URL that answered it (``url`` itself without redirects).
    """
    async with asyncio.timeout(total_timeout):
        return await _fetch(url, timeout=timeout, max_bytes=max_bytes, transport=transport, headers=headers)


async def _fetch(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None,
    headers: dict[str, str] | None = None,
) -> FetchedPage:
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _USER_AGENT}, transport=transport) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            address = await _assert_public(url)
            response = await client.send(_pinned_request(client, url, address, headers), stream=True)
            try:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.is_error:
                    raise RefusedError(response, await _read_head(response, _REFUSAL_HEAD_BYTES))
                response.raise_for_status()
                return FetchedPage(await _read_bounded(response, max_bytes, url), url)
            finally:
                await response.aclose()
    raise httpx.TooManyRedirects(f"More than {_MAX_REDIRECTS} redirects fetching {url}")


def _decoder(encoding: str) -> Callable[[bytes, int], bytes] | None:
    """
    A size-capped decoder for a ``Content-Encoding``.

    :param encoding: The header's value.
    :return: ``decode(chunk, max_out)``, or ``None`` for an unencoded body.
    :raises ValueError: For an encoding we didn't ask for and can't decode within a limit.
    """
    name = encoding.strip().lower()
    if name in {"", "identity"}:
        return None
    if name not in {"gzip", "x-gzip", "deflate"}:
        raise ValueError(f"Unsupported content encoding {name!r}")
    # 32 + MAX_WBITS reads a gzip or a zlib header, whichever comes.
    state = zlib.decompressobj(32 + zlib.MAX_WBITS)

    def decode(chunk: bytes, max_out: int) -> bytes:
        out = bytearray()
        data = chunk
        while data and len(out) < max_out:
            out += state.decompress(data, max_out - len(out))
            data = state.unconsumed_tail
        return bytes(out)

    return decode


async def _read_head(response: httpx.Response, limit: int) -> bytes:
    """
    Read and decode the start of a body, best effort: whatever arrived before the limit or a failure.

    :param response: A streamed response.
    :param limit: Most bytes to keep.
    :return: The first ``limit`` decoded bytes at most.
    """
    if response.is_stream_consumed:
        return response.content[:limit]
    body = bytearray()
    try:
        decode = _decoder(response.headers.get("content-encoding", ""))
        async for chunk in response.aiter_raw():
            body += chunk if decode is None else decode(chunk, limit - len(body))
            if len(body) >= limit:
                break
    except ValueError, zlib.error, httpx.HTTPError:
        pass  # what arrived is enough, or nothing is
    return bytes(body[:limit])


async def _read_bounded(response: httpx.Response, max_bytes: int, url: str) -> bytes:
    """
    Read and decode a body, never holding more than ``max_bytes`` (plus one chunk of the encoded stream).

    :param response: A streamed response.
    :param max_bytes: Largest accepted decoded body.
    :param url: The URL, for the error message.
    :return: The decoded body.
    :raises ValueError: If the decoded body exceeds ``max_bytes`` or its encoding isn't supported.
    """
    if response.is_stream_consumed:
        # Already read and decoded in memory: a response built from bytes (test transports), never one off the network.
        if len(response.content) > max_bytes:
            raise ValueError(f"Response from {url} exceeds {max_bytes} bytes")
        return response.content
    decode = _decoder(response.headers.get("content-encoding", ""))
    body = bytearray()
    try:
        async for chunk in response.aiter_raw():
            # One byte over the limit is enough to know the body is too large.
            body += chunk if decode is None else decode(chunk, max_bytes + 1 - len(body))
            if len(body) > max_bytes:
                raise ValueError(f"Response from {url} exceeds {max_bytes} bytes")
    except zlib.error as exc:
        raise ValueError(f"Response from {url} could not be decoded") from exc
    return bytes(body)
