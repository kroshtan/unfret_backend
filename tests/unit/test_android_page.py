"""/android: the signed APK's download page, coming soon until a release is published, and not linked yet."""

import hashlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from unfret import android_release
from unfret.main import app
from unfret.routes.pages import STATIC_DIR, asset_tag

APK = b"PK\x03\x04 not really an apk, but bytes with a known digest" * 40_000
RELEASE: dict[str, Any] = {
    "version": "0.1.0",
    "version_code": 1,
    "commit": "3ebfce5" + "0" * 33,
    "size": len(APK),
    "sha256": hashlib.sha256(APK).hexdigest(),
    "cert_sha256": android_release.SIGNING_CERT_SHA256,
    "min_sdk": 26,
    "permissions": list(android_release.DEFAULT_PERMISSIONS),
    "published": "2026-11-18",
    "prerelease": False,
}
BETA: dict[str, Any] = {**RELEASE, "version": "0.1.0-beta.1", "prerelease": True}
BETA_URL = "https://git.kroshtan.com/kroshtan/unfret_android/releases/download/v0.1.0-beta.1/unfret-0.1.0-beta.1.apk"
BETA_NOTE = (
    "This is a beta, an early version: things may still change. Tell us what doesn't work at "
    '<a href="mailto:hello@unfret.net">hello@unfret.net</a>.'
)
APK_URL = "https://git.kroshtan.com/kroshtan/unfret_android/releases/download/v0.1.0/unfret-0.1.0.apk"
CERT = "3E:71:EF:6B:76:FA:BA:49:41:97:22:AE:99:8A:42:4B:38:A7:E9:05:C6:A4:48:4A:37:F8:A9:6F:E4:5F:66:34"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
def release_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "android-release.json"
    monkeypatch.setattr(android_release, "RELEASE_PATH", path)
    return path


@pytest.fixture
def published(release_file: Path) -> Path:
    release_file.write_text(json.dumps(RELEASE))
    return release_file


def _text(page: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", page).split())


@pytest.mark.parametrize("state", ["coming soon", "published"])
async def test_the_page_has_a_strict_policy_the_shared_frame_and_nothing_from_elsewhere(
    client: httpx.AsyncClient, release_file: Path, state: str
) -> None:
    if state == "published":
        release_file.write_text(json.dumps(RELEASE))
    response = await client.get("/android")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    for directive in ("default-src 'none'", "script-src 'self'", "form-action 'none'", "frame-ancestors 'none'"):
        assert directive in csp, directive
    page = response.text
    assert "{{" not in page and "}}" not in page
    assert page.count('<header class="site-header">') == 1 and page.count('<footer class="site-footer">') == 1
    for tag in re.findall(r"<(?:script|img|link)\b[^>]*>", page):
        address = re.search(r'\b(?:src|href)="([^"]*)"', tag)
        assert address and address.group(1).startswith(f"/static/{asset_tag()}/"), tag
    assert "<style" not in page and " style=" not in page and "<iframe" not in page
    assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page) is None  # no inline scripts
    # Every outside address is Unfret's own, over HTTPS.
    for link in re.findall(r'href="(https?://[^"]*)"', page):
        assert link.startswith(("https://git.kroshtan.com/kroshtan/", "https://github.com/kroshtan/",
                                "https://kroshtan.com")), link  # fmt: skip


async def test_the_page_is_noindex_until_it_is_linked(client: httpx.AsyncClient, release_file: Path) -> None:
    response = await client.get("/android")
    assert response.headers["x-robots-tag"] == "noindex"
    assert '<meta name="robots" content="noindex">' in response.text


async def test_coming_soon_offers_no_file(client: httpx.AsyncClient, release_file: Path) -> None:
    page = (await client.get("/android")).text
    text = _text(page)

    assert "Download coming soon" in text and "The signed app isn't published yet." in text
    assert ".apk" not in page and "releases/download" not in page and " download>" not in page
    assert 'href="/check"' in page  # what to use meanwhile
    # The signing certificate is known before the first release; what the app asks for is 0.1.0's.
    assert CERT in page and "Listed here with each release." in text
    assert "android.permission.INTERNET" in page and "Android 8.0 or newer" in text


async def test_a_published_release_is_offered_with_what_verifies_it(
    client: httpx.AsyncClient, published: Path
) -> None:
    page = (await client.get("/android")).text
    text = _text(page)

    assert "coming soon" not in text.lower()
    assert f'href="{APK_URL}"' in page and page.count(APK_URL) == 1
    assert f"Download Unfret 0.1.0 (APK, {len(APK) / 1e6:.1f} MB)" in text
    assert f"{len(APK):,} bytes" in text and "0.1.0, published November 18, 2026" in text
    assert f"unfret-0.1.0.apk (SHA-256) {RELEASE['sha256']}" in text and CERT in page
    assert 'href="https://git.kroshtan.com/kroshtan/unfret_android/releases/tag/v0.1.0"' in page
    assert 'href="https://git.kroshtan.com/kroshtan/unfret_android/releases/download/v0.1.0/SHA256SUMS"' in page
    assert "Android 8.0 or newer" in text
    assert "Beta" not in text and 'class="beta' not in page


async def test_a_beta_is_labelled_and_says_what_that_means(client: httpx.AsyncClient, release_file: Path) -> None:
    release_file.write_text(json.dumps(BETA))
    page = (await client.get("/android")).text
    text = _text(page)

    assert "coming soon" not in text.lower()
    assert f'href="{BETA_URL}"' in page and page.count(BETA_URL) == 1
    assert f"Download Unfret 0.1.0-beta.1 (APK, {len(APK) / 1e6:.1f} MB)" in text
    # The label sits next to the version; the one sentence says what a beta is and where to report what doesn't work.
    assert '<dd>0.1.0-beta.1 <span class="beta">Beta</span>, published November 18, 2026</dd>' in page
    assert " ".join(page.split()).count(BETA_NOTE) == 1
    # The install steps and the certificate check are the same as for any release.
    assert f"unfret-0.1.0-beta.1.apk (SHA-256) {RELEASE['sha256']}" in text and CERT in page
    assert 'href="https://git.kroshtan.com/kroshtan/unfret_android/releases/tag/v0.1.0-beta.1"' in page
    for phrase in ("Allow from this source", "Turn it off again.", "apksigner verify --print-certs"):
        assert phrase in text, phrase


async def test_coming_soon_says_nothing_about_a_beta(client: httpx.AsyncClient, release_file: Path) -> None:
    page = (await client.get("/android")).text
    assert "Download coming soon" in page and "Beta" not in _text(page) and "This is a beta" not in page


def test_a_release_without_the_flag_is_a_stable_one() -> None:
    # release.json from before the flag: a version without a pre-release suffix is still read.
    facts = {key: value for key, value in RELEASE.items() if key != "prerelease"}
    release = android_release.parse(facts)
    assert release is not None and release.prerelease is False


async def test_the_page_says_how_to_install_update_and_what_it_asks_for(
    client: httpx.AsyncClient, published: Path
) -> None:
    text = _text((await client.get("/android")).text)
    for phrase in (
        "Share a link to Unfret from any app and check it on your phone itself",
        "It reads the page on your phone",
        "Allow from this source",
        "Install unknown apps",
        "Turn it off again.",
        "The app doesn't update itself yet.",
        "it installs over the old one and keeps your pass",
        "signed with the same key",
        "apksigner verify --print-certs",
        "Internet ( android.permission.INTERNET )",
        "No location, contacts, camera, microphone, files or notifications.",
        "Nothing is read until you tap Check this page.",
        "Unfret will be on Google Play later",
    ):
        assert phrase in text, phrase
    page = (await client.get("/android")).text
    assert 'href="/privacy"' in page
    assert 'href="https://git.kroshtan.com/kroshtan/unfret_android/src/branch/main/PRIVACY.md"' in page


async def test_an_unknown_permission_is_shown_by_name(client: httpx.AsyncClient, release_file: Path) -> None:
    release_file.write_text(json.dumps({**RELEASE, "permissions": ["android.permission.INTERNET", "x.y.NEW"]}))
    page = (await client.get("/android")).text
    assert "<li><code>x.y.NEW</code></li>" in page


@pytest.mark.parametrize(
    "change",
    [
        {"cert_sha256": "0" * 64},  # signed with another key
        {"sha256": "not-a-digest"},
        {"version": "0.1.0/../../evil"},
        {"version": "1.0"},
        {"version": "0.1.0-"},
        {"version": "0.1.0-beta 1"},
        {"version": "0.1.0-beta/../x", "prerelease": True},
        {"version": "0.1.0+build.5"},
        {"version": "0.1.0-beta.1"},  # a beta that doesn't say so
        {"version": "0.1.0-beta.1", "prerelease": "true"},
        {"prerelease": True},  # a stable version marked as a beta
        {"prerelease": None},
        {"commit": "main"},
        {"size": 0},
        {"size": "123"},
        {"size": True},
        {"size": android_release.MAX_APK_BYTES + 1},
        {"min_sdk": None},
        {"permissions": "android.permission.INTERNET"},
        {"permissions": ['"><script>']},
        {"published": "yesterday"},
    ],
)
async def test_a_release_that_does_not_check_out_is_not_offered(
    client: httpx.AsyncClient, release_file: Path, change: dict[str, Any]
) -> None:
    release_file.write_text(json.dumps({**RELEASE, **change}))
    assert android_release.load() is None
    page = (await client.get("/android")).text
    assert "Download coming soon" in page and "releases/download" not in page


@pytest.mark.parametrize("content", ["", "{", "[]", "null"])
def test_an_unreadable_file_means_coming_soon(release_file: Path, content: str) -> None:
    release_file.write_text(content)
    assert android_release.load() is None


def test_the_fingerprint_reads_with_or_without_colons() -> None:
    release = android_release.parse({**RELEASE, "cert_sha256": CERT})
    assert release is not None and release.cert_sha256 == android_release.SIGNING_CERT_SHA256
    assert android_release.fingerprint(android_release.SIGNING_CERT_SHA256) == CERT


@pytest.mark.parametrize("facts", [RELEASE, BETA], ids=["stable", "beta"])
def test_the_facts_round_trip(release_file: Path, facts: dict[str, Any]) -> None:
    release = android_release.parse(facts)
    assert release is not None
    android_release.write(release, release_file)
    assert json.loads(release_file.read_text()) == facts and android_release.load() == release


def test_no_release_is_published_in_the_repository_yet() -> None:
    # The first one comes from `make android-release TAG=v0.1.0` after the release workflow ran.
    path = STATIC_DIR / "android-release.json"
    assert not path.exists() or android_release.load(path) is not None


@pytest.mark.parametrize("path", ["/", "/nl", "/extension", "/check"])
async def test_the_page_is_not_linked_from_the_ways_to_get_unfret(client: httpx.AsyncClient, path: str) -> None:
    # Floris's call (2026-11-18): reachable by its address, not yet next to the other ways to get Unfret.
    page = (await client.get(path)).text
    assert 'href="/android' not in page and ".apk" not in page


def test_the_menu_and_footer_do_not_link_it() -> None:
    for name in ("header.html", "footer.html"):
        assert "/android" not in (STATIC_DIR / "partials" / name).read_text(), name


def _forge(
    release: dict[str, Any], apk: bytes, assets: list[str] | None = None, version: str = "0.1.0"
) -> httpx.Client:
    base = f"https://git.kroshtan.com/kroshtan/unfret_android/releases/download/v{version}/"
    names = assets if assets is not None else [f"unfret-{version}.apk", "SHA256SUMS", "release.json"]

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{android_release.RELEASES_API}/tags/v{version}":
            return httpx.Response(200, json={"assets": [{"name": n, "browser_download_url": base + n} for n in names]})
        if url == base + "release.json":
            return httpx.Response(200, json=release)
        if url == base + f"unfret-{version}.apk":
            return httpx.Response(200, content=apk)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(answer))


def test_fetch_checks_the_apk_against_its_facts() -> None:
    with _forge(RELEASE, APK) as client:
        release = android_release.fetch("v0.1.0", client)
    assert release == android_release.parse(RELEASE)


def test_fetch_takes_a_beta_tag(release_file: Path) -> None:
    # make android-release TAG=v0.1.0-beta.1
    with _forge(BETA, APK, version="0.1.0-beta.1") as client:
        release = android_release.fetch("v0.1.0-beta.1", client)
    assert release.prerelease and release.tag == "v0.1.0-beta.1" and release.apk_url == BETA_URL
    android_release.write(release, release_file)
    assert json.loads(release_file.read_text())["prerelease"] is True


@pytest.mark.parametrize(
    ("release", "apk", "assets", "error"),
    [
        (RELEASE, APK + b"x", None, "doesn't match"),
        (RELEASE, APK[:-1] + b"y", None, "doesn't match"),
        ({**RELEASE, "cert_sha256": "1" * 64}, APK, None, "not signed with Unfret's release key"),
        (RELEASE, APK, ["unfret-0.1.0.apk"], "has no release.json"),
        (RELEASE, APK, ["release.json"], "isn't attached"),
        ({**RELEASE, "version": "0.2.0"}, APK, None, "describes v0.2.0"),
    ],
)
def test_fetch_refuses_a_release_that_does_not_match(
    release: dict[str, Any], apk: bytes, assets: list[str] | None, error: str
) -> None:
    with _forge(release, apk, assets) as client, pytest.raises(ValueError, match=error):
        android_release.fetch("v0.1.0", client)
