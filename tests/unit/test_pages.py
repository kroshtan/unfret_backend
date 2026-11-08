"""The /connect and /checkout pages: a strict CSP, their assets load, and the JS sign-in text matches ours."""

import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from veridex.credits import siws
from veridex.main import app
from veridex.routes.pages import STATIC_DIR


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.parametrize(
    ("path", "script"), [("/connect", "/static/connect.js"), ("/checkout", "/static/checkout.js")]
)
async def test_pages_are_served_with_a_strict_policy(client: httpx.AsyncClient, path: str, script: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert f'<script type="module" src="{script}">' in response.text
    assert "http://" not in response.text.replace("http://test", "")  # nothing loaded from elsewhere


@pytest.mark.parametrize("asset", ["wallet.js", "connect.js", "checkout.js", "site.css"])
async def test_assets_are_served(client: httpx.AsyncClient, asset: str) -> None:
    response = await client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript", "text/css"}


def test_scripts_make_no_third_party_requests() -> None:
    for script in STATIC_DIR.glob("*.js"):
        text = script.read_text()
        assert "https://" not in text and "http://" not in text, script.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_sign_in_text_matches_the_server(tmp_path: Path) -> None:
    fields = siws.nonce_response(
        "s", domain="veridex.kroshtan.com", uri="https://veridex.kroshtan.com", chain_id="mainnet", ttl_s=300
    )
    address = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    module = tmp_path / "wallet.mjs"
    module.write_text((STATIC_DIR / "wallet.js").read_text())
    call = f"signInText({json.dumps(fields)}, {json.dumps(address)})"
    script = f"import {{ signInText }} from {json.dumps(module.as_uri())}; process.stdout.write({call});"
    js_text = subprocess.run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True, timeout=30
    ).stdout

    python_text = siws.message_text(siws.SignInFields(address=address, **fields))
    assert js_text == python_text
