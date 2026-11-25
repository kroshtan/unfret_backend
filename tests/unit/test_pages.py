"""The /connect and /checkout pages: a strict CSP, their assets load, and the JS sign-in text matches ours."""

import html
import json
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

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
    # The one exception is the Dutch landing page (/nl), which links to the English pages for everything else.
    for page in STATIC_DIR.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        if page.name == "nl.html":
            assert '<html lang="nl">' in text, page.name
            continue
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
# A reason's links to archived copies of a shop's address (domain_history): links the user may open, never requests.
ARCHIVE_LINKS = ('"https://web.archive.org/web/"', '"https://index.commoncrawl.org/CC-MAIN-"')


# The link checker completes a typed bare domain ("shop.example/item") to a link; it is checked, never fetched here.
BARE_DOMAIN_PREFIX = "`https://${value}`"


def test_scripts_make_no_third_party_requests() -> None:
    for script in STATIC_DIR.glob("*.js"):
        text = script.read_text().replace(BARE_DOMAIN_PREFIX, "")
        for archive in ARCHIVE_LINKS:
            text = text.replace(archive, '""')
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
    assert "Connect your wallet to the device showing" in html
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
    assert "before you pay" in response.text and "<span>Unfret</span>" in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert 'href="/extension"' in response.text and 'href="/privacy"' in response.text
    # Both store links filled in, under the hero's link field.
    assert "{{" not in response.text
    assert response.text.count(f'href="{settings.chrome_store_url}"') == 1
    assert response.text.count(f'href="{settings.firefox_store_url}"') == 1
    assert 'href="https://addons.mozilla.org/' in response.text
    # Its script fills in the leaderboard and the store buttons.
    assert response.text.count("<script") == 1
    assert f'<script type="module" src="/static/{asset_tag()}/home.js"></script>' in response.text


async def test_home_page_shows_the_tricks_as_cards_without_real_shops(client: httpx.AsyncClient) -> None:
    text = (await client.get("/")).text
    tricks = text[text.index('id="tricks"') : text.index("</section>", text.index('id="tricks"'))]
    titles = re.findall(r"<h3>(.*?)</h3>", tricks)
    assert len(titles) == 8
    for trick in ("countdown", "closing-down", "“was” price", "far away", "Reviews", "made-up brand"):
        assert any(trick in title for title in titles), trick
    assert "<img" not in tricks and "https://" not in tricks  # made-up examples only: no shop, link or screenshot
    assert "spotted" not in text.lower() and "/v1/showcase" not in text


def test_scripts_only_load_images_the_policy_allows() -> None:
    # The pages' CSP allows images from 'self' and data: only; a blob: URL (createObjectURL) would render nothing,
    # as the checkout's QR code once did.
    for script in STATIC_DIR.glob("*.js"):
        assert "createObjectURL" not in script.read_text(encoding="utf-8"), script.name


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
    "/checkout": None,  # developer passes and old store clients: not in the menu
    "/check": "/check",
    "/connect": None,  # the wallet sign-in: reached from My checks and the extension, not the menu
    "/my-checks": None,  # developer passes: in the footer, not the menu
    "/privacy": None,  # in the footer, not the menu
    "/extension": "/extension",
    "/welcome": None,
    "/developers/http": None,  # under /developers, which the menu links to
}


@pytest.mark.parametrize(("path", "current"), SITE_PAGES.items())
async def test_every_page_has_the_shared_header_and_footer(
    client: httpx.AsyncClient, path: str, current: str | None
) -> None:
    page = (await client.get(path)).text
    assert "{{" not in page and "}}" not in page
    assert page.count('<header class="site-header">') == 1 and page.count('<footer class="site-footer">') == 1
    footer = page.split('<footer class="site-footer">', 1)[1]
    assert (
        "<p>Free consumer protection for online shoppers. Unfret reads" in footer
        and "Shop without the worry" not in page
    )  # the home's line
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


async def test_the_welcome_page_offers_a_check_rather_than_the_extension(client: httpx.AsyncClient) -> None:
    nav = (await client.get("/welcome")).text.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    assert '<a class="cta" href="/check">Check a link</a>' in nav  # checks are free: nothing to buy


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


async def test_the_install_page_offers_check_on_a_phone(client: httpx.AsyncClient) -> None:
    page = (await client.get("/extension")).text
    assert 'id="phone"' in page and 'href="/check"' in page
    assert 'src="data:image/svg+xml;base64,' in page  # the QR code, an image the policy allows
    host = settings.public_base_url.split("://")[-1]
    assert f'Check a link on your phone: open <a href="/check">{host}/check</a>' in " ".join(page.split())
    # On a phone Unfret is /check in the browser, not an app to install from it.
    assert "Nothing to install." in page and "as an app" not in page and "Add to Home" not in page
    assert 'href="/android"' not in page  # /android stays unlisted for now


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


def test_the_menu_is_for_shoppers_and_the_developer_pass_is_in_the_footer() -> None:
    header = (STATIC_DIR / "partials" / "header.html").read_text()
    footer = (STATIC_DIR / "partials" / "footer.html").read_text()
    assert 'href="/support">Support Unfret</a>' in header and "/checkout" not in header and "/my-checks" not in header
    assert 'href="/my-checks">Developer pass</a>' in footer
    assert "/connect" not in header and "/connect" not in footer and "Sign in" not in header


def test_an_old_link_with_checks_fills_in_their_amount() -> None:
    checkout = (STATIC_DIR / "checkout.js").read_text()
    assert 'params.get("amount")' in checkout and 'params.get("checks")' in checkout
    assert 'id="topup-amount"' in (STATIC_DIR / "checkout.html").read_text()
    assert "pricing.packs" not in checkout


def test_the_pages_say_shoppers_check_free_and_developers_top_up() -> None:
    home = (STATIC_DIR / "index.html").read_text()
    assert 'id="prices"' not in home and "gift code" not in home and "$6.50" not in home
    checkout = (STATIC_DIR / "checkout.html").read_text()
    assert 'id="topup-amount"' in checkout and 'id="topup-line"' in checkout and 'id="packs"' not in checkout
    money = " ".join((STATIC_DIR / "money.html").read_text().split())
    assert "gift code" not in money and "$6.50" not in money and "top-ups of any amount from $10" in money
    assert money.index("Donations") < money.index("Checks for developers")
    my_checks = " ".join((STATIC_DIR / "my-checks.html").read_text().split())
    assert "gift code" not in my_checks


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
    assert "Coming next" not in page and "coming next" not in page  # every way in is here, nothing announced


async def test_the_developer_page_opens_with_three_ways_to_integrate(client: httpx.AsyncClient) -> None:
    page = (await client.get("/developers")).text
    ways = page.split('id="ways"', 1)[1].split("</section>", 1)[0]
    text = html.unescape(re.sub(r"<[^>]+>", "", ways))
    assert re.findall(r"\d\. [^<\n]+", text)[:3] == ["1. MCP server", "2. API with a pass", "3. x402 pay-per-call"]
    base = settings.public_base_url.rstrip("/")
    assert f"claude mcp add --transport http unfret {base}/mcp" in text  # one working example each
    assert f'curl {base}/v1/check -H "Authorization: Bearer $UNFRET_PASS"' in text
    assert "402" in text and '-H "PAYMENT-SIGNATURE: $PAYMENT"' in text
    assert page.index('id="ways"') < page.index('id="developer-pass"') < page.index('id="mcp"')  # the overview first


async def test_the_oauth_sign_in_shows_only_while_it_is_on(client: httpx.AsyncClient) -> None:
    from unfret.routes.pages import with_oauth  # noqa: PLC0415 - only here

    off = (await client.get("/developers")).text  # mcp_oauth_enabled is off by default
    assert "oauth-on" not in off and 'id="mcp-oauth"' not in off and "or by signing in" not in off
    on = with_oauth((STATIC_DIR / "developers.html").read_text(), True)
    assert "oauth-on" not in on and 'id="mcp-oauth"' in on and "or by signing in" in on
    assert "call <code>get_pricing</code>; a check is what asks you to sign in" in " ".join(on.split())


async def test_the_claude_page_exists_only_while_oauth_is_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/developers/claude")).status_code == 404  # mcp_oauth_enabled is off by default
    monkeypatch.setattr(settings, "mcp_oauth_enabled", True)
    response = await client.get("/developers/claude")
    assert response.status_code == 200
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", response.text)).split())
    base = settings.public_base_url.rstrip("/")
    assert f"claude mcp add --transport http unfret {base}/mcp" in text and "{{" not in response.text
    assert f"up to {settings.developer_free_checks} free deep checks" in text
    for tool in ("check_store_quick", "check_store_deep", "get_checks_left", "get_pricing"):
        assert tool in text, tool
    for section in ("examples", "troubleshooting", "support"):
        assert f'id="{section}"' in response.text, section
    assert "hello@unfret.net" in text and 'href="/privacy"' in response.text and 'href="/terms"' in response.text
    from unfret.routes.pages import with_oauth  # noqa: PLC0415 - only here

    assert 'href="/developers/claude"' in with_oauth((STATIC_DIR / "developers.html").read_text(), True)


async def test_developer_page_marks_balance_and_pauses_as_the_code_answers(client: httpx.AsyncClient) -> None:
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", (await client.get("/developers")).text)).split())
    assert "402 INSUFFICIENT_CREDITS until it resets" in text and "x402 go on" in text


async def test_the_developer_page_has_calls_a_real_response_speed_and_limits(client: httpx.AsyncClient) -> None:
    from unfret.routes.pages import DEVELOPERS_MEASURED  # noqa: PLC0415 - only here

    page = html.unescape(re.sub(r"<[^>]+>", "", (await client.get("/developers")).text))  # the text, no markup
    base = settings.public_base_url.rstrip("/")
    assert f'curl {base}/v1/check -H "Authorization: Bearer $UNFRET_PASS"' in page  # with a pass
    assert '-H "PAYMENT-SIGNATURE: $PAYMENT"' in page  # with x402
    measured = json.loads(DEVELOPERS_MEASURED.read_text())
    assert measured["deep"]["runs"] >= 3 and measured["quick"]["runs"] >= 3  # measured, not made up
    assert json.dumps(measured["sample"], indent=2, ensure_ascii=False) in page  # the whole sample response
    assert f"{measured['deep']['median_s']:.1f} s" in page and measured["server"] in page
    for limit in (settings.x402_check_per_minute, settings.quick_checks_per_minute, settings.developer_free_checks):
        assert f"{limit}" in page
    assert f"{base}/mcp" in page and "UNFRET_PASS" in page and "uvx" in page  # remote and local MCP
    assert "Make a developer pass" in page  # a developer pass, from the page


async def test_developers_are_one_click_away_from_every_page(client: httpx.AsyncClient) -> None:
    header = (STATIC_DIR / "partials" / "header.html").read_text()
    footer = (STATIC_DIR / "partials" / "footer.html").read_text()
    assert 'href="/developers">Developers</a>' in header
    assert 'href="/developers">Developers and AI agents</a>' in footer
    assert 'href="/llms.txt"' not in footer  # humans get a page, machines get llms.txt from it
    home = (await client.get("/")).text
    assert 'href="/developers">developer page</a>' in home
    assert "/developers" in app.openapi()["info"]["description"]  # /docs points there too


def test_small_words_match_across_checkout() -> None:
    # "Paying from: the Netherlands", as the VAT line says it; invites get the same Copy button as the pass.
    script = (STATIC_DIR / "checkout.js").read_text()
    assert "countryPhrase(payCountry)" in script
    assert 'import { copyButton } from "./pass.js";' in (STATIC_DIR / "wallet.js").read_text()
    checkout = (STATIC_DIR / "checkout.html").read_text()
    assert checkout.count("SOL (a little cheaper for the small pack)") == 0 and "a little less" not in checkout
    assert "small discount" not in checkout  # crypto costs the same as a card: the amount plus VAT


def test_the_home_page_leads_with_protection_and_keeps_ads_and_trades_lower() -> None:
    page = (STATIC_DIR / "index.html").read_text()
    hero = page.split('<section class="hero', 1)[1].split("</section>", 1)[0]
    assert "Consumer protection, free for every shopper" in hero and "risk level" in " ".join(hero.split())
    names = ("questions", "levels", "tricks", "together", "beyond")
    order = [page.index(f'aria-labelledby="{name}-heading"') for name in names]
    assert order == sorted(order)  # the shopper's questions first; ads and tradespeople a secondary section below


@pytest.mark.parametrize("path", ["/check", "/checkout", "/connect", "/my-checks", "/"])
async def test_pages_cannot_be_steered_by_the_window_that_opened_them(client: httpx.AsyncClient, path: str) -> None:
    # Another site that opens one of our pages must not keep a handle on it (navigating it on to a page that hands a
    # pass to the extension, say); popups our pages open themselves (wallets) keep theirs.
    response = await client.get(path)

    assert response.headers["cross-origin-opener-policy"] == "same-origin-allow-popups"


@pytest.mark.parametrize("path", ["/v1/showcase", "/v1/showcase/1", "/showcase/1", "/showcase/1/screenshot"])
async def test_the_showcase_is_retired(client: httpx.AsyncClient, path: str) -> None:
    assert (await client.get(path)).status_code == 404


async def test_no_page_mentions_the_retired_showcase(client: httpx.AsyncClient) -> None:
    for path in ("/", "/check", "/privacy"):
        text = (await client.get(path)).text.lower()
        assert "spotted recently" not in text and "showcase" not in text, path


async def test_privacy_statement_covers_storage_logs_bases_and_rights(client: httpx.AsyncClient) -> None:
    text = " ".join((await client.get("/privacy")).text.split())
    assert "sets <strong>no cookies</strong>" in text
    # unfret-account, unfret-new-key, unfret-invite-claims, unfret-free-pass, unfret-developer-pass
    for key in ("Your sign-in", "A new pass", "Claim tokens", "A note", "A date", "A contributor key"):
        assert f"<strong>{key}</strong>" in text
    assert "no request log" in text and "proxy log" not in text  # kamal-proxy runs with Docker's `none` log driver
    assert "salted one-way hash, in memory only" in text
    assert "Why we may use it (legal basis)" in text and "Because the law requires it" in text
    assert "Autoriteit Persoonsgegevens" in text and "mailto:hello@unfret.net" in text
    assert "Polar Software Inc., United States" in text and "Cloudflare, Inc. is based in the United States" in text
    main = text.split("<main>", 1)[1].split("</main>", 1)[0]
    assert "kroshtan.com" not in main  # one contact address: hello@unfret.net (the footer links the maker)


def test_the_privacy_statement_names_every_key_the_site_keeps_in_the_browser() -> None:
    keys = set()
    for script in STATIC_DIR.glob("*.js"):
        keys |= set(re.findall(r'"(unfret-[a-z-]+)"', script.read_text()))
    # Message types between the page and the extension, not storage.
    messages = {"unfret-key", "unfret-key-received", "unfret-session-status"}
    messages |= {"unfret-free-ask", "unfret-free-answer"}
    assert keys - messages == {
        "unfret-account",
        "unfret-new-key",
        "unfret-invite-claims",
        "unfret-session",
        "unfret-free-pass",
        "unfret-start-check",
        "unfret-developer-pass",
        "unfret-contributor-key",
    }


# ── What a tester found on 0.12.0: honest promises, the phone's main button, who handles a card ────────────────


async def test_the_home_page_says_which_shops_need_the_extension_or_app(client: httpx.AsyncClient) -> None:
    hero = (await client.get("/")).text.split('<section class="hero', 1)[1].split("</section>", 1)[0]
    note = " ".join(hero.split("turn automatic checks away", 1)[1].split("</p>", 1)[0].split())
    assert "browser extension and the Android app still read them" in note
    assert 'href="/check"' in note  # where the list is


def _button_plans(tmp_path: Path, cases: list[tuple[str, bool]]) -> list[dict[str, object]]:
    module = tmp_path / "install-button.mjs"
    module.write_text((STATIC_DIR / "install-button.js").read_text())
    calls = ",".join(
        f"m.buttonPlan(m.browserChoice({json.dumps(agent)}, false), {json.dumps(small)})" for agent, small in cases
    )
    script = f"import * as m from {json.dumps(module.as_uri())}; process.stdout.write(JSON.stringify([{calls}]));"
    run = subprocess.run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    plans: list[dict[str, object]] = json.loads(run.stdout)
    return plans


@pytest.mark.usefixtures("node")
def test_on_a_phone_or_narrow_screen_check_a_link_is_the_main_button(tmp_path: Path) -> None:
    android = "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36"
    chrome = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    firefox = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"

    phone, narrow, narrow_firefox, desktop, desktop_firefox = _button_plans(
        tmp_path, [(android, False), (chrome, True), (firefox, True), (chrome, False), (firefox, False)]
    )

    assert phone == {"linkFirst": True, "store": "chrome", "mainText": "Add to Chrome", "linkText": "Check a link",
                     "also": "computer"}  # fmt: skip
    assert narrow["linkFirst"] and narrow["linkText"] == "Check a link" and narrow["mainText"] == "Add to Chrome"
    assert narrow_firefox["linkFirst"] and narrow_firefox["mainText"] == "Add to Firefox"
    assert not desktop["linkFirst"] and desktop["linkText"] == "Check a link instead"
    assert desktop_firefox["store"] == "firefox" and desktop_firefox["also"] == "chrome"
    script = (STATIC_DIR / "install-button.js").read_text()
    assert '"(max-width: 40rem)", "(pointer: coarse)"' in script  # narrow or touch, the same rule on every page


def test_the_card_buttons_say_who_handles_the_payment_and_why_the_email() -> None:
    page = (STATIC_DIR / "checkout.html").read_text()
    line = "Card payments are handled by Polar for Kroshtan, Unfret's maker; they ask for your email for the receipt."
    notes = [" ".join(note.split()) for note in re.findall(r'<p class="pay-note">(.*?)</p>', page, re.DOTALL)]
    assert notes == [line, line]  # under Buy checks' and Add to my pass's card button
    for panel in ("new", "topup"):
        assert page.index(f'id="{panel}-pay"') < page.index('class="pay-note"', page.index(f'id="{panel}-pay"'))


@pytest.mark.usefixtures("node")
def test_the_result_card_puts_the_same_product_first_and_says_what_was_checked(tmp_path: Path) -> None:
    for name in ("check-render.js", "check-core.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    render = (tmp_path / "check-render.js").as_uri()
    origin = "https://unfret.net"
    same = {
        "title": "Looks like the same product, sold elsewhere for $12.99",
        "detail": "The main photo is the same photo as on cheap.shop.",
        "where": "cheap.shop",
        "image": f"{origin}/v1/images/1/thumbnail.jpg",
        "own_image": "https://cheap.shop/photo.jpg",  # never loaded from anywhere but this server
    }
    verdict = {
        "band": "careful",
        "reasons": [
            {"type": "photo_reused", "title": "Same photo on other shops", "detail": "…", "severity": "medium",
             "thumbnails": [f"{origin}/v1/images/1/thumbnail.jpg"], "source": "Unfret's image index"},
        ],
        "positives": [],
        "same_product": same,
        "next_step": {"type": "compare_elsewhere", "text": "Compare."},
        "checklist": [{"key": "who", "label": "Who's behind the shop", "status": "concern", "note": "New"},
                      {"key": "reviews", "label": "Reviews", "status": "not_checked", "note": "No reviews."}],
        "checks_run": 9,
        "incomplete": {"steps": ["content"], "text": "Part of this check didn't finish."},
    }  # fmt: skip
    clean = {"band": "reliable", "reasons": [], "positives": [{"title": "Established shop"}], "checks_run": 14}
    script = (
        f"import {{ cardOf }} from {json.dumps(render)};"
        f"const origin = {json.dumps(origin)};"
        "process.stdout.write(JSON.stringify(["
        f"cardOf({{ verdict: {json.dumps(verdict)} }}, 'careful', origin),"
        f"cardOf({{ verdict: {json.dumps(clean)} }}, 'reliable', origin),"
        "cardOf({ score: 30, verdict: { band: 'careful', reasons: [] } }, 'careful', origin)]));"
    )
    card, clean_card, older = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        ).stdout
    )
    assert card["sameProduct"] == {
        "title": same["title"], "detail": same["detail"], "where": "cheap.shop", "image": same["image"],
    }  # fmt: skip
    assert card["reasons"][0]["thumbs"] == [] and card["reasons"][0]["source"] == "Unfret's image index"
    assert card["nextStep"] == "Compare." and card["checks"] == 9
    assert card["incomplete"] == "Part of this check didn't finish." and clean_card["incomplete"] is None
    assert [i["status"] for i in card["checklist"]] == ["concern", "not_checked"]
    assert clean_card["clean"] == "Nothing worrying in 14 checks." and clean_card["positivesFirst"]
    # A result from before the card: its reasons as ever, and no checklist, step or same product.
    assert older["reasons"][0]["title"] == "Nothing clear-cut"
    assert (older["checklist"], older["nextStep"], older["sameProduct"], older["clean"], older["incomplete"]) == (
        [], None, None, None, None,
    )  # fmt: skip


@pytest.mark.usefixtures("node")
def test_the_checklist_heading_adds_up_and_the_charge_line_is_a_sentence(tmp_path: Path) -> None:
    for name in ("check-render.js", "check-core.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    render = (tmp_path / "check-render.js").as_uri()
    items = [
        {
            "key": "who",
            "label": "Who's behind the shop",
            "status": "concern",
            "note": "Brand-new web shop",
            "checks": 4,
        },
        {"key": "reviews", "label": "Reviews", "status": "checked", "note": "", "checks": 1},
        {"key": "prices", "label": "Prices", "status": "not_checked", "note": "No prices.", "checks": 0},
    ]
    verdict = {"band": "careful", "reasons": [], "checklist": items, "checks_run": 5}
    older = {"band": "careful", "reasons": [], "checklist": [{**i, "checks": None} for i in items], "checks_run": 5}
    charges = [
        {"tier": "deep", "score": 40, "charged": True, "free_check": True,
         "charge_note": "This check used one of your free checks; 4 are left."},
        {"tier": "deep", "score": 40, "charged": True},
        {"tier": "deep", "score": 40, "charged": True, "free_check": True},
        {"tier": "quick", "score": 40, "charged": False},
        {"tier": "deep", "score": 40, "charged": False},
        {"tier": "deep", "score": -1, "charged": False},
    ]  # fmt: skip
    script = (
        f"import {{ cardOf, chargeNoteOf }} from {json.dumps(render)};"
        "process.stdout.write(JSON.stringify(["
        f"cardOf({{ verdict: {json.dumps(verdict)} }}, 'careful', 'https://unfret.net'),"
        f"cardOf({{ verdict: {json.dumps(older)} }}, 'careful', 'https://unfret.net'),"
        f"{json.dumps(charges)}.map(chargeNoteOf)]));"
    )
    card, older_card, notes = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        ).stdout
    )
    assert card["checklistHeading"] == {"title": "What we checked: 5 checks in 2 areas", "count": ""}
    assert sum(i["checks"] for i in card["checklist"]) == card["checks"]
    assert [i["checks"] for i in card["checklist"]] == [4, 1, 0]
    # An older result without per-area counts keeps the old heading.
    assert older_card["checklistHeading"] == {"title": "What we checked", "count": " · 5 checks"}
    assert notes == [
        "This check used one of your free checks; 4 are left.",  # the server's, with the count
        "This check used one of your checks.",
        "This check used one of your free checks.",
        "Quick checks are free.",
        "You checked this page recently, so no new check was used.",
        "This page couldn't be assessed, so no check was used.",
    ]


async def test_the_developer_page_says_the_terms_and_who_may_get_in_touch(client: httpx.AsyncClient) -> None:
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", (await client.get("/developers")).text)).split())
    assert f"Developer pass: {settings.developer_free_checks} free deep checks, then packs of checks." in text
    assert "x402:" in text and "Solana" in text
    assert "Organizations that protect consumers (consumer groups, regulators, researchers) are welcome" in text
    assert "risk is the overall risk level, higher is worse" in text


async def test_the_old_pass_pages_speak_to_developers(client: httpx.AsyncClient) -> None:
    for path, line in (
        ("/checkout", "Buy checks for a developer pass"),
        ("/my-checks", "Your developer pass"),
        ("/connect", "shoppers don't need one, checks are free"),
    ):
        text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", (await client.get(path)).text)).split())
        assert line in text, path
        assert "gift code" not in text.lower() and "invite" not in text.lower(), path
