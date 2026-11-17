"""The /connect and /checkout pages: a strict CSP, their assets load, and the JS sign-in text matches ours."""

import json
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from unfret import shopper
from unfret.config import settings
from unfret.credits import siws
from unfret.main import app
from unfret.routes.pages import STATIC_DIR, asset_tag


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_privacy_statement_covers_private_sellers_ads(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/privacy")).text.split())

    assert "Never the seller's name." in text
    assert "Ad fingerprints" in text and "keyed one-way hash" in text
    assert "Kept 90 days after we last saw the ad." in text
    assert "For private sellers" in text


async def test_privacy_statement_names_the_price_observations(client: httpx.AsyncClient) -> None:
    response = await client.get("/privacy")

    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert "90 days" in response.text


def test_pages_are_english_only() -> None:
    # The app writes everything in English: no translated sections, and no page asks for a verdict in a language.
    for page in STATIC_DIR.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert '<html lang="en">' in text and 'lang="nl"' not in text, page.name
    for script in STATIC_DIR.glob("*.js"):
        text = script.read_text(encoding="utf-8")
        assert "Accept-Language" not in text and "navigator.language" not in text, script.name


@pytest.mark.parametrize(
    ("path", "script"), [("/connect", "/static/connect.js"), ("/checkout", "/static/checkout.js")]
)
async def test_pages_are_served_with_a_strict_policy(client: httpx.AsyncClient, path: str, script: str) -> None:
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert f'<script type="module" src="{script.replace("/static/", f"/static/{asset_tag()}/")}">' in response.text
    assert "http://" not in response.text.replace("http://test", "")  # nothing loaded from elsewhere


@pytest.mark.parametrize("asset", ["wallet.js", "connect.js", "checkout.js", "site.css", "home.js"])
async def test_assets_are_served(client: httpx.AsyncClient, asset: str) -> None:
    response = await client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript", "text/css"}
    # Caches (Cloudflare) must check before reusing, or a deploy serves a new page with an old stylesheet or script.
    assert response.headers["cache-control"] == "no-cache"


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
    fields = siws.nonce_response("s", domain="unfret.net", uri="https://unfret.net", ttl_s=300)
    address = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
    module = tmp_path / "wallet.mjs"
    module.write_text((STATIC_DIR / "wallet.js").read_text())
    (tmp_path / "pass.js").write_text((STATIC_DIR / "pass.js").read_text())  # it imports the Copy button
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
    assert "Shop without" in response.text and "<span>Unfret</span>" in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert 'href="/extension"' in response.text and 'href="/privacy"' in response.text
    # Both store links filled in, on the hero and the closing section.
    assert "{{" not in response.text
    assert response.text.count(f'href="{settings.chrome_store_url}"') == 2
    assert response.text.count(f'href="{settings.firefox_store_url}"') == 2
    assert 'href="https://addons.mozilla.org/' in response.text
    # Its only script fills in "Checked by us recently" (/v1/showcase), the prices (/v1/pricing) and the code box.
    assert response.text.count("<script") == 1
    assert f'<script type="module" src="/static/{asset_tag()}/home.js"></script>' in response.text
    assert 'id="spotted"' in response.text


def test_scripts_only_load_images_the_policy_allows() -> None:
    # The pages' CSP allows images from 'self' and data: only; a blob: URL (createObjectURL) would render nothing,
    # as the checkout's QR code once did.
    for script in STATIC_DIR.glob("*.js"):
        assert "createObjectURL" not in script.read_text(encoding="utf-8"), script.name


async def test_privacy_statement_describes_invites(client: httpx.AsyncClient) -> None:
    text = (await client.get("/privacy")).text
    assert "<strong>Invites</strong>" in text and "nothing links the buyer to the person they invited" in text


def test_pages_show_invites_and_offer_to_redeem_one() -> None:
    wallet = (STATIC_DIR / "wallet.js").read_text()
    assert "Give these to people who haven't tried Unfret yet: each one gives them 3 free checks." in wallet
    assert "/v1/invites/redeem" in wallet and "copyButton(code)" in wallet  # Copy, as for the pass
    checkout = (STATIC_DIR / "checkout.js").read_text()
    assert '"X-Unfret-Claim"' in checkout and "inviteNodes(" in checkout  # the payment-received panel
    assert "to give away" in checkout  # each pack's offer
    for page in ("checkout", "connect", "check"):
        html = (STATIC_DIR / f"{page}.html").read_text()
        assert "Have an invite or intro code?" in html and 'id="invite-redeem"' in html, page
    for name in ("index.html", "checkout.html", "checkout.js", "wallet.js", "connect.html", "check.html"):
        text = (STATIC_DIR / name).read_text().lower()
        assert "free invite" not in text and "trial" not in text, name  # an invite, never "free" or a "trial"


async def test_pages_name_their_assets_under_the_build(client: httpx.AsyncClient) -> None:
    # A deploy changes every asset address, so no cache can give a new page an old stylesheet or script.
    tag = asset_tag()
    for path in ("/", "/checkout", "/check", "/connect", "/privacy", "/extension"):
        page = (await client.get(path)).text
        assert f'"/static/{tag}/site.css"' in page, path
        assert '"/static/site.css"' not in page, path
    built = await client.get(f"/static/{tag}/site.css")
    assert built.status_code == 200 and "--bg-glow" in built.text
    assert built.headers["cache-control"] == "public, max-age=31536000, immutable"
    plain = await client.get("/static/site.css")  # an old page's address still works, and is revalidated
    assert plain.status_code == 200 and plain.headers["cache-control"] == "no-cache"
    module = await client.get(f"/static/{tag}/check-render.js")  # what check.js imports relatively
    assert module.status_code == 200


# ── One header and footer, one typeface, drawn icons ──────────────────────────────────────────────────────────

SITE_PAGES = {
    "/": None,
    "/checkout": "/checkout",
    "/check": "/check",
    "/connect": None,  # the wallet sign-in: reached from My checks and the extension, not the menu
    "/my-checks": "/my-checks",
    "/privacy": None,  # in the footer, not the menu
    "/extension": "/extension",
    "/welcome": None,
}


@pytest.mark.parametrize(("path", "current"), SITE_PAGES.items())
async def test_every_page_has_the_shared_header_and_footer(
    client: httpx.AsyncClient, path: str, current: str | None
) -> None:
    page = (await client.get(path)).text
    assert "{{" not in page and "}}" not in page
    assert page.count('<header class="site-header">') == 1 and page.count('<footer class="site-footer">') == 1
    nav = page.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    marked = [line.strip() for line in nav.splitlines() if 'aria-current="page"' in line]
    if current is None:
        assert marked == []
    else:
        assert len(marked) == 1 and f'href="{current}"' in marked[0], marked


def test_no_page_carries_its_own_header_or_footer() -> None:
    # A change to the menu or the footer is made once, in static/partials.
    for page in STATIC_DIR.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        assert "{{header}}" in text and "{{footer}}" in text, page.name
        assert "<header" not in text and "<footer" not in text, page.name


async def test_the_welcome_page_offers_a_code_rather_than_the_extension(client: httpx.AsyncClient) -> None:
    nav = (await client.get("/welcome")).text.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    assert '<a class="cta" href="/checkout?mode=code">I have a code</a>' in nav


async def test_the_typeface_is_served_from_here(client: httpx.AsyncClient) -> None:
    csp = (await client.get("/")).headers["content-security-policy"]
    assert "font-src 'self'" in csp
    css = (STATIC_DIR / "site.css").read_text()
    assert 'url("fonts/figtree-latin-wght-normal.woff2")' in css and "@import" not in css
    font = await client.get(f"/static/{asset_tag()}/fonts/figtree-latin-wght-normal.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
    assert (STATIC_DIR / "fonts" / "OFL-Figtree.txt").exists()  # the licence ships with it


def test_icons_are_drawn_not_typed() -> None:
    # A typed symbol looks different on every phone, and Android may draw ⚠ as a colour emoji.
    for path in [*STATIC_DIR.glob("*.html"), *STATIC_DIR.glob("*.css"), *(STATIC_DIR / "partials").glob("*.html")]:
        text = path.read_text(encoding="utf-8")
        assert 'content: "✓"' not in text and "⚠" not in text, path.name
    css = (STATIC_DIR / "site.css").read_text()
    for band, icon in (("reliable", "check"), ("concerns", "bang"), ("careful", "warn"), ("junk_or_scam", "cross")):
        assert f"--sym: var(--i-{icon})" in css.split(f".{band} .symbol", 1)[1].split("}", 1)[0], band
    assert "::-webkit-details-marker { display: none; }" in css  # fold-outs get a chevron, not the browser's triangle


async def test_the_install_page_offers_the_phone_app(client: httpx.AsyncClient) -> None:
    page = (await client.get("/extension")).text
    assert 'id="phone"' in page and 'href="/check"' in page
    assert 'src="data:image/svg+xml;base64,' in page  # the QR code, an image the policy allows
    assert f">{settings.public_base_url.split('://')[-1]}/check</a>" in page


# ── Plain words for shoppers: a pass, a backup code, My checks ───────────────────────────────────────────────

SHOPPER_PAGES = ("index.html", "check.html", "my-checks.html", "welcome.html", "extension.html", "checkout.html")


@pytest.mark.parametrize("name", SHOPPER_PAGES)
def test_shopper_pages_speak_of_a_pass_not_an_api_key(name: str) -> None:
    text = " ".join((STATIC_DIR / name).read_text(encoding="utf-8").split())
    # The home page's section for developers and agents is meant to talk about x402, USDC and SOL, and one quiet line
    # under the prices says crypto works too.
    text = re.sub(r'<section[^>]*aria-labelledby="dev-heading".*?</section>', "", text)
    text = re.sub(r"<p[^>]*>[^<]*Paying with crypto\?.*?</p>", "", text)
    assert "API key" not in text and "ufk_" not in text and "ufr_" not in text and "credits" not in text, name
    if name != "checkout.html":  # its crypto tab names the coins it takes
        assert "Solana" not in text and "SOL" not in text, name


def test_shopper_scripts_speak_of_a_pass_not_an_api_key() -> None:
    for name in ("check.js", "my-checks.js", "checkout.js", "wallet.js", "home.js"):
        strings = re.findall(r'"[^"\n]*"|`[^`]*`', (STATIC_DIR / name).read_text(encoding="utf-8"))
        shown = [s for s in strings if " " in s]  # words shown to people, not ids or paths
        for phrase in ("API key", "recovery code", "Top up my key", "Use an API key"):
            assert not any(phrase in s for s in shown), (name, phrase)


async def test_my_checks_is_a_static_page_with_a_strict_policy(client: httpx.AsyncClient) -> None:
    response = await client.get("/my-checks")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert f'<script type="module" src="/static/{asset_tag()}/my-checks.js"></script>' in response.text
    for element in ('id="mine-pass"', 'id="mine-none"', 'id="restore"', 'id="pass-phone"', 'id="save-backup"'):
        assert element in response.text
    script = (STATIC_DIR / "my-checks.js").read_text()
    # It keeps what the link checker keeps (the sign-in) and the unsaved backup code from checkout; nothing else.
    assert set(re.findall(r'"(unfret-[a-z-]+)"', script)) == {"unfret-account", "unfret-new-key"}
    assert "/v1/keys/phone-qr" in script and "/v1/keys/recover" in script


def test_the_menu_leads_to_my_checks_not_a_wallet_sign_in() -> None:
    header = (STATIC_DIR / "partials" / "header.html").read_text()
    footer = (STATIC_DIR / "partials" / "footer.html").read_text()
    assert 'href="/my-checks">My checks</a>' in header and 'href="/my-checks">My checks</a>' in footer
    assert "/connect" not in header and "/connect" not in footer and "Sign in" not in header


@pytest.mark.usefixtures("node")
def test_a_pass_id_reads_in_two_groups(tmp_path: Path) -> None:
    module = tmp_path / "pass.mjs"
    module.write_text((STATIC_DIR / "pass.js").read_text())
    key = "ufk_abcdefghij_" + "a" * 52
    script = (
        f"import {{ passIdOf, readPassId, showPassId }} from {json.dumps(module.as_uri())};"
        f"process.stdout.write(JSON.stringify([showPassId('abcdefghij'), readPassId('ABCDE-FGHIJ'),"
        f" readPassId(' abcde fghij '), readPassId({json.dumps(key)}), readPassId('abcde-fghi1'),"
        f" passIdOf({json.dumps(key)}), passIdOf('ufr_abcdefghij_' + 'a'.repeat(52))]));"
    )
    out = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        ).stdout
    )
    assert out == ["ABCDE-FGHIJ", "abcdefghij", "abcdefghij", "abcdefghij", None, "abcdefghij", None]


@pytest.mark.usefixtures("node")
def test_results_never_repeat_the_label_and_follow_the_page_type(tmp_path: Path) -> None:
    for name in ("check-render.js", "check-core.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    render, core = (tmp_path / "check-render.js").as_uri(), (tmp_path / "check-core.js").as_uri()
    script = (
        f"import {{ headlineOf }} from {json.dumps(render)};"
        f"import {{ checklistFor, CHECKLISTS }} from {json.dumps(core)};"
        "process.stdout.write(JSON.stringify({"
        "headlines: [headlineOf('Many warning signs: fake countdown.', 'Many warning signs'),"
        " headlineOf('Looks fine, but one thing to note: a new shop.', 'Looks fine'),"
        " headlineOf('Fake countdown.', 'Many warning signs'),"
        " headlineOf('Many warning signs.', 'Many warning signs')],"
        "kinds: ['storefront', 'aggregate_listing', 'p2p_listing', 'service_site', 'service_listing', undefined]"
        ".map(checklistFor),"
        "labels: Object.values(CHECKLISTS).flat().map((item) => item.label)}));"
    )
    out = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        ).stdout
    )
    assert out["headlines"] == [
        "Fake countdown.",
        "One thing to note: a new shop.",
        "Fake countdown.",
        "Many warning signs.",
    ]
    assert out["kinds"] == ["shop", "shop", "ad", "service", "service", "shop"]
    assert "Page code" not in out["labels"]  # no technical words in the progress list


@pytest.mark.usefixtures("node")
def test_the_four_verdicts_and_a_reason_below_looks_fine(tmp_path: Path) -> None:
    for name in ("check-render.js", "check-core.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    render, core = (tmp_path / "check-render.js").as_uri(), (tmp_path / "check-core.js").as_uri()
    reason = {"title": "Fake countdown", "detail": "", "severity": "high"}
    script = (
        f"import {{ reasonsOf }} from {json.dumps(render)};"
        f"import {{ BAND_WORD }} from {json.dumps(core)};"
        f"const reason = {json.dumps(reason)};"
        "const titles = (result, band) => reasonsOf(result, band).map((r) => r.title);"
        "process.stdout.write(JSON.stringify({"
        "words: BAND_WORD,"
        "middle: ['concerns', 'careful', 'junk_or_scam']"
        ".map((band) => titles({ verdict: { band, reasons: [] } }, band)),"
        "older: titles({ score: 30 }, 'careful'),"
        "kept: titles({ verdict: { band: 'careful', reasons: [reason] } }, 'careful'),"
        "reliable: titles({ verdict: { band: 'reliable', reasons: [] } }, 'reliable'),"
        "unassessed: titles({ score: -1 }, null)}));"
    )
    out = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        ).stdout
    )
    assert out["words"] == shopper.catalogue()["labels"]  # the site's words are the API's labels
    assert list(out["words"].values()) == ["Looks fine", "Worth a look", "Think twice", "Serious concerns"]
    assert out["middle"] == [["Several small signals"]] * 3
    assert out["older"] == ["Several small signals"]
    assert out["kept"] == ["Fake countdown"]
    assert out["reliable"] == [] and out["unassessed"] == []
    assert shopper.catalogue()["reasons"]["overall"]["title"] == "Several small signals"  # the same words


# ── Card and Wero first; crypto one quiet step away ────────────────────────────────────────────────────────────


def test_checkout_has_three_tabs_and_crypto_behind_a_link() -> None:
    page = (STATIC_DIR / "checkout.html").read_text()
    tabs = re.findall(r'role="tab"[^>]*>([^<]+)</button>', page)
    assert tabs == ["Buy checks", "I have a code", "Add to my pass"]
    assert "tab-crypto" not in page and page.count("data-to-crypto") == 2  # under Buy checks and Add to my pass
    assert 'id="crypto-back"' in page
    # The test network's "no real money" concerns crypto only: it sits with the crypto panel, not above the tabs.
    assert page.index('id="network"') > page.index('id="crypto-head"')
    script = (STATIC_DIR / "checkout.js").read_text()
    assert 'for (const way of document.querySelectorAll(".crypto-way")) way.hidden = !cryptoAvailable();' in script


def test_the_home_page_prices_are_card_prices() -> None:
    page = (STATIC_DIR / "index.html").read_text()
    prices = page.split('<ul class="prices" id="prices">', 1)[1].split("</ul>", 1)[0]
    assert "crypto" not in prices  # the price cards show what a shopper pays by card
    assert 'href="/checkout?mode=crypto"' in page  # one quiet line for those who pay with crypto


def test_buying_again_adds_to_the_pass_this_browser_uses() -> None:
    script = (STATIC_DIR / "checkout.js").read_text()
    assert "const existing = freshPass ? null : currentPassId();" in script
    assert 'id="new-for"' in (STATIC_DIR / "checkout.html").read_text()


# ── Developers and AI agents: easy to find, in the site's own look ─────────────────────────────────────────────


async def test_the_developer_page_prices_come_from_the_settings(client: httpx.AsyncClient) -> None:
    from unfret.prices import format_usd  # noqa: PLC0415 - only here

    response = await client.get("/developers")
    assert response.status_code == 200
    assert "default-src 'none'" in response.headers["content-security-policy"]
    page = response.text
    assert "{{" not in page
    assert format_usd(settings.x402_price_quick_usd_micros) in page
    assert format_usd(settings.x402_price_deep_usd_micros) in page
    assert f"{settings.public_base_url.rstrip('/')}/v1/check" in page
    for link in ('href="/llms.txt"', 'href="/.well-known/x402"', 'href="/docs"', 'href="/openapi.json"'):
        assert link in page, link
    assert "uvx" in page and "unfret-mcp" in page and "UNFRET_MCP_KEYPAIR" in page
    assert "x402 on Base" in page  # the roadmap, as promised on the page


async def test_developers_are_one_click_away_from_every_page(client: httpx.AsyncClient) -> None:
    header = (STATIC_DIR / "partials" / "header.html").read_text()
    footer = (STATIC_DIR / "partials" / "footer.html").read_text()
    assert 'href="/developers">Developers</a>' in header
    assert 'href="/developers">Developers and AI agents</a>' in footer
    assert 'href="/llms.txt"' not in footer  # humans get a page, machines get llms.txt from it
    home = (await client.get("/")).text
    assert 'href="/developers">Developer guide</a>' in home
    assert "/developers" in app.openapi()["info"]["description"]  # /docs points there too


def test_small_words_match_across_checkout() -> None:
    # "Paying from: the Netherlands", as the VAT line says it; invites get the same Copy button as the pass.
    script = (STATIC_DIR / "checkout.js").read_text()
    assert "countryPhrase(payCountry)" in script
    assert 'import { copyButton } from "./pass.js";' in (STATIC_DIR / "wallet.js").read_text()
    checkout = (STATIC_DIR / "checkout.html").read_text()
    assert checkout.count("SOL for a small discount") == 2 and "a little less" not in checkout


def test_the_home_page_leads_with_the_seller_then_agents_and_keeps_ads_and_trades_lower() -> None:
    page = (STATIC_DIR / "index.html").read_text()
    hero = page.split('<section class="hero">', 1)[1].split("</section>", 1)[0]
    assert "Check the seller" in hero and 'href="/developers"' in hero  # agents get a way in right at the top
    order = [page.index(f'aria-labelledby="{name}-heading"') for name in ("how", "looks", "dev", "price", "beyond")]
    assert order == sorted(order)  # ads and tradespeople: a secondary section below pricing
