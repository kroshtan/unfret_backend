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


def _zstd_bomb(size: int) -> bytes:
    import zstandard  # noqa: PLC0415 - a dependency of a dependency; only this test needs it

    return zstandard.ZstdCompressor().compress(b"\0" * size)


async def test_compressed_body_is_bounded_while_decoding(fake_dns: dict[str, str]) -> None:
    """A small compressed answer that expands enormously is refused before it is expanded in memory."""
    import tracemalloc  # noqa: PLC0415

    fake_dns["shop.test"] = "93.184.216.34"
    bomb = _zstd_bomb(64 * 1024 * 1024)  # 64 MB of zeros in a few kB
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=httpx.ByteStream(bomb), headers={"content-encoding": "zstd"})
    )
    tracemalloc.start()
    try:
        with pytest.raises(ValueError):
            await fetch_public("https://shop.test/bomb", transport=transport, max_bytes=1024 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024


async def test_asks_only_for_encodings_it_can_bound(fake_dns: dict[str, str]) -> None:
    fake_dns["shop.test"] = "93.184.216.34"
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"ok")

    await fetch_public("https://shop.test/", transport=httpx.MockTransport(respond))
    assert seen[0].headers["accept-encoding"] == "gzip, deflate"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_decodes_gzip_and_deflate(fake_dns: dict[str, str], encoding: str) -> None:
    import gzip  # noqa: PLC0415
    import zlib  # noqa: PLC0415

    fake_dns["shop.test"] = "93.184.216.34"
    page = b"<html>" + b"product " * 5000 + b"</html>"
    body = gzip.compress(page) if encoding == "gzip" else zlib.compress(page)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=httpx.ByteStream(body), headers={"content-encoding": encoding})
    )
    assert await fetch_public("https://shop.test/", transport=transport) == page


async def test_gzip_bomb_is_refused(fake_dns: dict[str, str]) -> None:
    import gzip  # noqa: PLC0415

    fake_dns["shop.test"] = "93.184.216.34"
    bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=httpx.ByteStream(bomb), headers={"content-encoding": "gzip"})
    )
    with pytest.raises(ValueError, match="exceeds"):
        await fetch_public("https://shop.test/bomb", transport=transport, max_bytes=1024 * 1024)


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "::127.0.0.1", "::ffff:10.0.0.1"])
async def test_rejects_ipv6_forms_of_private_ipv4(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    async def getaddrinfo(self: object, host: str, port: int, **_: object) -> list[tuple]:
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, port, 0, 0))]

    monkeypatch.setattr("asyncio.base_events.BaseEventLoop.getaddrinfo", getaddrinfo)
    with pytest.raises(UnsafeURLError):
        await net._assert_public("http://nat64.test/")


async def test_a_refusal_carries_the_start_of_its_body(fake_dns: dict[str, str]) -> None:
    fake_dns["shop.test"] = "93.184.216.34"
    wall = b"<script>window.KPSDK={};</script>" + b"x" * 100_000
    transport = httpx.MockTransport(lambda _: httpx.Response(400, content=wall))

    with pytest.raises(net.RefusedError) as refused:
        await fetch_public("https://shop.test/p/1", transport=transport)

    assert refused.value.response.status_code == 400
    assert refused.value.head.startswith("<script>window.KPSDK")
    assert len(refused.value.head) == 32 * 1024  # the start only
    assert "shop.test" not in str(refused.value)  # the message names no page
