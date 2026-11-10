"""The /connect and /checkout pages: a strict CSP, their assets load, and the JS sign-in text matches ours."""

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from unfret.credits import siws
from unfret.main import app
from unfret.routes.pages import STATIC_DIR


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_privacy_statement_names_the_price_observations(client: httpx.AsyncClient) -> None:
    response = await client.get("/privacy")

    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "90 days" in response.text and "90 dagen" in response.text


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


# Links the user may click to reopen /connect inside a wallet app's browser: navigations, never requests.
WALLET_UNIVERSAL_LINKS = ("https://phantom.app/ul/browse/", "https://solflare.com/ul/v1/browse/")


# The link checker completes a typed bare domain ("shop.example/item") to a link; it is checked, never fetched here.
BARE_DOMAIN_PREFIX = "`https://${value}`"


def test_scripts_make_no_third_party_requests() -> None:
    for script in STATIC_DIR.glob("*.js"):
        text = script.read_text().replace(BARE_DOMAIN_PREFIX, "")
        for link in WALLET_UNIVERSAL_LINKS:
            text = text.replace(f"`{link}", "`")
        assert "https://" not in text and "http://" not in text, script.name


def test_wallet_links_are_only_assigned_as_hrefs() -> None:
    script = (STATIC_DIR / "connect.js").read_text()
    for link in WALLET_UNIVERSAL_LINKS:
        assert f".href =\n  `{link}" in script  # assigned to a link the user clicks, not fetched


@pytest.mark.usefixtures("node")
def test_js_sign_in_text_matches_the_server(tmp_path: Path) -> None:
    fields = siws.nonce_response("s", domain="unfret.kroshtan.com", uri="https://unfret.kroshtan.com", ttl_s=300)
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


def test_connect_page_has_a_phone_pairing_mode() -> None:
    html = (STATIC_DIR / "connect.html").read_text()
    script = (STATIC_DIR / "connect.js").read_text()
    for element in ('id="pairing"', 'id="pair-code"', 'id="pair-error"', 'id="pair-done"'):
        assert element in html
    assert "Sign in to the device showing" in html
    assert "/v1/auth/pair/" in script and "pairId" in script
    assert "pair_id: pairId" in (STATIC_DIR / "wallet.js").read_text()


def test_sign_in_never_asks_the_wallet_for_a_chain_id() -> None:
    # Phantom refuses the request when the chain ID doesn't match its selected network ("chain ID does not match").
    script = (STATIC_DIR / "wallet.js").read_text()
    assert "chainId:" not in script
    assert "Chain ID" not in script
    fields = siws.nonce_response("s", domain="d.test", uri="https://d.test", ttl_s=300)
    assert "Chain ID" not in siws.message_text(siws.SignInFields(address="x", **fields))


async def test_home_page_introduces_unfret_without_third_party_requests(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "<h1>Unfret</h1>" in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert 'href="/extension"' in response.text and 'href="/privacy"' in response.text
    # Its only script fills in "Spotted recently" from this server's /v1/showcase.
    assert response.text.count("<script") == 1
    assert '<script type="module" src="/static/showcase-list.js"></script>' in response.text
    assert 'id="spotted"' in response.text


def test_scripts_only_load_images_the_policy_allows() -> None:
    # The pages' CSP allows images from 'self' and data: only; a blob: URL (createObjectURL) would render nothing,
    # as the checkout's QR code once did.
    for script in STATIC_DIR.glob("*.js"):
        assert "createObjectURL" not in script.read_text(encoding="utf-8"), script.name


async def test_privacy_statement_describes_invites_in_both_languages(client: httpx.AsyncClient) -> None:
    text = (await client.get("/privacy")).text
    english, dutch = text.split('<h2 id="nl"', 1)
    assert "<strong>Invites</strong>" in english and "nothing links the buyer to the person they invited" in english
    assert "<strong>Uitnodigingen:</strong>" in dutch
    assert "niets koppelt de koper aan degene die hij uitnodigde" in dutch


def test_pages_show_invites_and_offer_to_redeem_one() -> None:
    wallet = (STATIC_DIR / "wallet.js").read_text()
    assert "Give this to someone who hasn't tried Unfret: it adds 3 checks to a new wallet or a new API key." in wallet
    assert "/v1/invites/redeem" in wallet and "navigator.clipboard.writeText(code)" in wallet  # Copy
    checkout = (STATIC_DIR / "checkout.js").read_text()
    assert '"X-Unfret-Claim"' in checkout and "inviteNodes(" in checkout  # the payment-received panel
    assert "to give away" in checkout  # each pack's offer
    for page in ("checkout", "connect", "check"):
        html = (STATIC_DIR / f"{page}.html").read_text()
        assert "Have an invite?" in html and 'id="invite-redeem"' in html, page
    for name in ("index.html", "checkout.html", "checkout.js", "wallet.js", "connect.html", "check.html"):
        text = (STATIC_DIR / name).read_text().lower()
        assert "free invite" not in text and "trial" not in text, name  # an invite, never "free" or a "trial"
