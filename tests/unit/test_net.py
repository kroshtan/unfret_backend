import socket

import httpx
import pytest

from unfret import net
from unfret.net import UnsafeURLError, fetch_public, hostname


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.Example.com/item/1", "example.com"),
        ("http://shop.example.co.uk:8080/x", "shop.example.co.uk"),
        ("example.com/path", "example.com"),
        ("", ""),
    ],
)
def test_hostname(url: str, expected: str) -> None:
    assert hostname(url) == expected


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Resolve hostnames from a dict instead of real DNS."""
    records: dict[str, str] = {}

    async def getaddrinfo(self: object, host: str, port: int, **_: object) -> list[tuple]:
        if host not in records:
            raise socket.gaierror(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (records[host], port))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", getaddrinfo)
    return records


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0"])
async def test_rejects_non_public_addresses(fake_dns: dict[str, str], address: str) -> None:
    fake_dns["internal.test"] = address
    with pytest.raises(UnsafeURLError):
        await net._assert_public("http://internal.test/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/", "http:///nohost"])
async def test_rejects_bad_schemes(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await net._assert_public(url)


async def test_fetches_public_url(fake_dns: dict[str, str]) -> None:
    fake_dns["shop.test"] = "93.184.216.34"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello"))
    assert await fetch_public("https://shop.test/img.jpg", transport=transport) == b"hello"


async def test_blocks_redirect_to_internal_address(fake_dns: dict[str, str]) -> None:
    fake_dns["shop.test"] = "93.184.216.34"
    fake_dns["metadata.test"] = "169.254.169.254"
    transport = httpx.MockTransport(
        lambda request: (
            httpx.Response(302, headers={"location": "http://metadata.test/latest"})
            if request.headers["host"] == "shop.test"
            else httpx.Response(200, content=b"secret")
        )
    )
    with pytest.raises(UnsafeURLError):
        await fetch_public("https://shop.test/img.jpg", transport=transport)


async def test_connects_to_the_validated_address(fake_dns: dict[str, str]) -> None:
    """The request goes to the address that was checked, so a second DNS answer cannot redirect it."""
    fake_dns["shop.test"] = "93.184.216.34"
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        fake_dns["shop.test"] = "127.0.0.1"  # a rebinding DNS server would answer this on the next lookup
        return httpx.Response(200, content=b"ok")

    assert await fetch_public("https://shop.test:8443/img.jpg", transport=httpx.MockTransport(respond)) == b"ok"
    (request,) = seen
    assert request.url.host == "93.184.216.34"
    assert request.url.port == 8443
    assert request.headers["host"] == "shop.test:8443"
    assert request.extensions["sni_hostname"] == "shop.test"


async def test_rejects_oversized_body(fake_dns: dict[str, str]) -> None:
    fake_dns["shop.test"] = "93.184.216.34"
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 100))
    with pytest.raises(ValueError, match="exceeds"):
        await fetch_public("https://shop.test/big", transport=transport, max_bytes=10)
