"""
Onboarding: /welcome (opened once by the extension after installing) and the key hand-off from /checkout.

A new key shown on /checkout is posted to the page itself for the Unfret extension's content script (``handoff.js``);
links from the extension and /check name a key's ID to top up (``?mode=topup&key_id=``), never the key itself.
"""

import json
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
    # Nothing loaded from elsewhere: no script at all, and the only outside addresses are the footer's links.
    assert "<script" not in page
    for footer_link in ('href="https://git.kroshtan.com/kroshtan/unfret_backend"', 'href="https://kroshtan.com"'):
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
    assert 'import { handOffKey, topupKeyId } from "./handoff.js";' in script
    # The code, card, recovery results (showResult) and the crypto tab's "Make a new key".
    assert "nodes.push(handoffBox(issued, steps), keyCard(issued)" in script
    assert "keyReveal.replaceChildren(handoffBox(issued), keyCard(issued));" in script
    assert "Your Unfret extension is signed in with this key — you can start checking pages now." in script
    assert "Switch it to this new key?" in script
    assert "choose “API key” and paste your key" in script


def test_check_page_tops_up_a_key_by_its_id() -> None:
    script = (STATIC_DIR / "check.js").read_text()
    assert "`/checkout?mode=topup&key_id=${id}`" in script
    assert "?for=key&key" not in script and "key_id=${account.token" not in script


def _run(tmp_path: Path, body: str) -> Any:
    """
    Run ``body`` in node with ``handoff.js`` imported and a fake same-origin window; it prints JSON.

    :param tmp_path: Where to put the module.
    :param body: An async function body using ``handOffKey``, ``topupKeyId`` and ``makeWindow``.
    :return: What it printed, parsed.
    """
    module = tmp_path / "handoff.mjs"
    module.write_text((STATIC_DIR / "handoff.js").read_text())
    script = f"""
import {{ handOffKey, topupKeyId }} from {json.dumps(module.as_uri())};
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
