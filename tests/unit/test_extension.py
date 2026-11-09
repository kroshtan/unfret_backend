"""The self-hosted extension channel: the install page, the update manifest and the release files."""

import hashlib
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from veridex.main import app
from veridex.routes import extension
from veridex.routes.extension import RELEASE_DIR, current_release, linux_policy_command

# DigestInfo prefix for SHA-256 in EMSA-PKCS1-v1_5 (RFC 8017, section 9.2).
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── An independent CRX3 verifier (Chromium's rules, pure Python) for the shipped release ──────────────────────────


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7


def _fields(buf: bytes) -> list[tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(buf):
        tag, pos = _varint(buf, pos)
        assert tag & 7 == 2
        length, pos = _varint(buf, pos)
        out.append((tag >> 3, buf[pos : pos + length]))
        pos += length
    return out


def _der(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(buf[pos : pos + n], "big")
        pos += n
    return tag, buf[pos : pos + length], pos + length


def _rsa_public_numbers(spki: bytes) -> tuple[int, int]:
    _, seq, _ = _der(spki, 0)  # SubjectPublicKeyInfo
    _, _, pos = _der(seq, 0)  # AlgorithmIdentifier
    _, bits, _ = _der(seq, pos)  # BIT STRING
    _, rsa, _ = _der(bits[1:], 0)  # RSAPublicKey (after the unused-bits byte)
    _, n, pos = _der(rsa, 0)
    _, e, _ = _der(rsa, pos)
    return int.from_bytes(n, "big"), int.from_bytes(e, "big")


def _verify_pkcs1_sha256(spki: bytes, message: bytes, signature: bytes) -> bool:
    n, e = _rsa_public_numbers(spki)
    k = (n.bit_length() + 7) // 8
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    digest_info = _SHA256_PREFIX + hashlib.sha256(message).digest()
    return em == b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info


def _verify_crx3(crx: bytes) -> str:
    """Verify a CRX3 like Chromium does and return the extension ID."""
    assert crx[:4] == b"Cr24" and int.from_bytes(crx[4:8], "little") == 3
    size = int.from_bytes(crx[8:12], "little")
    header, archive = crx[12 : 12 + size], crx[12 + size :]
    fields = _fields(header)
    proofs = [dict(_fields(b)) for number, b in fields if number == 2]
    signed_header = next(b for number, b in fields if number == 10000)
    crx_id = dict(_fields(signed_header))[1]
    message = b"CRX3 SignedData\x00" + len(signed_header).to_bytes(4, "little") + signed_header + archive
    assert proofs and all(_verify_pkcs1_sha256(p[1], message, p[2]) for p in proofs)
    developer = next(p[1] for p in proofs if hashlib.sha256(p[1]).digest()[:16] == crx_id)
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(developer).hexdigest()[:32])


# ── The shipped release ──────────────────────────────────────────────────────────────────────────────────────────


def test_the_shipped_release_is_a_valid_crx3_with_the_manifest_id() -> None:
    release = current_release()
    assert release is not None, "no release in veridex/static/extension"
    crx = (RELEASE_DIR / release.crx).read_bytes()

    assert _verify_crx3(crx) == release.extension_id
    tampered = bytearray(crx)
    tampered[-1] ^= 1
    with pytest.raises(AssertionError):
        _verify_crx3(bytes(tampered))


def test_the_shipped_checksums_match_the_files() -> None:
    release = current_release()
    assert release is not None
    for line in release.checksums.strip().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((RELEASE_DIR / name).read_bytes()).hexdigest() == digest, name


async def test_update_manifest_is_served_as_xml(client: httpx.AsyncClient) -> None:
    release = current_release()
    assert release is not None
    response = await client.get("/extension/updates.xml")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert "max-age=300" in response.headers["cache-control"]
    assert f"appid='{release.extension_id}'" in response.text
    assert re.search(
        rf"codebase='https://[a-z.]+/extension/{re.escape(release.crx)}'", response.text
    )  # host: per release


async def test_release_files_are_served_with_their_types(client: httpx.AsyncClient) -> None:
    release = current_release()
    assert release is not None and release.zip is not None

    crx = await client.get(f"/extension/{release.crx}")
    assert crx.status_code == 200
    assert crx.headers["content-type"] == "application/x-chrome-extension"
    assert "immutable" in crx.headers["cache-control"]
    assert crx.content == (RELEASE_DIR / release.crx).read_bytes()

    assert (await client.get(f"/extension/{release.zip}")).headers["content-type"] == "application/zip"
    assert (await client.get("/extension/SHA256SUMS")).text == release.checksums


@pytest.mark.parametrize(
    "name",
    [
        "veridex-9.9.9.crx",
        "unfret-9.9.9.crx",
        "../extension.html",
        "..%2Fpages.py",
        "updates.xml.bak",
        "veridex-0.1.0.crx.pem",
        "x.crx",
        "other-1.0.crx",
    ],
)
async def test_other_names_are_not_found(client: httpx.AsyncClient, name: str) -> None:
    assert (await client.get(f"/extension/{name}")).status_code == 404


async def test_install_page_shows_the_release_and_policy_commands(client: httpx.AsyncClient) -> None:
    release = current_release()
    assert release is not None
    response = await client.get("/extension")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "<script" not in response.text
    assert "{{" not in response.text
    assert release.extension_id in response.text
    assert f'href="/extension/{release.crx}"' in response.text
    assert "/etc/brave/policies/managed/unfret.json" in response.text
    assert "http://" not in response.text.replace("http://test", "")


def test_linux_policy_command_force_installs_from_this_server() -> None:
    command = linux_policy_command("abcdefghijklmnopabcdefghijklmnop")
    assert command == (
        "sudo mkdir -p /etc/brave/policies/managed && sudo rm -f /etc/brave/policies/managed/veridex.json && echo "
        '\'{"ExtensionInstallForcelist":["abcdefghijklmnopabcdefghijklmnop;'
        "https://unfret.kroshtan.com/extension/updates.xml\"]}' "
        "| sudo tee /etc/brave/policies/managed/unfret.json > /dev/null"
    )


async def test_connect_links_to_the_install_page(client: httpx.AsyncClient) -> None:
    assert 'href="/extension"' in (await client.get("/connect")).text


# ── Nothing published ────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def no_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    empty = tmp_path / "extension"
    monkeypatch.setattr(extension, "RELEASE_DIR", empty)
    return empty


async def test_without_a_release_everything_is_404_and_the_page_says_so(
    client: httpx.AsyncClient, no_release: Path
) -> None:
    assert current_release(no_release) is None
    assert (await client.get("/extension/updates.xml")).status_code == 404
    assert (await client.get("/extension/veridex-0.1.0.crx")).status_code == 404
    page = await client.get("/extension")
    assert page.status_code == 200
    assert "No release is published yet" in page.text
    assert re.search(r"\{\{\w+\}\}", page.text) is None


def test_a_broken_manifest_is_no_release(tmp_path: Path) -> None:
    (tmp_path / "updates.xml").write_text("<gupdate")
    (tmp_path / "SHA256SUMS").write_text("")
    assert current_release(tmp_path) is None


def test_releases_named_after_either_brand_are_found(tmp_path: Path) -> None:
    for name in ("unfret", "veridex"):
        release_dir = tmp_path / name
        release_dir.mkdir()
        (release_dir / "updates.xml").write_text(
            "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>"
            "<app appid='abcdefghijklmnopabcdefghijklmnop'><updatecheck "
            f"codebase='https://unfret.kroshtan.com/extension/{name}-1.2.3.crx' version='1.2.3'/></app></gupdate>"
        )
        (release_dir / "SHA256SUMS").write_text("")
        (release_dir / f"{name}-1.2.3.crx").write_bytes(b"crx")
        (release_dir / f"{name}-chrome-1.2.3.zip").write_bytes(b"zip")
        release = current_release(release_dir)
        assert release is not None
        assert release.crx == f"{name}-1.2.3.crx"
        assert release.zip == f"{name}-chrome-1.2.3.zip"
