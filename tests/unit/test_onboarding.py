"""
Onboarding: /welcome (opened once by the extension after installing) and the key hand-off from /checkout.

A new key shown on /checkout is posted to the page itself for the Unfret extension's content script (``handoff.js``);
links from the extension and /check name a key's ID to top up (``?mode=topup&key_id=``), never the key itself.
"""

import json
import re
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from unfret.config import settings
from unfret.main import app
from unfret.routes.pages import STATIC_DIR, asset_tag

KEY = "ufk_abcdefghij_" + "a" * 52


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_welcome_page_offers_three_ways_in(client: httpx.AsyncClient) -> None:
    response = await client.get("/welcome")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    page = response.text
    assert "Shop without the worry" in page
    assert 'href="/checkout?mode=code"' in page and 'href="/checkout"' in page and 'href="#have-key"' in page
    assert f'"/static/{asset_tag()}/site.css"' in page and '"/static/site.css"' not in page
    # Nothing loaded from elsewhere: only our own scripts (the affiliate link's code, and handing it to the
    # extension), and the only outside addresses are the footer's links.
    assert re.findall(r'<script[^>]*src="([^"]+)"', page) == [
        f"/static/{asset_tag()}/welcome.js",
        f"/static/{asset_tag()}/ref.js",
    ]
    sources = [
        f'href="https://{host}/kroshtan/{repo}"'
        for host in ("git.kroshtan.com", "github.com")
        for repo in ("unfret_backend", "unfret_extension", "unfret_android")
    ]
    for footer_link in (*sources, 'href="https://kroshtan.com"'):
        page = page.replace(footer_link, "")
    assert "http://" not in page and "https://" not in page


async def test_checkout_names_the_store_listings_for_the_hand_off(client: httpx.AsyncClient) -> None:
    page = (await client.get("/checkout")).text

    assert "{{" not in page
    assert f'href="{settings.chrome_store_url}"' in page and f'href="{settings.firefox_store_url}"' in page
    assert '<template id="extension-get">' in page
    assert 'id="topup-for"' in page and 'id="topup-other"' in page
    assert f'"/static/{asset_tag()}/checkout.js"' in page


def test_checkout_hands_every_new_key_to_the_extension() -> None:
    script = (STATIC_DIR / "checkout.js").read_text()
    assert 'import { awaitConfirmation, handOffKey, topupKeyId } from "./handoff.js";' in script
    # The code, card, recovery results (showResult) and the crypto tab's "Make a new key".
    assert "nodes.push(handoffBox(issued, steps), keyCard(issued)" in script
    assert "keyReveal.replaceChildren(handoffBox(issued), keyCard(issued));" in script
    assert "Your Unfret extension uses this pass now: you can start checking pages." in script
    assert "Switch it to this pass?" in script
    assert "Paste your pass into the extension when it asks." in script


def test_my_checks_sends_the_pass_through_checkout_where_the_extension_listens() -> None:
    # The extension's key relay (0.11.0) runs on /checkout* only and its background takes a key from /checkout only:
    # a hand-off posted on /my-checks would never be answered.
    page = (STATIC_DIR / "my-checks.html").read_text()
    assert 'href="/checkout?send=pass">Send to the extension</a>' in page
    assert "handOffKey" not in (STATIC_DIR / "my-checks.js").read_text()
    script = (STATIC_DIR / "checkout.js").read_text()
    assert 'const send = params.get("send") === "pass";' in script
    assert "handoffBox({ key: stored.token })" in script


def test_check_page_tops_up_a_key_by_its_id() -> None:
    script = (STATIC_DIR / "check.js").read_text()
    assert "`/checkout?mode=topup&key_id=${id}`" in script
    assert "?for=key&key" not in script and "key_id=${account.token" not in script


def _run(tmp_path: Path, body: str) -> Any:
    """
    Run ``body`` in node with ``handoff.js`` imported and a fake same-origin window; it prints JSON.

    :param tmp_path: Where to put the module.
    :param body: An async function body using ``handOffKey``, ``awaitConfirmation``, ``topupKeyId`` and
        ``makeWindow``.
    :return: What it printed, parsed.
    """
    module = tmp_path / "handoff.mjs"
    module.write_text((STATIC_DIR / "handoff.js").read_text())
    script = f"""
import {{ awaitConfirmation, handOffKey, topupKeyId }} from {json.dumps(module.as_uri())};
// A window that delivers posted messages to its listeners, and lets a test play the extension's content script.
function makeWindow(extension) {{
  const listeners = new Set();
  const win = {{
    location: {{ origin: "https://unfret.net" }},
    posted: [],
    addEventListener: (type, fn) => listeners.add(fn),
    removeEventListener: (type, fn) => listeners.delete(fn),
    deliver(data, {{ source = win, origin = "https://unfret.net" }} = {{}}) {{
      for (const fn of [...listeners]) fn({{ data, source, origin }});
    }},
    postMessage(data, targetOrigin) {{
      win.posted.push({{ data, targetOrigin }});
      setTimeout(() => extension?.(data, win), 5);
    }},
  }};
  return win;
}}
const out = await (async () => {{ {body} }})();
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


@pytest.mark.usefixtures("node")
def test_hand_off_signs_the_extension_in(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        f"""
        const win = makeWindow((data, w) => {{
          if (data.type === "unfret-key") w.deliver({{ type: "unfret-key-received", status: "signed-in" }});
        }});
        const status = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500 }});
        return {{ status, posted: win.posted }};
        """,
    )
    assert out["status"] == "signed-in"
    # Only to this page's own origin, never "*"; the key goes out, nothing else.
    assert out["posted"] == [{"data": {"type": "unfret-key", "key": KEY}, "targetOrigin": "https://unfret.net"}]


@pytest.mark.usefixtures("node")
def test_hand_off_asks_again_only_to_replace(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        f"""
        const win = makeWindow((data, w) => {{
          if (data.type !== "unfret-key") return;
          w.deliver({{ type: "unfret-key-received", status: data.replace ? "signed-in" : "other-account" }});
        }});
        const first = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500 }});
        const second = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500, replace: true }});
        return {{ first, second, replace: win.posted.map((p) => p.data.replace ?? null) }};
        """,
    )
    assert out == {"first": "other-account", "second": "signed-in", "replace": [None, True]}


@pytest.mark.usefixtures("node")
def test_hand_off_ignores_answers_from_elsewhere(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        f"""
        const win = makeWindow((data, w) => {{
          if (data.type !== "unfret-key") return;
          w.deliver({{ type: "unfret-key-received", status: "signed-in" }}, {{ source: {{}} }}); // an iframe
          w.deliver({{ type: "unfret-key-received", status: "signed-in" }}, {{ origin: "https://evil.example" }});
          w.deliver({{ type: "unfret-key-received", status: "anything" }});
          w.deliver({{ type: "unfret-key-received" }});
          w.deliver("unfret-key-received");
          w.deliver(null);
        }});
        const ignored = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 200 }});
        const silent = await handOffKey({json.dumps(KEY)}, {{ win: makeWindow(null), waitMs: 50 }});
        const bad = makeWindow(null);
        const invalid = await handOffKey("ufk_not_a_key", {{ win: bad, waitMs: 50 }});
        return {{ ignored, silent, invalid, postedInvalid: bad.posted.length }};
        """,
    )
    assert out == {"ignored": "no-extension", "silent": "no-extension", "invalid": "invalid", "postedInvalid": 0}


@pytest.mark.usefixtures("node")
def test_top_up_links_name_a_key_id_only(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        const ids = ["abcdefghij", "ABCDEFGHIJ", " abcdefghij ", "abcdefghi1", "abcdefghijk", "abc", "", KEY_ARG];
        return ids.map((id) => topupKeyId(new URLSearchParams({ key_id: id })))
          .concat([topupKeyId(new URLSearchParams("mode=topup"))]);
        """.replace("KEY_ARG", json.dumps(KEY)),
    )
    assert out == ["abcdefghij", "abcdefghij", "abcdefghij", None, None, None, None, None, None]


def test_a_pass_from_a_link_is_never_kept_without_asking() -> None:
    # /check#pass=… can be opened by any site: it must not sign this browser in with that pass by itself, even when
    # the browser has no pass yet (the next stop could hand it to the extension, and the visitor's checks would then
    # be bought onto someone else's pass).
    script = (STATIC_DIR / "check.js").read_text()
    receive = script.split("async function receivePass(pass) {", 1)[1].split("\n}\n", 1)[0]
    assert "return take()" not in receive
    assert '$("pass-offer-use").onclick = () => void take();' in receive


def test_checkout_sends_a_stored_pass_to_the_extension_only_on_a_click() -> None:
    script = (STATIC_DIR / "checkout.js").read_text()
    send = script.split("function sendPass() {", 1)[1].split("\n}\n", 1)[0]
    before_click, _, on_click = send.partition('button("Send it to the extension", () =>')
    assert "handoffBox(" not in before_click
    assert "handoffBox({ key: stored.token })" in on_click


@pytest.mark.usefixtures("node")
def test_hand_off_says_when_the_extension_has_its_free_pass(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        f"""
        const win = makeWindow((data, w) => {{
          if (data.type !== "unfret-key") return;
          const status = data.replace ? "signed-in" : "other-account";
          w.deliver({{ type: "unfret-key-received", status, free_pass: true }});
        }});
        const first = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500 }});
        const second = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500, replace: true }});
        return {{ first, second }};
        """,
    )
    assert out == {"first": "free-pass", "second": "signed-in"}
    script = (STATIC_DIR / "checkout.js").read_text()
    assert 'status === "free-pass"' in script and "Any free checks left on it come along" in script


@pytest.mark.usefixtures("node")
def test_hand_off_without_a_click_is_offered_again_on_one(tmp_path: Path) -> None:
    # Extension 0.12.0 takes a first pass only right after a click on the page (any site can open /checkout).
    out = _run(
        tmp_path,
        f"""
        let clicked = false;
        const win = makeWindow((data, w) => {{
          if (data.type !== "unfret-key") return;
          w.deliver({{ type: "unfret-key-received", status: clicked ? "signed-in" : "needs-click" }});
        }});
        const first = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500 }});
        clicked = true;
        const second = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500 }});
        return {{ first, second }};
        """,
    )
    assert out == {"first": "needs-click", "second": "signed-in"}
    script = (STATIC_DIR / "checkout.js").read_text()
    needs_click = script.split('if (status === "needs-click") {', 1)[1].split("\n    }\n", 1)[0]
    assert 'button("Send it to the extension", () => {' in needs_click and "offer(false);" in needs_click


@pytest.mark.usefixtures("node")
def test_hand_off_waits_for_the_answer_in_the_extensions_own_window(tmp_path: Path) -> None:
    # On replace, extension 0.12.0 asks in its own window ("confirm"); the answer follows: "signed-in" or "kept".
    out = _run(
        tmp_path,
        f"""
        const win = makeWindow((data, w) => {{
          if (data.type !== "unfret-key") return;
          w.deliver({{ type: "unfret-key-received", status: "confirm" }});
        }});
        const status = await handOffKey({json.dumps(KEY)}, {{ win, waitMs: 500, replace: true }});
        const answers = [];
        awaitConfirmation((answer) => answers.push(answer), {{ win }});
        win.deliver({{ type: "unfret-key-received", status: "signed-in" }}, {{ origin: "https://evil.example" }});
        win.deliver({{ type: "unfret-key-received", status: "confirm" }});
        win.deliver({{ type: "unfret-key-received", status: "kept" }});
        win.deliver({{ type: "unfret-key-received", status: "signed-in" }}); // only the first answer counts
        return {{ status, answers }};
        """,
    )
    assert out == {"status": "confirm", "answers": ["kept"]}
    script = (STATIC_DIR / "checkout.js").read_text()
    assert "Confirm in the Unfret window that just opened" in script
    assert 'awaitConfirmation((answer) => (answer === "signed-in" ? signedIn() : kept()));' in script


def test_connect_page_hands_the_session_over_again_on_a_click() -> None:
    script = (STATIC_DIR / "connect.js").read_text()
    assert 'if (event.data?.type === "unfret-session-status") return extensionStatus(event.data.status);' in script
    needs_click = script.split('if (status === "needs-click" && signedIn) {', 1)[1].split("\n  }", 1)[0]
    assert 'send.addEventListener("click", () => {' in needs_click and "postSession(signedIn);" in needs_click
    assert "Confirm in the Unfret window that just opened" in script
    assert 'id="handoff"' in (STATIC_DIR / "connect.html").read_text()


async def test_the_welcome_page_after_a_free_pass_leads_straight_to_a_check(client: httpx.AsyncClient) -> None:
    page = (await client.get("/welcome?free=5")).text

    assert "You have 5 free checks" in page and "{{" not in page
    assert "No code, no account, nothing to buy." in page
    assert 'href="/test-shop"' in page and "Check this page" in page
    main = page.split("<main", 1)[1].split("</main>", 1)[0]
    assert main.count('href="/checkout') == 1  # buying stays one quiet link away
    assert re.findall(r'<script[^>]*src="([^"]+)"', page) == [
        f"/static/{asset_tag()}/welcome.js",
        f"/static/{asset_tag()}/ref.js",
    ]
    nav = page.split('<nav class="site-nav"', 1)[1].split("</nav>", 1)[0]
    assert '<a class="cta" href="/check">Check a link</a>' in nav
    for odd in ("0", "-1", "101", "five"):
        other = await client.get(f"/welcome?free={odd}")
        assert other.status_code in {200, 422}
        assert "free checks</h1>" not in other.text  # anything else: the page for a pass without free checks


async def test_the_link_checker_offers_examples_starting_with_the_test_shop(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = (await client.get("/check")).text
    examples = page.split('<ul class="example-links">', 1)[1].split("</ul>", 1)[0]
    assert examples.count("<li>") == len(settings.check_examples)
    assert examples.index('data-example="/test-shop"') >= 0
    monkeypatch.setattr(settings, "check_examples", [("A <b>shop</b>", '/x?a="1"'), ("Two", "/y"), ("Three", "/z")])
    page = (await client.get("/check")).text
    examples = page.split('<ul class="example-links">', 1)[1].split("</ul>", 1)[0]
    assert examples.count("<li>") == 3 and "A &lt;b&gt;shop&lt;/b&gt;" in examples and "&quot;1&quot;" in examples


def test_the_link_checker_gets_a_free_pass_once_per_browser() -> None:
    script = (STATIC_DIR / "check.js").read_text()
    assert 'import { makePass } from "./new-pass.js";' in script
    assert "makePass(api, { free: true })" in script and "markFreeGiven();" in script
    # After that, or without a pass: a quick check, saying why, never a refusal.
    assert 'run(link, "quick", abort, "none")' in script and 'await run(link, "quick", abort, outcome)' in script
    assert 'error.code === "FREE_CHECKS_PAUSED"' in script
    checkout = (STATIC_DIR / "checkout.js").read_text()
    assert "const makeKey = () => makePass(api);" in checkout
