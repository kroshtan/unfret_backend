"""
The pass's phone link: ``/pass`` (the Android app's verified App Link, else a hop to ``/check``) and assetlinks.json.

The QR code on /my-checks holds ``<site>/pass#pass=<pass>``. With Unfret for Android installed, Android opens it in
the app, because ``/.well-known/assetlinks.json`` names the app's package and release certificate; anywhere else
``/pass`` forwards to ``/check`` with the fragment, and ``/check#pass=…`` (in older codes too) asks before it keeps
the pass.
"""

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from unfret import android_app_links, android_release
from unfret.main import app
from unfret.routes.pages import STATIC_DIR, asset_tag

PASS = "ufk_abcdefghij_" + "a" * 52
# unfret_android/docs/release-cert.sha256, as assetlinks.json writes it; the package is the app's applicationId.
CERT = "3E:71:EF:6B:76:FA:BA:49:41:97:22:AE:99:8A:42:4B:38:A7:E9:05:C6:A4:48:4A:37:F8:A9:6F:E4:5F:66:34"
APP_REPO = Path(__file__).resolve().parents[3] / "unfret_android"


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ── /.well-known/assetlinks.json ─────────────────────────────────────────────────────────────────────────────────


async def test_assetlinks_names_the_app_and_its_release_certificate(client: httpx.AsyncClient) -> None:
    response = await client.get("/.well-known/assetlinks.json", follow_redirects=False)

    # Android's verifier is strict: a 200, application/json, and no redirect.
    assert response.status_code == 200 and "location" not in response.headers
    assert response.headers["content-type"] == "application/json"
    assert response.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "net.unfret.android",
                "sha256_cert_fingerprints": [CERT],
            },
        }
    ]
    assert android_release.fingerprint(android_release.SIGNING_CERT_SHA256) == CERT


async def test_assetlinks_is_the_same_on_dev(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from unfret.config import settings  # noqa: PLC0415 - patched for this test only

    production = (await client.get("/.well-known/assetlinks.json")).json()
    monkeypatch.setattr(settings, "public_base_url", "https://dev.example.test/")
    monkeypatch.setattr(settings, "solana_network", "solana-devnet")
    assert (await client.get("/.well-known/assetlinks.json")).json() == production


@pytest.mark.skipif(not APP_REPO.is_dir(), reason="the app's checkout isn't next to this one")
def test_assetlinks_matches_the_app_repository() -> None:
    gradle = (APP_REPO / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    assert f'applicationId = "{android_app_links.PACKAGE_NAME}"' in gradle
    pinned = (APP_REPO / "docs" / "release-cert.sha256").read_text(encoding="utf-8").strip()
    assert pinned.replace(":", "").lower() == android_release.SIGNING_CERT_SHA256


# ── /pass ────────────────────────────────────────────────────────────────────────────────────────────────────────


async def test_the_pass_link_is_a_noindex_uncached_page_with_the_checkers_policy(client: httpx.AsyncClient) -> None:
    response = await client.get(android_app_links.PHONE_LINK_PATH, follow_redirects=False)
    check = await client.get("/check")

    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "noindex" in response.headers["x-robots-tag"]
    assert '<meta name="robots" content="noindex, nofollow">' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    for header in ("content-security-policy", "x-content-type-options", "cross-origin-opener-policy"):
        assert response.headers[header] == check.headers[header], header
    # The forward is the page's own script (the policy allows no inline script) ...
    assert f'<script src="/static/{asset_tag()}/pass-link.js"></script>' in response.text
    assert "<script>" not in response.text
    # ... and without one, the page says what the link is for and links to the checker.
    noscript = response.text.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
    assert 'href="/check"' in noscript and "JavaScript" in noscript


async def test_the_pass_link_script_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/static/{asset_tag()}/pass-link.js")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript"}


@pytest.mark.usefixtures("node")
def test_the_pass_link_forwards_to_the_checker_keeping_the_fragment(tmp_path: Path) -> None:
    runner = tmp_path / "run.cjs"
    runner.write_text(
        """
        const vm = require("node:vm");
        const fs = require("node:fs");
        const code = fs.readFileSync(process.argv[2], "utf8");
        const out = JSON.parse(process.argv[3]).map((hash) => {
          const calls = [];
          const location = { hash, assign: () => calls.push("assign"), replace: (to) => calls.push(to) };
          vm.runInNewContext(code, { location });
          return calls;
        });
        process.stdout.write(JSON.stringify(out));
        """
    )
    hashes = [f"#pass={PASS}", "", "#url=https%3A%2F%2Fshop.example%2Fitem"]
    out = subprocess.run(
        ["node", str(runner), str(STATIC_DIR / "pass-link.js"), json.dumps(hashes)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    # location.replace, so /pass stays out of the history; the fragment as it came, never sent to the server.
    assert json.loads(out) == [
        [f"/check#pass={PASS}"],
        ["/check"],
        ["/check#url=https%3A%2F%2Fshop.example%2Fitem"],
    ]


# ── /check#pass= still works (older QR codes, and where /pass forwards) ──────────────────────────────────────────


async def test_the_checker_still_takes_a_pass_from_the_fragment(client: httpx.AsyncClient) -> None:
    page = await client.get("/check")
    assert page.status_code == 200
    assert f'<script type="module" src="/static/{asset_tag()}/check.js">' in page.text
    assert 'id="pass-offer"' in page.text and "Use this pass here?" in page.text

    script = (STATIC_DIR / "check.js").read_text(encoding="utf-8")
    take = script.split("function takeSharedLink() {", 1)[1].split("\n}\n", 1)[0]
    assert "new URLSearchParams(window.location.hash.slice(1))" in take
    assert 'fragment.get("pass")' in take and "PASS_RE.test(pass)" in take
    assert 'history.replaceState(null, "", window.location.pathname)' in take  # cleared from the address bar
    # The pass found there goes to receivePass, which asks first (test_onboarding checks that it never just keeps it).
    assert "if (shared.pass) void receivePass(shared.pass);" in script
