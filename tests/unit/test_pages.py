"""The /connect and /checkout pages: a strict CSP, their assets load, and the JS sign-in text matches ours."""

import html
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
    assert "Check before" in response.text and "<span>Unfret</span>" in response.text
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert 'href="/extension"' in response.text and 'href="/privacy"' in response.text
    # Both store links filled in, under the hero's link field.
    assert "{{" not in response.text
    assert response.text.count(f'href="{settings.chrome_store_url}"') == 1
    assert response.text.count(f'href="{settings.firefox_store_url}"') == 1
    assert 'href="https://addons.mozilla.org/' in response.text
    # Its script fills in the prices (/v1/pricing) and the store buttons; the footer's keeps an affiliate link's
    # code (ref.js).
    assert response.text.count("<script") == 2
    assert f'<script type="module" src="/static/{asset_tag()}/ref.js"></script>' in response.text
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


async def test_privacy_statement_describes_gift_codes_and_affiliates(client: httpx.AsyncClient) -> None:
    text = (await client.get("/privacy")).text
    assert "<strong>Gift codes</strong>" in text and "nothing links the buyer to the friend who" in text
    assert "<strong>Affiliates and bonus codes</strong>" in text and "no IP address" in text
    assert "No affiliate links to shops" in text


def test_pages_show_invites_and_offer_to_redeem_one() -> None:
    wallet = (STATIC_DIR / "wallet.js").read_text()
    assert "Share them with friends: each one gives a friend 5 free checks" in wallet
    assert "can use one gift code a month" in wallet
    assert "/v1/invites/redeem" in wallet and "copyButton(code)" in wallet  # Copy, as for the pass
    checkout = (STATIC_DIR / "checkout.js").read_text()
    assert '"X-Unfret-Claim"' in checkout and "inviteNodes(" in checkout  # the payment-received panel
    assert "gift code for a friend" in checkout and "gift codes for friends" in checkout  # each pack's offer
    for page in ("checkout", "connect", "check"):
        html = (STATIC_DIR / f"{page}.html").read_text()
        assert "Got a gift code from a friend?" in html and 'id="invite-redeem"' in html, page
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
    footer = page.split('<footer class="site-footer">', 1)[1]
    assert (
        "<p>Check before you pay. Unfret reads" in footer and "Shop without the worry" not in page
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


async def test_the_welcome_page_offers_checks_rather_than_the_extension(client: httpx.AsyncClient) -> None:
    nav = (await client.get("/welcome")).text.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    assert '<a class="cta" href="/checkout">Buy checks</a>' in nav


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
def test_an_old_link_to_the_50_pack_lands_on_the_nearest_pack(tmp_path: Path) -> None:
    module = tmp_path / "prices.mjs"
    module.write_text((STATIC_DIR / "prices.js").read_text())
    packs = [{"checks": 25}, {"checks": 100}]
    script = (
        f"import {{ packFor }} from {json.dumps(module.as_uri())};"
        f"const packs = {json.dumps(packs)};"
        "process.stdout.write(JSON.stringify([50, 25, 100, 60, 62.5, 70, 500, 1, 0, 'x']"
        ".map((n) => packFor(packs, n)).map((f) => f && [f.pack.checks, f.replaced])));"
    )
    out = json.loads(
        subprocess.run(
            ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True
        ).stdout
    )
    assert out == [
        [25, True],  # the 50-pack: 25 is nearer than 100
        [25, False],
        [100, False],
        [25, True],
        [25, True],  # a tie: the smaller pack
        [100, True],
        [100, True],
        [25, True],
        None,  # no size asked: the page's default
        None,
    ]
    checkout = (STATIC_DIR / "checkout.js").read_text()
    assert "packFor(pricing.packs" in checkout and "isn't sold any more" in checkout
    assert 'id="pack-note"' in (STATIC_DIR / "checkout.html").read_text()


def test_the_pages_show_the_free_checks_first_then_the_two_packs() -> None:
    home = (STATIC_DIR / "index.html").read_text()
    prices = home[home.index('id="prices"') : home.index("</ul>", home.index('id="prices"'))]
    assert prices.index('class="free"') < prices.index('data-checks="25"') < prices.index('data-checks="100"')
    assert 'data-checks="50"' not in prices and "checks=50" not in home
    assert "$6.00" in prices and "$20.00" in prices and "$11.50" not in home and "$22.00" not in home
    assert "with 1 gift code for a friend" in prices and "with 4 gift codes for friends" in prices
    assert "deep checks, then quick checks" in prices
    home_js = (STATIC_DIR / "home.js").read_text()
    assert "checks_per_pass" in home_js and "pack.invites" in home_js  # live from /v1/pricing
    checkout = (STATIC_DIR / "checkout.html").read_text()
    assert checkout.index('id="free-first"') < checkout.index('id="packs"')
    money = " ".join((STATIC_DIR / "money.html").read_text().split())
    assert "25 checks for $6, with 1 gift code for a friend" in money
    assert "100 checks for $20, with 4 gift codes for friends (5 checks each)" in money
    assert money.index("5 free deep checks") < money.index("25 checks for $6")
    my_checks = " ".join((STATIC_DIR / "my-checks.html").read_text().split())
    assert "A pass can use one gift code a month" in my_checks


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
    assert tabs == ["Buy checks", "Gift code", "Add to my pass"]
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
    assert 'href="/developers">Developer guide</a>' in home
    assert "/developers" in app.openapi()["info"]["description"]  # /docs points there too


def test_small_words_match_across_checkout() -> None:
    # "Paying from: the Netherlands", as the VAT line says it; invites get the same Copy button as the pass.
    script = (STATIC_DIR / "checkout.js").read_text()
    assert "countryPhrase(payCountry)" in script
    assert 'import { copyButton } from "./pass.js";' in (STATIC_DIR / "wallet.js").read_text()
    checkout = (STATIC_DIR / "checkout.html").read_text()
    assert checkout.count("SOL (a little cheaper for the small pack)") == 2 and "a little less" not in checkout
    assert "small discount" not in checkout  # crypto is cheaper only for the 25-pack (B5)


def test_the_home_page_leads_with_the_seller_then_agents_and_keeps_ads_and_trades_lower() -> None:
    page = (STATIC_DIR / "index.html").read_text()
    hero = page.split('<section class="hero', 1)[1].split("</section>", 1)[0]
    assert "Check before" in hero and "seller" in hero
    order = [page.index(f'aria-labelledby="{name}-heading"') for name in ("dev", "tricks", "price", "beyond")]
    assert order == sorted(order)  # agents right after the hero; ads and tradespeople a secondary section below


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
    # unfret-account, unfret-new-key, unfret-invite-claims, unfret-free-pass, unfret-ref, unfret-developer-pass
    for key in ("Your sign-in", "A new pass", "Claim tokens", "A note", "An affiliate's code", "A date"):
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
    messages = {"unfret-key", "unfret-key-received", "unfret-ref-received", "unfret-session-status"}
    messages |= {"unfret-free-ask", "unfret-free-answer"}
    assert keys - messages == {
        "unfret-account",
        "unfret-new-key",
        "unfret-invite-claims",
        "unfret-session",
        "unfret-free-pass",
        "unfret-ref",
        "unfret-start-check",
        "unfret-developer-pass",
    }


# ── What a tester found on 0.12.0: honest promises, the phone's main button, who handles a card ────────────────


async def test_the_home_page_says_which_shops_need_the_extension_or_app(client: httpx.AsyncClient) -> None:
    hero = (await client.get("/")).text.split('<section class="hero', 1)[1].split("</section>", 1)[0]
    assert "On Amazon and other marketplaces" in hero
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
    home = " ".join((STATIC_DIR / "index.html").read_text().split())
    assert "asks for your email for the receipt" in home  # the same words as the home page


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
    assert [i["status"] for i in card["checklist"]] == ["concern", "not_checked"]
    assert clean_card["clean"] == "Nothing worrying in 14 checks." and clean_card["positivesFirst"]
    # A result from before the card: its reasons as ever, and no checklist, step or same product.
    assert older["reasons"][0]["title"] == "Several small signals"
    assert (older["checklist"], older["nextStep"], older["sameProduct"], older["clean"]) == ([], None, None, None)
