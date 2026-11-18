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
    assert "<h1>Check before you pay</h1>" in page
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


async def test_the_welcome_page_waits_while_the_extension_makes_its_pass(client: httpx.AsyncClient) -> None:
    page = (await client.get("/welcome?setup=1")).text

    assert "Getting your free checks ready" in page and "{{" not in page
    assert re.findall(r'<script[^>]*src="([^"]+)"', page) == [
        f"/static/{asset_tag()}/welcome.js",
        f"/static/{asset_tag()}/ref.js",
    ]
    assert 'href="/welcome">Continue</a>' in page  # without scripts


async def test_the_welcome_page_says_why_a_pass_came_without_free_checks(client: httpx.AsyncClient) -> None:
    had = (await client.get("/welcome?note=had-free")).text
    assert "This browser has had its free checks already" in had and "<h1>Check before you pay</h1>" in had
    none = (await client.get("/welcome?note=none-today")).text
    assert "There were no free checks to give just now" in none
    for path in ("/welcome", "/welcome?note=other", "/welcome?note=%3Cb%3E"):
        page = (await client.get(path)).text
        assert "{{" not in page and 'class="note"' not in page and 'role="status"' not in page


def _run_welcome(tmp_path: Path, body: str) -> Any:
    """
    Run ``body`` in node with ``welcome.js`` (and the ``ref.js`` it imports) and a fake same-origin window.

    :param tmp_path: Where to put the modules.
    :param body: An async function body using ``askFreeChecks``, ``nextWelcome``, ``makeWindow`` and ``makeStorage``.
    :return: What it printed, parsed.
    """
    for name in ("welcome.js", "ref.js"):
        (tmp_path / name).write_text((STATIC_DIR / name).read_text())
    script = f"""
import {{ askFreeChecks, nextWelcome }} from {json.dumps((tmp_path / "welcome.js").as_uri())};
// A window that delivers posted messages to its listeners; `extension` plays the extension's content script.
function makeWindow(extension) {{
  const listeners = new Set();
  const win = {{
    location: {{ origin: "https://unfret.net", replaced: [], replace: (path) => win.location.replaced.push(path) }},
    posted: [],
    addEventListener: (type, fn) => listeners.add(fn),
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
function makeStorage(items = {{}}) {{
  return {{ items, getItem: (k) => items[k] ?? null, setItem: (k, v) => {{ items[k] = String(v); }} }};
}}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const out = await (async () => {{ {body} }})();
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True, timeout=30
    )
    return json.loads(result.stdout)


@pytest.mark.usefixtures("node")
def test_the_welcome_page_tells_the_extension_whether_this_browser_had_its_free_checks(tmp_path: Path) -> None:
    out = _run_welcome(
        tmp_path,
        """
        const fresh = makeStorage();
        const first = makeWindow((data, w) => {
          if (data.type === "unfret-free-ask") w.deliver({ type: "unfret-free-answer", status: "free", free: 5 });
        });
        askFreeChecks(first, fresh, { everyMs: 20, giveUpMs: 2000 });
        await sleep(100);
        const reinstalled = makeWindow((data, w) => {
          if (data.type === "unfret-free-ask") w.deliver({ type: "unfret-free-answer", status: "had-free", free: 0 });
        });
        askFreeChecks(reinstalled, fresh, { everyMs: 20, giveUpMs: 2000 });
        await sleep(100);
        return {
          first: first.posted, firstTo: first.location.replaced, note: fresh.items,
          again: reinstalled.posted.map((p) => p.data), againTo: reinstalled.location.replaced,
        };
        """,
    )
    # A yes or no, to this page's own origin only; nothing else goes to the extension.
    assert out["first"] == [
        {"data": {"type": "unfret-free-ask", "had": False}, "targetOrigin": "https://unfret.net"},
    ]
    assert out["firstTo"] == ["/welcome?free=5"]
    assert out["note"] == {"unfret-free-pass": "1"}  # the link checker won't give a second round either
    assert out["again"] == [{"type": "unfret-free-ask", "had": True}]
    assert out["againTo"] == ["/welcome?note=had-free"]


@pytest.mark.usefixtures("node")
def test_the_welcome_page_moves_on_without_an_answer_and_ignores_other_windows(tmp_path: Path) -> None:
    out = _run_welcome(
        tmp_path,
        """
        const storage = makeStorage();
        const win = makeWindow((data, w) => {
          w.deliver({ type: "unfret-free-answer", status: "free", free: 5 }, { source: {} }); // an iframe
          w.deliver({ type: "unfret-free-answer", status: "free", free: 5 }, { origin: "https://evil.example" });
        });
        askFreeChecks(win, storage, { tries: 3, everyMs: 10, giveUpMs: 200 });
        await sleep(400);
        const failed = makeWindow((data, w) => w.deliver({ type: "unfret-free-answer", status: "error" }));
        askFreeChecks(failed, storage, { everyMs: 20, giveUpMs: 200 });
        await sleep(400);
        const answers = [
          { status: "free", free: 3 }, { status: "free", free: 0 }, { status: "free", free: "5x" },
          { status: "none-today" }, { status: "has-pass" }, { status: "<b>" }, null,
        ];
        return {
          asked: win.posted.length, to: win.location.replaced, failedTo: failed.location.replaced,
          note: storage.items, next: answers.map(nextWelcome),
        };
        """,
    )
    assert out["asked"] == 3 and out["to"] == ["/welcome"]
    assert out["failedTo"] == ["/welcome"]  # once, though the give-up timer fires later too
    assert out["note"] == {}  # no pass made: this browser still has its turn
    assert out["next"] == [
        "/welcome?free=3",
        "/welcome",
        "/welcome",
        "/welcome?note=none-today",
        "/welcome",
        "/welcome",
        "/welcome",
    ]
