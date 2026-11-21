"""The link checker (/check): its page (a web page, not an installable app), the old app's worker and the fetch."""

import json
import logging
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.conftest import page_fetcher
from unfret import page_fetch
from unfret.main import app
from unfret.net import FetchedPage, RefusedError, UnresolvableHostError, UnsafeURLError
from unfret.routes.pages import BROWSER_ONLY_SHOPS, STATIC_DIR, asset_tag


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
    assert "frame-ancestors 'none'" in csp
    # Not an installable web app: no manifest or service worker allowed.
    assert "manifest-src" not in csp and "worker-src" not in csp
    assert response.headers["referrer-policy"] == "no-referrer"
    assert f'<script type="module" src="/static/{asset_tag()}/check.js">' in response.text
    # Nothing from elsewhere: the only outside addresses are the shared footer's links (the source code, the maker).
    page = response.text.replace("https://shop.example", "")
    sources = [
        f'href="https://{host}/kroshtan/{repo}"'
        for host in ("git.kroshtan.com", "github.com")
        for repo in ("unfret_backend", "unfret_extension", "unfret_android")
    ]
    for footer_link in (*sources, 'href="https://kroshtan.com"'):
        page = page.replace(footer_link, "")
    assert "http://" not in page and "https://" not in page


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


# Pages a visitor sees, none of which may offer /check (or anything) as an app to install.
SITE_PAGES = ("/", "/nl", "/check", "/extension", "/my-checks", "/checkout", "/welcome", "/about", "/privacy")


@pytest.mark.parametrize("path", SITE_PAGES)
async def test_no_page_is_an_installable_app(client: httpx.AsyncClient, path: str) -> None:
    page = (await client.get(path)).text

    assert 'rel="manifest"' not in page and "webmanifest" not in page
    assert "apple-mobile-web-app" not in page and "apple-touch-icon" not in page
    assert "Add to Home Screen" not in page and "Add to Home screen" not in page and "home screen" not in page
    assert 'id="install-app"' not in page and "Install Unfret" not in page


async def test_there_is_no_manifest(client: httpx.AsyncClient) -> None:
    assert (await client.get("/manifest.webmanifest")).status_code == 404
    assert not (STATIC_DIR / "manifest.webmanifest").exists()


def test_the_page_offers_no_install_and_registers_no_worker() -> None:
    script = (STATIC_DIR / "check.js").read_text()

    assert "beforeinstallprompt" not in script and "appinstalled" not in script and ".prompt()" not in script
    assert "display-mode" not in script and "navigator.standalone" not in script
    assert "serviceWorker.register" not in script
    # A browser that still has the old app's worker drops it.
    assert "serviceWorker" in script and "getRegistrations()" in script and "registration.unregister()" in script
    assert "display-mode" not in (STATIC_DIR / "site.css").read_text()


async def test_the_old_apps_worker_removes_itself(client: httpx.AsyncClient) -> None:
    # Installed copies keep their worker and ask this address for a new version: it must still answer, with a worker
    # that takes over at once, clears any caches, unregisters itself and handles no request.
    response = await client.get("/check-sw.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["cache-control"] == "no-cache"
    script = response.text
    assert "self.skipWaiting()" in script
    assert "caches.keys()" in script and "caches.delete(" in script
    assert "self.registration.unregister()" in script
    assert '"fetch"' not in script and "respondWith" not in script
    assert "indexedDB" not in script and "localStorage" not in script


@pytest.mark.parametrize("asset", ["check.js", "check-core.js"])
async def test_the_scripts_are_served(client: httpx.AsyncClient, asset: str) -> None:
    response = await client.get(f"/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] in {"text/javascript", "application/javascript"}


def test_the_page_keeps_only_the_sign_in() -> None:
    script = (STATIC_DIR / "check.js").read_text()
    # Three localStorage keys, every access wrapped in try/catch (private modes throw): the account (get, set,
    # remove), that this browser had its free pass (get, set; no link to the pass or anything checked), and a pass
    # made here with its backup code, as /checkout keeps one (get, set; get and remove once that pass has ended).
    assert script.count("localStorage.") == 3 + 2 + 2 + 2
    assert script.count('"unfret-account"') == 1
    assert script.count('"unfret-free-pass"') == 1 and script.count('"unfret-new-key"') == 1
    assert "sessionStorage" not in script and "indexedDB" not in script
    # The clipboard is read only from the "Paste link" button's handler.
    assert script.count("clipboard.readText()") == 1
    assert 'pasteButton.addEventListener("click", () => void paste())' in script


async def test_home_and_extension_pages_link_to_the_checker(client: httpx.AsyncClient) -> None:
    assert 'href="/check"' in (await client.get("/")).text
    assert 'href="/check"' in (STATIC_DIR / "extension.html").read_text()


@pytest.mark.usefixtures("node")
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


def _refusal(code: int, body: bytes = b"") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://shop.test/")
    return RefusedError(httpx.Response(code, request=request), body)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_refusal(403), "PAGE_BLOCKED"),
        (_refusal(999), "PAGE_BLOCKED"),
        (_refusal(404), "PAGE_UNAVAILABLE"),
        (_refusal(500), "PAGE_UNAVAILABLE"),
        (_refusal(400), "PAGE_UNAVAILABLE"),
        (_refusal(400, b"<script>window.KPSDK={};</script><script src='/149e/2d20/ips.js'></script>"), "PAGE_BLOCKED"),
        (_refusal(404, b"<H1>Access Denied</H1> Reference https://errors.edgesuite.net/18.1"), "PAGE_BLOCKED"),
        (httpx.ReadTimeout("x"), "PAGE_BLOCKED"),
        (httpx.ConnectTimeout("x"), "PAGE_UNAVAILABLE"),
        (httpx.ConnectError("x"), "PAGE_UNAVAILABLE"),
        (ValueError("too big"), "PAGE_UNAVAILABLE"),
        (UnsafeURLError("private"), "INVALID_URL"),
        (UnresolvableHostError("dead"), "PAGE_UNAVAILABLE"),
    ],
)
async def test_fetch_failures_are_named(monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str) -> None:
    async def refuse(url: str, **_: Any) -> bytes:
        raise error

    monkeypatch.setattr(page_fetch, "fetch_public_page", page_fetcher(refuse))
    with pytest.raises(page_fetch.PageUnreadableError) as raised:
        await page_fetch.fetch_page("https://shop.test/p")
    assert raised.value.reason == reason
    assert "shop.test" not in raised.value.detail


async def test_a_shop_that_keeps_the_fetch_waiting_is_given_up_on_in_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def hang(url: str, **kwargs: Any) -> bytes:
        seen.update(kwargs)
        raise httpx.ReadTimeout("no answer")

    monkeypatch.setattr(page_fetch, "fetch_public_page", page_fetcher(hang))
    with pytest.raises(page_fetch.PageUnreadableError) as raised:
        await page_fetch.fetch_page("https://shop.test/p")

    assert seen["timeout"] <= 6 and seen["total_timeout"] <= 12  # was 10 s a read and 25 s in all
    assert raised.value.reason == "PAGE_BLOCKED"
    assert raised.value.detail == "The shop didn't answer an automatic check of this page."


@pytest.mark.parametrize(
    "html",
    [
        "<html><head><title>Pardon Our Interruption</title></head><body>As you were browsing…</body></html>",
        "<html><body>Request unsuccessful. Incapsula incident ID: 123-456</body></html>",
        "<html><body><div id='sec-if-cpt-container'><script src='/_sec/cp_challenge/sec-4-4.js'></script></div>",
    ],
)
def test_more_bot_walls_are_recognised(html: str) -> None:
    assert page_fetch.is_challenge_page(html)


def test_a_kasada_script_on_a_page_that_loaded_is_not_a_bot_wall() -> None:
    # Only in a refusal's body: a shop's real page may carry the same script.
    assert not page_fetch.is_challenge_page("<html><script>window.KPSDK={}</script><p>Wool sweater</p></html>")


async def test_a_dead_domain_couldnt_be_reached_and_a_private_address_isnt_public() -> None:
    with pytest.raises(page_fetch.PageUnreadableError) as dead:
        await page_fetch.fetch_page("https://no-such-host.invalid/p")
    with pytest.raises(page_fetch.PageUnreadableError) as private:
        await page_fetch.fetch_page("http://127.0.0.1/admin")

    assert (dead.value.reason, private.value.reason) == ("PAGE_UNAVAILABLE", "INVALID_URL")
    assert "couldn't be reached" in dead.value.detail and "public" not in dead.value.detail
    assert private.value.detail == "That isn't a public web address."


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
    # unfret.main configured logging (unfret.log.configure) on import; httpx's request lines, which carry the
    # full URL, are below its level.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# ── Shops whose pages only the extension and the Android app can read; the hint for an unreadable page ──────────


async def test_the_page_lists_the_shops_that_turn_the_server_away(client: httpx.AsyncClient) -> None:
    text = (await client.get("/check")).text

    section = text.split('id="browser-only"', 1)[1].split("</details>", 1)[0]
    for name in ("bol.com", "Coolblue", "Allbirds", "AliExpress", "Temu", "eBay"):
        assert f">{name.replace('&', '&amp;')}</li>" in section
    assert 'data-hosts="bol.com"' in section
    assert "Unfret browser extension" in section and "Android app" in section
    assert "not a judgement" in section
    # The note says why, in a sentence: the server is turned away, the extension and the app read it in the browser.
    note = " ".join(section.split('<p class="muted">', 1)[1].split("</p>", 1)[0].split())
    assert "these shops block or stall requests that come from a server" in note
    assert "read the page in your own browser" in note
    assert "scam" not in section.lower()
    assert "{{" not in text


def _run_core(tmp_path: Path, expression: str) -> Any:
    module = tmp_path / "check-core.mjs"
    module.write_text((STATIC_DIR / "check-core.js").read_text())
    script = f"import * as core from {json.dumps(module.as_uri())};process.stdout.write(JSON.stringify({expression}));"
    run = subprocess.run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(run.stdout)


@pytest.mark.usefixtures("node")
def test_a_product_ad_or_profile_link_is_told_from_a_search_or_home_page(tmp_path: Path) -> None:
    cases = {
        "https://www.aliexpress.com/item/1005006144375553.html": True,
        "https://www.amazon.nl/dp/B07WTQ4YT5": True,
        "https://www.bol.com/nl/nl/p/koptelefoon/9300000181761399/": True,
        "https://shop.example/products/wool-sweater": True,
        "https://shop.example/product/wool-sweater/": True,
        "https://www.ebay.com/itm/266000000000": True,
        "https://www.etsy.com/listing/123/mug": True,
        "https://www.temu.com/nl-en/goods.html?goods_id=601099512345678": True,
        "https://www.marktplaats.nl/v/fietsen/m2243708464-fiets": True,
        "https://www.ebay.com/usr/some-seller": True,
        "https://www.zalando.nl/tommy-jeans-jas-black-tob22t0fk-q11.html": True,  # a marketplace's listing
        "https://www.zara.com/us/en/linen-blend-shirt-p03332301.html?v1=123": True,  # -p + 8 digits
        "https://www.zara.com/us/en/man-shirts-l737.html": False,  # a category
        "https://www.aliexpress.com/": False,
        "https://www.aliexpress.com/w/wholesale-airpods.html": False,
        "https://www.amazon.nl/s?k=airpods": False,
        "https://www.bol.com/nl/nl/s/?searchtext=airpods": False,
        "https://www.ebay.com/sch/i.html?_nkw=airpods": False,
        "https://shop.example/": False,
        "https://shop.example/search?q=sweater": False,
        "https://shop.example/collections/all": False,
        "not a link": False,
    }

    out = _run_core(tmp_path, f"{json.dumps(list(cases))}.map((link) => core.looksLikeItem(link))")

    assert dict(zip(cases, out, strict=True)) == cases


@pytest.mark.usefixtures("node")
def test_a_link_is_matched_to_a_listed_shop_by_its_host(tmp_path: Path) -> None:
    shops = [{"name": name, "hosts": list(hosts)} for name, hosts in BROWSER_ONLY_SHOPS]
    links = [
        "https://www.bol.com/nl/nl/p/x/1/",
        "https://www2.hm.com/nl_nl/productpage.1.html",
        "https://nl.aliexpress.com/item/1.html",
        "https://www.coolblue.be/nl/product/1",
        "https://notbol.com/p/1",
        "https://bol.com.example/p/1",
        "https://www.zalando.nl/x.html",
    ]

    out = _run_core(
        tmp_path, f"{json.dumps(links)}.map((link) => core.shopOf(link, {json.dumps(shops)})?.name ?? null)"
    )

    assert out == ["bol.com", "H&M", "AliExpress", "Coolblue", None, None, None]


@pytest.mark.usefixtures("node")
def test_an_unreadable_product_link_gets_the_extension_and_app_not_the_search_hint(tmp_path: Path) -> None:
    shops = [{"name": "bol.com", "hosts": ["bol.com"]}]
    cases = [
        ("PAGE_TOO_THIN", "https://www.aliexpress.com/item/1005006144375553.html", "item"),  # the tester's case
        ("PAGE_TOO_THIN", "https://shop.example/products/wool-sweater", "item"),
        ("PAGE_TOO_THIN", "https://shop.example/", "reason"),  # a home page keeps "try the product's own link"
        ("PAGE_TOO_THIN", "https://www.amazon.nl/s?k=airpods", "reason"),
        ("PAGE_BLOCKED", "https://shop.example/products/wool-sweater", "reason"),
        ("PAGE_BLOCKED", "https://www.bol.com/nl/nl/p/x/1/", "shop"),
        ("PAGE_UNAVAILABLE", "https://www.bol.com/", "shop"),
    ]
    calls = ",".join(f"core.unreadableKind({json.dumps(c)}, {json.dumps(link)}, shops)" for c, link, _ in cases)

    out = _run_core(tmp_path, f"(() => {{ const shops = {json.dumps(shops)}; return [{calls}]; }})()")

    assert out == [kind for *_, kind in cases]
    script = (STATIC_DIR / "check.js").read_text()
    assert "If this was a search or home page" in script  # the old hint, still there for searches and home pages
    assert "only work with the Unfret browser extension or the Android app" in script
    # A first check of a listed shop asks the server before the free pass's proof of work.
    assert "shopOf(link, BROWSER_ONLY_SHOPS) && (await unreadableFirst(link, abort))" in script


@pytest.mark.usefixtures("node")
def test_a_pass_the_server_no_longer_knows_is_a_new_free_start(tmp_path: Path) -> None:
    out = _run_core(
        tmp_path,
        """[
          core.passEnded({ status: 401, code: "INVALID_API_KEY" }, { kind: "key" }),
          core.passEnded({ status: 401, code: "INVALID_API_KEY" }, { kind: "wallet" }),
          core.passEnded({ status: 401, code: "UNAUTHORIZED" }, { kind: "key" }),
          core.passEnded({ status: 402, code: "INSUFFICIENT_CREDITS" }, { kind: "key" }),
          core.passEnded({ status: 401, code: "INVALID_API_KEY" }, null),
          core.passEndedHint(5),
          core.passEndedHint(0),
        ]""",
    )
    assert out[:5] == [True, False, False, False, False]  # a wallet's 401 stays an expired sign-in
    assert out[5:] == [
        "Unfret has a new free start: your old pass has ended. Your first 5 deep checks are free: tap Check.",
        "Unfret has a new free start: your old pass has ended. Quick checks stay free; deep checks come in packs.",
    ]


def test_the_page_drops_an_ended_pass_before_treating_a_401_as_an_expired_sign_in() -> None:
    script = (STATIC_DIR / "check.js").read_text()
    # Wherever a 401 is handled (the balance, a check, a code), an ended pass is caught first: dropped (with the pass
    # made here, if it is that one), no "sign in again", and the next Check gets the free pass.
    assert script.count("if (passEnded(error, account))") == 3
    for handler in ("async function refreshChecks()", "async function failed(error, link)", "invite-redeem"):
        start = script.index(handler)
        assert script.index("passEnded(error, account)", start) < script.index("error.status === 401", start)
    retired = script[script.index("function passRetired()") :]
    retired = retired[: retired.index("\n}\n")]
    assert "forgetNewPass(" in retired and "signedOut(false)" in retired and "ended = true" in retired
    assert "passEndedHint(fresh ? freePerPass() : 0)" in script
    assert "function freeAvailable() {\n  return !account && !freeGiven()" in script  # the free pass follows
    mine = (STATIC_DIR / "my-checks.js").read_text()
    assert "Unfret has a new free start: the pass that was on this device has ended." in mine


_READABLE = (
    "<html><body><main><h1>Lamp</h1>" + "<p>A brass desk lamp with a linen shade.</p>" * 20 + "</main></body></html>"
)


@pytest.mark.parametrize(
    ("asked", "landed", "address"),
    [
        # A short link: the page's own address, without fragment, credentials or tracking (the product id stays).
        (
            "https://short.test/d/AbC",
            "https://user:pw@Shop.test/dp/B0TEST1234/ref=x?psc=1&utm_source=share&variant=7#reviews",
            "https://shop.test/dp/B0TEST1234/ref=x?variant=7",
        ),
        ("http://shop.test/p/1", "https://shop.test/p/1", "https://shop.test/p/1"),  # to https: that address
        # No redirect: the address as asked (cleaned downstream, as before).
        (
            "https://shop.test/p/1?utm_source=x",
            "https://shop.test/p/1?utm_source=x",
            "https://shop.test/p/1?utm_source=x",
        ),
    ],
)
async def test_a_fetched_page_comes_with_its_own_address(
    monkeypatch: pytest.MonkeyPatch, asked: str, landed: str, address: str
) -> None:
    async def fetch(url: str, **_: Any) -> FetchedPage:
        assert url == asked
        return FetchedPage(_READABLE.encode(), landed)

    monkeypatch.setattr(page_fetch, "fetch_public_page", fetch)

    page = await page_fetch.fetch_page(asked)

    assert page.url == address and "brass desk lamp" in page.html
