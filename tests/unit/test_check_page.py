"""The link checker (/check): its page, the web app manifest and share target, the service worker and the fetch."""

import json
import logging
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from veridex import page_fetch
from veridex.main import app
from veridex.net import UnsafeURLError
from veridex.routes.pages import STATIC_DIR


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_the_page_is_served_with_a_strict_policy(client: httpx.AsyncClient) -> None:
    response = await client.get("/check")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "connect-src 'self'" in csp
    assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response.headers["referrer-policy"] == "no-referrer"
    assert '<script type="module" src="/static/check.js">' in response.text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in response.text
    assert "http://" not in response.text and "https://" not in response.text.replace("https://shop.example", "")


async def test_a_shared_link_is_never_put_into_the_page_by_the_server(client: httpx.AsyncClient) -> None:
    # The link is read by the page's script in the browser; the server answers every /check the same.
    plain = await client.get("/check")
    shared = await client.get(
        "/check",
        params={
            "title": "Sweater",
            "text": "Look https://secret-shop.test/p/1",
            "url": "https://secret-shop.test/p/1",
        },
    )

    assert shared.status_code == 200
    assert shared.text == plain.text
    assert "secret-shop" not in shared.text


async def test_the_manifest_makes_an_installable_share_target(client: httpx.AsyncClient) -> None:
    response = await client.get("/manifest.webmanifest")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/manifest+json"
    manifest = response.json()
    assert manifest["name"] == "Unfret" and manifest["start_url"] == "/check"
    assert manifest["display"] == "standalone"
    assert manifest["share_target"] == {
        "action": "/check",
        "method": "GET",
        "params": {"title": "title", "text": "text", "url": "url"},
    }
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])
    for icon in manifest["icons"]:
        served = await client.get(icon["src"])
        assert served.status_code == 200, icon["src"]
        assert served.headers["content-type"] == icon["type"]


async def test_the_service_worker_caches_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("/check-sw.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    script = response.text
    assert "caches." not in script and "indexedDB" not in script and "localStorage" not in script
    # It only turns a shared link in the query into a fragment, which the browser never sends.
    assert 'url.pathname !== "/check"' in script and "/check#" in script


@pytest.mark.parametrize("asset", ["check.js", "check-core.js"])
async def test_the_scripts_are_served(client: httpx.AsyncClient, asset: str) -> None:
    response = await client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript"}


def test_the_page_keeps_only_the_sign_in() -> None:
    script = (STATIC_DIR / "check.js").read_text()
    # One localStorage key, the account, and every access wrapped in try/catch (private modes throw).
    assert script.count("localStorage.") == 2 + 1  # get, set, remove
    assert script.count('"unfret-account"') == 1
    assert "sessionStorage" not in script and "indexedDB" not in script
    # The clipboard is read only from the "Paste link" button's handler.
    assert script.count("clipboard.readText()") == 1
    assert 'pasteButton.addEventListener("click", () => void paste())' in script


async def test_home_and_extension_pages_link_to_the_checker(client: httpx.AsyncClient) -> None:
    assert 'href="/check"' in (await client.get("/")).text
    assert 'href="/check"' in (STATIC_DIR / "extension.html").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_link_is_found_in_shared_text(tmp_path: Path) -> None:
    module = tmp_path / "check-core.mjs"
    module.write_text((STATIC_DIR / "check-core.js").read_text())
    cases = {
        "Wool sweater https://shop.example/p/1?x=2": "https://shop.example/p/1?x=2",
        "Look at this: https://shop.example/p/1!": "https://shop.example/p/1",
        "(see https://en.wikipedia.org/wiki/Foo_(bar))": "https://en.wikipedia.org/wiki/Foo_(bar)",
        "shop.example/item": "https://shop.example/item",
        "  https://WWW.Shop.Example/A  ": "https://www.shop.example/A",
        "no link here": "",
        "javascript:alert(1)": "",
        "ftp://shop.example/file": "",
    }
    shared = "title=Sweater&text=Buy%20it%3A%20https%3A%2F%2Fshop.example%2Fp%2F9"
    script = (
        f"import {{ extractLink, sharedLink }} from {json.dumps(module.as_uri())};"
        f"const cases = {json.dumps(list(cases))};"
        "const out = cases.map((c) => extractLink(c));"
        f"out.push(sharedLink(new URLSearchParams({json.dumps(shared)})));"
        f"out.push(sharedLink(new URLSearchParams('url=&text=Title%20only&title=x')));"
        "process.stdout.write(JSON.stringify(out));"
    )
    out = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True, timeout=30
        ).stdout
    )

    assert out == [*cases.values(), "https://shop.example/p/9", ""]


# ── Fetching the page ─────────────────────────────────────────────────────────────────────────────────────────


def _refusal(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://shop.test/")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_refusal(403), "PAGE_BLOCKED"),
        (_refusal(999), "PAGE_BLOCKED"),
        (_refusal(404), "PAGE_UNAVAILABLE"),
        (_refusal(500), "PAGE_UNAVAILABLE"),
        (httpx.ConnectError("x"), "PAGE_UNAVAILABLE"),
        (ValueError("too big"), "PAGE_UNAVAILABLE"),
        (UnsafeURLError("private"), "INVALID_URL"),
    ],
)
async def test_fetch_failures_are_named(monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str) -> None:
    async def refuse(url: str, **_: Any) -> bytes:
        raise error

    monkeypatch.setattr(page_fetch, "fetch_public", refuse)
    with pytest.raises(page_fetch.PageUnreadableError) as raised:
        await page_fetch.fetch_page("https://shop.test/p")
    assert raised.value.reason == reason
    assert "shop.test" not in raised.value.detail


@pytest.mark.parametrize(
    ("html", "thin"),
    [
        ("<html><body><div id='app'></div></body></html>", True),
        ("<html><body><p>" + "Merino wool sweater, knitted in Portugal. " * 20 + "</p></body></html>", False),
        (
            (
                "<html><head><script type='application/ld+json'>"
                '{"@type": "Product", "name": "Sweater", "offers": {"price": "49.00", "priceCurrency": "EUR"}}'
                "</script></head><body><div id='app'></div></body></html>"
            ),
            False,
        ),
    ],
)
def test_an_empty_shell_is_too_thin(html: str, thin: bool) -> None:
    assert page_fetch.is_too_thin(html, "https://shop.test/p") is thin


def test_httpx_never_logs_the_urls_it_fetches() -> None:
    # veridex.main configured logging (veridex.log.configure) on import; httpx's request lines, which carry the
    # full URL, are below its level.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
