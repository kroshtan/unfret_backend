"""
/developers/http, "Use Unfret from any language": what it says is what the server does.

The JavaScript examples run under Node against a small stand-in server that answers with the real models and the
real proof-of-work check (:class:`unfret.credits.keys.KeyGuard`); every endpoint and field the page names is in the
OpenAPI document. Node is required in CI (``CI`` set); elsewhere those tests are skipped without it.
"""

import html
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import AsyncIterator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from unfret import clients_info
from unfret.config import settings
from unfret.credits.keys import KeyGuard, pow_ok
from unfret.main import app
from unfret.routes.check import ERRORS, CheckRequest
from unfret.routes.keys import ChallengeResponse, CreateKeyRequest, KeyResponse
from unfret.routes.pages import DEVELOPERS_MEASURED, STATIC_DIR, _pow_example
from unfret.schemas import errors as errors_module
from unfret.visitors import DailyLimit
from unfret.x402.verdict import Verdict

BASE = settings.public_base_url.rstrip("/")
NODE = shutil.which("node")
PASS = "ufk_" + "t" * 40


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _page(client: httpx.AsyncClient) -> str:
    response = await client.get("/developers/http")
    assert response.status_code == 200
    return response.text


def _snippet(page: str, block: str) -> str:
    found = re.search(rf'<pre class="terminal" id="{block}">(.*?)</pre>', page, re.DOTALL)
    assert found, block
    return html.unescape(re.sub(r"<[^>]+>", "", found.group(1)))


def _text(page: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def _node() -> str:
    if NODE is None:
        if os.environ.get("CI"):
            pytest.fail("node is needed to run the page's JavaScript examples")
        pytest.skip("node is not installed")
    return NODE


def _run_js(code: str, tmp_path: Path, env: dict[str, str] | None = None) -> str:
    script = tmp_path / "snippet.mjs"
    script.write_text(code, encoding="utf-8")
    done = subprocess.run(
        [_node(), str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, **(env or {})},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


# ── A stand-in server: the real models and the real proof-of-work check, nothing else ───────────────────────────


class _Server(ThreadingHTTPServer):
    guard: KeyGuard
    requests: list[tuple[str, str, dict[str, Any]]]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path != "/v1/keys/challenge":
            self._answer(404, {"detail": "Not found", "error_code": "NOT_FOUND"})
            return
        text, bits, expires = self.server.guard.challenge()
        self._answer(200, ChallengeResponse(challenge=text, bits=bits, expires_at=expires).model_dump())

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.server.requests.append((self.path, self.headers.get("Authorization", ""), json.loads(raw)))
        if self.path == "/v1/keys":
            body = CreateKeyRequest.model_validate_json(raw)
            if not self.server.guard.redeem(body.challenge, body.solution):
                self._answer(400, {"detail": "Challenge invalid.", "error_code": "BAD_CHALLENGE"})
                return
            made = KeyResponse(
                key_id="k1",
                key=PASS,
                recovery_code="ufr_test",
                account="key:k1",
                balance_usd_micros=0,
                free_checks=settings.developer_free_checks if body.developer else 0,
            )
            self._answer(201, made.model_dump())
        elif self.path == "/v1/check":
            CheckRequest.model_validate_json(raw)
            if self.headers.get("Authorization") != f"Bearer {PASS}":
                self._answer(401, {"detail": "Not a valid pass.", "error_code": "INVALID_API_KEY"})
                return
            sample = json.loads(DEVELOPERS_MEASURED.read_text(encoding="utf-8"))["sample"]
            Verdict.model_validate(sample)  # what the real endpoint answers
            self._answer(200, sample)
        else:
            self._answer(404, {"detail": "Not found", "error_code": "NOT_FOUND"})


@pytest.fixture
def stand_in() -> Iterator[_Server]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.guard = KeyGuard(
        secret="test", bits=12, ttl_s=60, creations=DailyLimit(5, 500), recoveries=DailyLimit(5, 500)
    )
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(server: _Server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host!s}:{port}"


# ── The examples run ─────────────────────────────────────────────────────────────────────────────────────────────


async def test_the_proof_of_work_in_javascript_is_the_one_the_server_checks(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    solve = _snippet(await _page(client), "pow-js")
    guard = KeyGuard(secret="s", bits=14, ttl_s=60, creations=DailyLimit(5, 500), recoveries=DailyLimit(5, 500))
    challenge, bits, _ = guard.challenge()
    solution = _run_js(f"{solve}\nconsole.log(await solve({json.dumps(challenge)}, {bits}));", tmp_path)
    # The same answer as counting up with the server's own check (and the clients' solve): the first counter.
    first = next(str(n) for n in range(1 << 22) if pow_ok(challenge, str(n), bits))
    assert solution == first
    assert guard.redeem(challenge, solution)


async def test_the_javascript_examples_make_a_pass_and_check_a_page(
    client: httpx.AsyncClient, stand_in: _Server, tmp_path: Path
) -> None:
    page = await _page(client)
    here = _url(stand_in)
    made = _run_js(_snippet(page, "pow-js") + "\n\n" + _snippet(page, "pass-js").replace(BASE, here), tmp_path)
    assert made == f"{PASS} ufr_test {settings.developer_free_checks}"
    printed = _run_js(_snippet(page, "example-js").replace(BASE, here), tmp_path, {"UNFRET_PASS": PASS})
    sample = json.loads(DEVELOPERS_MEASURED.read_text(encoding="utf-8"))["sample"]
    assert printed == f"{sample['band']} {sample['action']}"
    assert [path for path, _, _ in stand_in.requests] == ["/v1/keys", "/v1/check"]
    assert stand_in.requests[0][2]["developer"] is True
    assert stand_in.requests[1][2] == {"url": f"{here}/test-shop", "tier": "deep"}


async def test_the_curl_example_prints_the_band(client: httpx.AsyncClient, stand_in: _Server) -> None:
    if not (shutil.which("curl") and shutil.which("jq") and shutil.which("bash")):
        pytest.skip("needs curl, jq and bash")
    command = _snippet(await _page(client), "example-curl").replace(BASE, _url(stand_in))
    done = subprocess.run(
        [str(shutil.which("bash")), "-c", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "UNFRET_PASS": PASS},
    )
    sample = json.loads(DEVELOPERS_MEASURED.read_text(encoding="utf-8"))["sample"]
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [sample["band"], sample["action"]]


async def test_every_example_checks_the_test_shop_with_a_pass_and_a_timeout_above_the_servers(
    client: httpx.AsyncClient,
) -> None:
    page = await _page(client)
    for block in ("example-curl", "example-js", "example-go", "example-php"):
        code = _snippet(page, block)
        assert f"{BASE}/v1/check" in code and f"{BASE}/test-shop" in code, block
        assert "UNFRET_PASS" in code and "Bearer" in code, block
        assert "band" in code and "120" in code, block
    # The examples wait 120 s: longer than the server takes before it answers a free 504.
    assert settings.analysis_timeout_s < 120 and settings.x402_analysis_timeout_s < 120


# ── What it names exists ─────────────────────────────────────────────────────────────────────────────────────────


def _schema(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref``, ``anyOf`` (the non-null branch) and arrays' ``items`` to an object schema."""
    while True:
        if "$ref" in schema:
            schema = document["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
        elif "anyOf" in schema:
            schema = next(s for s in schema["anyOf"] if s.get("type") != "null")
        elif schema.get("type") == "array":
            schema = schema["items"]
        else:
            return schema


def _has(document: dict[str, Any], schema: dict[str, Any], dotted: str) -> bool:
    for part in dotted.replace("[]", "").split("."):
        properties = _schema(document, schema).get("properties", {})
        if part not in properties:
            return False
        schema = properties[part]
    return True


def _table(page: str, table: str) -> list[tuple[str, bool]]:
    found = re.search(rf'<table class="settings fields" id="{table}">(.*?)</table>', page, re.DOTALL)
    assert found, table
    rows = re.findall(r"<tr( class=\"pass-only\")?><td><code>([^<]+)</code>", found.group(1))
    assert rows, table
    return [(name, bool(pass_only)) for pass_only, name in rows]


async def test_every_endpoint_and_field_on_the_page_is_in_the_openapi_document(client: httpx.AsyncClient) -> None:
    page = await _page(client)
    document = app.openapi()
    text = _text(page)
    named = set(re.findall(r"\b(GET|POST) (/v1/[\w/{}-]+)", text))
    assert {
        ("POST", "/v1/check"),
        ("GET", "/v1/keys/challenge"),
        ("POST", "/v1/keys"),
        ("GET", "/v1/credits"),
    } <= named
    for method, path in named:
        assert method.lower() in document["paths"].get(path, {}), (method, path)

    check = document["paths"]["/v1/check"]["post"]
    request = check["requestBody"]["content"]["application/json"]["schema"]
    response = check["responses"]["200"]["content"]["application/json"]["schema"]
    for name, _ in _table(page, "check-request"):
        assert _has(document, request, name), name
    for name, pass_only in _table(page, "check-response"):
        if pass_only:  # added to the verdict for a pass; the operation's description names them
            assert f"``{name}``" in check["description"], name
        else:
            assert _has(document, response, name), name


def _schema_of(document: dict[str, Any], path: str, method: str, status: str | None = None) -> dict[str, Any]:
    operation = document["paths"][path][method]
    if status is None:
        return dict(operation["requestBody"]["content"]["application/json"]["schema"])
    return dict(operation["responses"][status]["content"]["application/json"]["schema"])


async def test_the_pass_and_credits_fields_on_the_page_are_in_the_openapi_document(client: httpx.AsyncClient) -> None:
    page = await _page(client)
    document = app.openapi()
    named_fields = {
        ("/v1/keys/challenge", "get", "200"): ("challenge", "bits", "expires_at"),
        ("/v1/keys", "post", None): ("challenge", "solution", "developer"),
        ("/v1/keys", "post", "201"): (
            "key",
            "recovery_code",
            "key_id",
            "account",
            "balance_usd_micros",
            "free_checks",
        ),
        ("/v1/credits", "get", "200"): (
            "checks_remaining",
            "free_checks_remaining",
            "free_checks_paused",
            "balance_usd_micros",
        ),
    }
    for (path, method, status), fields in named_fields.items():
        for name in fields:
            assert f"<code>{name}</code>" in page or f'"{name}"' in page, name
            assert _has(document, _schema_of(document, path, method, status), name), (path, name)


async def test_every_error_code_on_the_page_is_one_the_server_sends(client: httpx.AsyncClient) -> None:
    page = await _page(client)
    errors = page.split('id="errors"', 1)[1].split("</table>", 1)[0]
    sent = set(ERRORS) | {
        "BAD_CHALLENGE",
        *re.findall(r'error_code = "(\w+)"', Path(errors_module.__file__).read_text()),
    }
    codes = re.findall(r"<tr><td><code>([A-Z_]+)</code>(?:, <code>([A-Z_]+)</code>)?", errors)
    assert len(codes) >= 10
    for pair in codes:
        for code in filter(None, pair):
            assert code in sent, code


async def test_the_links_on_the_page_lead_somewhere(client: httpx.AsyncClient) -> None:
    page = await _page(client)
    for path in sorted(set(re.findall(r'href="(/[^"#]*)', page)) - {"/"}):
        if path.startswith("/static/"):
            continue
        assert (await client.get(path)).status_code == 200, path
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    for anchor in re.findall(r'href="#([^"]+)"', page):
        assert anchor in ids, anchor
    developers = (await client.get("/developers")).text
    developer_ids = set(re.findall(r'\bid="([^"]+)"', developers))
    for anchor in re.findall(r'href="/developers#([^"]+)"', page):
        assert anchor in developer_ids, anchor


# ── The numbers come from the settings ───────────────────────────────────────────────────────────────────────────


async def test_the_numbers_come_from_the_settings(client: httpx.AsyncClient) -> None:
    response = await client.get("/developers/http")
    assert "default-src 'none'" in response.headers["content-security-policy"]
    page = response.text
    assert "{{" not in page and "<script>" not in page and " style=" not in page and " on" + "click=" not in page
    text = _text(page)
    bits = settings.key_pow_bits
    assert f'"bits": {bits}' in text and f"about {2**bits:,} hashes" in text
    assert f"within {settings.key_pow_ttl_s / 60:g} minutes" in text
    assert f"{settings.developer_free_checks} free deep checks" in text
    assert f"At most {settings.key_create_per_ip_per_day} new passes a day per network" in text
    assert f"{settings.quick_checks_per_minute} a minute" in text
    assert f"within {settings.analysis_timeout_s:g} s with a pass" in text
    measured = json.loads(DEVELOPERS_MEASURED.read_text(encoding="utf-8"))
    assert f"{measured['deep']['median_s']:.1f} s" in text and measured["server"] in text


def test_the_proof_of_work_is_described_byte_by_byte() -> None:
    assert _pow_example(18) == "the first 2 bytes are 0 and the next one is below 64"
    assert _pow_example(16) == "the first 2 bytes are 0"
    assert _pow_example(8) == "the first byte is 0"
    assert _pow_example(4) == "the first byte is below 16"
    assert _pow_example(9) == "the first byte is 0 and the next one is below 128"


# ── Python on PyPI, TypeScript to copy, everything else here ─────────────────────────────────────────────────────


async def test_typescript_is_a_reference_to_copy_not_an_npm_package(client: httpx.AsyncClient) -> None:
    page = await _page(client)
    assert clients_info.TYPESCRIPT_CLIENT_URL in page and clients_info.TYPESCRIPT_CLIENT_MIRROR in page
    assert "isn't published on npm" in _text(page)
    for path in ("/developers", "/developers/http", "/llms.txt"):
        body = (await client.get(path)).text
        assert "coming to npm" not in body.lower() and 'from "unfret-client"' not in body, path
    package = json.loads((Path(__file__).parents[2] / "clients" / "typescript" / "package.json").read_text())
    assert package["private"] is True  # npm refuses to publish it


@pytest.mark.parametrize("on_pypi", [False, True])
async def test_the_python_client_install_line_is_true_before_and_after_pypi(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, on_pypi: bool
) -> None:
    monkeypatch.setattr(clients_info, "PYTHON_CLIENT_ON_PYPI", on_pypi)
    source = "git+https://git.kroshtan.com/kroshtan/unfret_backend.git#subdirectory=clients/python"
    developers = (await client.get("/developers")).text
    llms = (await client.get("/llms.txt")).text
    for body in (_text(developers), llms):
        assert "pip install unfret-client" in body
        assert (source in body) is (not on_pypi)
    assert 'href="/developers/http"' in developers and f"{BASE}/developers/http" in llms
    assert "## Clients" in llms


async def test_developers_and_the_footer_lead_to_the_page(client: httpx.AsyncClient) -> None:
    developers = (await client.get("/developers")).text
    assert developers.count('href="/developers/http"') >= 3
    footer = (STATIC_DIR / "partials" / "footer.html").read_text(encoding="utf-8")
    assert 'href="/developers/http">Unfret from any language</a>' in footer
