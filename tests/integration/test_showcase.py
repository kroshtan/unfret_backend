"""
"Spotted recently": operator-run checks kept as drafts, published after review, and only then public.

The CLI (``python -m veridex.showcase``) runs here against the app in-process: its synchronous client is bridged
to the test's ASGI client, and the shop's page and the analysis graph are test doubles, as in the URL-only tests.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from pydantic import SecretStr

from tests.integration.conftest import FakeGraph
from veridex import page_fetch
from veridex.config import settings
from veridex.facts.schema import PageFacts
from veridex.showcase import MAX_SCREENSHOT_BYTES
from veridex.showcase.cli import promote, run

PRICE = settings.analysis_price_usd_micros
ADMIN = {"Authorization": "Bearer operator-token"}
URL = "https://shop.example/products/led-mask#reviews"
PRODUCT_HTML = (
    "<html><head><title>LED mask</title></head><body><main><h1>LED face mask</h1>"
    + "<p>Professional LED therapy at home, 90% off today only. Ships in 3 weeks.</p>" * 8
    + "</main></body></html>"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
Wallet = Callable[[int], Awaitable[tuple[str, dict[str, str]]]]


def verdict(**extra: Any) -> dict[str, Any]:
    return {
        "band": "junk_or_scam",
        "label": "Many warning signs",
        "headline": "Many warning signs: a generic product with a discount it never had.",
        "reasons": [
            {
                "type": "was_never_charged",
                "title": "A discount that was never real",
                "detail": "The 'was' price of €199 was never charged in the last 90 days.",
                "severity": "high",
            },
            {
                "type": "pushy_copy",
                "title": "Pushy sales talk",
                "detail": 'The page says "100% legit, not a scam!"',
                "severity": "medium",
            },
        ],
        "positives": [],
        "language": "en",
        **extra,
    }


ASSESSMENT = {
    "risk_score": 0.9,
    "confidence": 0.8,
    "model_version": "test",
    "flags": [
        {"type": "content.scam_copy", "label": "Pushy sales talk", "evidence": "not a scam", "kind": "signal"},
        {"type": "domain_age.age_years", "label": "Domain age", "evidence": "3 months", "kind": "context"},
    ],
}


@pytest.fixture(autouse=True)
def operator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))


@pytest.fixture
def shop(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The shop's answer to the server's fetch; ``calls`` lists the fetched URLs."""
    answer: dict[str, Any] = {"body": PRODUCT_HTML.encode(), "error": None, "calls": []}

    async def fake_fetch(url: str, **_: Any) -> bytes:
        answer["calls"].append(url)
        if answer["error"] is not None:
            raise answer["error"]
        body: bytes = answer["body"]
        return body

    monkeypatch.setattr(page_fetch, "fetch_public", fake_fetch)
    return answer


@pytest.fixture
def warned(graph: FakeGraph) -> FakeGraph:
    """A graph that warns about the page, with a headline and a quote that use a word the showcase never shows."""
    graph.result = {
        "score": 12,
        "explanation": "Looks like a scam: fake discount.",
        "verdict": verdict(headline="Many warning signs: this looks like a scam."),
        "assessment": ASSESSMENT,
        "facts": PageFacts(title="LED face mask, professional"),
        "skill_results": [],
    }
    return graph


class AppBridge(httpx.BaseTransport):
    """Lets the CLI's synchronous client (run in a thread) call the app through the test's async client."""

    def __init__(self, client: httpx.AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        self.client, self.loop = client, loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def send() -> httpx.Response:
            forwarded = httpx.Request(request.method, request.url, headers=request.headers, content=request.read())
            response = await self.client.send(forwarded)
            await response.aread()
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        return asyncio.run_coroutine_threadsafe(send(), self.loop).result(timeout=30)


@pytest.fixture
def cli(client: httpx.AsyncClient) -> Callable[..., Awaitable[int]]:
    """Run a CLI command against the app: ``await cli("add", URL)`` returns the exit status."""

    async def _run(*argv: str) -> int:
        api = httpx.Client(
            transport=AppBridge(client, asyncio.get_running_loop()), base_url="http://test", headers=ADMIN
        )
        with api:
            return await asyncio.to_thread(run, api, list(argv))

    return _run


async def test_the_migration_creates_a_table_without_any_account_link(pool: asyncpg.Pool) -> None:
    columns = {
        r["column_name"]
        for r in await pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'showcase_cases'"
        )
    }
    assert columns == {
        "id",
        "origin",
        "title",
        "host",
        "product_url",
        "checked_at",
        "response",
        "screenshot",
        "screenshot_type",
        "status",
        "published_at",
        "removed_at",
        "removed_reason",
    }
    with pytest.raises(asyncpg.CheckViolationError):  # nothing can be published without a date
        await pool.execute(
            "INSERT INTO showcase_cases (origin, title, host, product_url, checked_at, response, status) "
            "VALUES ('operator', 't', 'h', 'https://h/x', NOW(), '{}', 'published')"
        )


async def test_add_review_publish_and_remove_with_the_cli(  # noqa: PLR0917 - one case's whole life
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    warned: FakeGraph,
    shop: dict[str, Any],
    cli: Callable[..., Awaitable[int]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    assert await cli("add", URL) == 0

    out = capsys.readouterr().out
    assert "Draft #1 added" in out and "Unfret would warn you about this listing" in out
    assert "Left out of the public page" in out  # the headline and the quote
    assert shop["calls"] == ["https://shop.example/products/led-mask"]  # fetched by the server, fragment dropped
    assert warned.calls[0]["page_content"] == PRODUCT_HTML and warned.calls[0]["language"] == "en"
    row = await pool.fetchrow("SELECT * FROM showcase_cases")
    assert row["status"] == "draft" and row["origin"] == "operator"
    assert row["title"] == "LED face mask, professional" and row["host"] == "shop.example"
    stored = json.loads(row["response"])
    assert stored["score"] == 12 and stored["verdict"]["band"] == "junk_or_scam"  # what a paying user gets
    assert not {"report_token", "balance_usd_micros", "charged"} & stored.keys()  # nothing of an account

    # A draft is nowhere public.
    assert (await client.get("/v1/showcase")).json() == {"items": []}
    assert (await client.get("/showcase/1")).status_code == 404
    assert (await client.get("/v1/showcase/1")).status_code == 404

    (tmp_path / "shot.png").write_bytes(PNG)
    assert await cli("screenshot", "1", str(tmp_path / "shot.png")) == 0
    assert (await client.get("/showcase/1/screenshot")).status_code == 404  # not before it's published
    assert await cli("publish", "1") == 0
    assert await cli("publish", "1") == 1  # only a draft

    listed = await client.get("/v1/showcase")
    assert listed.headers["cache-control"] == "public, max-age=300"
    [item] = listed.json()["items"]
    assert item["band"] == "warning" and item["symbol"] == "✕"
    assert item["label"] == "Unfret would warn you about this listing"
    assert item["page_url"] == "/showcase/1" and item["screenshot_url"] == "/showcase/1/screenshot"
    assert "score" not in json.dumps(listed.json())

    page = await client.get("/showcase/1")
    assert page.status_code == 200
    assert "default-src 'none'" in page.headers["content-security-policy"]
    text = page.text
    assert "LED face mask, professional" in text and "shop.example" in text
    assert '<a href="https://shop.example/products/led-mask" rel="nofollow noopener">' in text
    assert "the page may have changed since" in text and "Checked on " in text
    assert "Shop owner and think this is wrong?" in text
    assert '<img src="/showcase/1/screenshot"' in text
    assert "A discount that was never real" in text  # the findings, for the shared renderer
    assert "scam" not in text.lower()
    case = (await client.get("/v1/showcase/1")).json()
    assert "scam" not in json.dumps(case).lower()
    shot = await client.get("/showcase/1/screenshot")
    assert shot.content == PNG and shot.headers["content-type"] == "image/png"

    assert await cli("remove", "1", "--reason", "The shop owner asked") == 0
    assert (await client.get("/v1/showcase")).json() == {"items": []}
    assert (await client.get("/showcase/1")).status_code == 404
    assert (await client.get("/showcase/1/screenshot")).status_code == 404
    removed = await pool.fetchrow("SELECT status, removed_reason, screenshot FROM showcase_cases WHERE id = 1")
    assert dict(removed) == {"status": "removed", "removed_reason": "The shop owner asked", "screenshot": None}
    assert await cli("publish", "1") == 1  # removed for good


async def test_only_published_cases_are_listed_newest_first(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, shop: dict[str, Any]
) -> None:
    for n in range(4):
        response = await client.post(
            "/v1/admin/showcase", json={"url": f"https://shop.example/p/{n}", "title": f"Item {n}"}, headers=ADMIN
        )
        assert response.status_code == 201, response.text
    for case_id in (1, 2, 3):
        assert (await client.post(f"/v1/admin/showcase/{case_id}/publish", headers=ADMIN)).status_code == 200
    removed = await client.post("/v1/admin/showcase/2/remove", json={"reason": "wrong page"}, headers=ADMIN)
    assert removed.json()["status"] == "removed"

    items = (await client.get("/v1/showcase")).json()["items"]
    assert [i["id"] for i in items] == [3, 1]  # 4 is a draft, 2 was removed
    assert [i["id"] for i in (await client.get("/v1/showcase?limit=1")).json()["items"]] == [3]
    assert (await client.get("/v1/showcase?limit=0")).status_code == 422
    drafts = (await client.get("/v1/admin/showcase?status=draft", headers=ADMIN)).json()["items"]
    assert [d["id"] for d in drafts] == [4]


async def test_the_admin_api_needs_the_operator_token(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    assert (await client.post("/v1/admin/showcase", json={"url": URL})).status_code == 401
    assert (await client.get("/v1/admin/showcase")).status_code == 401


async def test_a_page_that_cant_be_read_or_assessed_is_no_case(
    client: httpx.AsyncClient, pool: asyncpg.Pool, graph: FakeGraph, shop: dict[str, Any]
) -> None:
    shop["body"] = b"<html><title>Just a moment...</title></html>"
    blocked = await client.post("/v1/admin/showcase", json={"url": URL}, headers=ADMIN)
    assert blocked.status_code == 422 and "PAGE_BLOCKED" in blocked.json()["detail"]

    shop["body"] = PRODUCT_HTML.encode()
    graph.result = {"score": -1, "explanation": "Not a product page.", "skill_results": []}
    article = await client.post("/v1/admin/showcase", json={"url": URL}, headers=ADMIN)
    assert article.status_code == 422

    bad = await client.post("/v1/admin/showcase", json={"url": "ftp://shop.example/x"}, headers=ADMIN)
    assert bad.status_code == 422
    assert await pool.fetchval("SELECT COUNT(*) FROM showcase_cases") == 0


async def test_a_screenshot_must_be_a_small_image(
    client: httpx.AsyncClient, pool: asyncpg.Pool, warned: FakeGraph, shop: dict[str, Any]
) -> None:
    await client.post("/v1/admin/showcase", json={"url": URL}, headers=ADMIN)

    not_image = await client.put("/v1/admin/showcase/1/screenshot", content=b"<svg/>", headers=ADMIN)
    too_big = await client.put(
        "/v1/admin/showcase/1/screenshot", content=PNG + b"\x00" * MAX_SCREENSHOT_BYTES, headers=ADMIN
    )
    webp = await client.put("/v1/admin/showcase/1/screenshot", content=b"RIFF\x00\x00\x00\x00WEBPVP8 ", headers=ADMIN)

    assert not_image.status_code == 422 and too_big.status_code == 422
    assert webp.json()["screenshot"]["type"] == "image/webp"


async def test_a_promoted_report_is_a_fresh_check_without_any_account_link(
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    warned: FakeGraph,
    shop: dict[str, Any],
    wallet: Wallet,
) -> None:
    # A shopper checks a page from the extension and reports the result.
    address, headers = await wallet(10 * PRICE)
    body = {"url": "https://shop.example/products/led-mask?utm_source=x&session=42", "page_content": PRODUCT_HTML}
    result = (await client.post("/v1/analyze", json=body, headers=headers)).json()
    reported = await client.post(
        "/v1/reports",
        json={
            "report_token": result["report_token"],
            "reason": "scam",
            "consent": True,
            "request": body,
            "explanation": result["explanation"],
        },
        headers=headers,
    )
    assert reported.status_code == 201, reported.text
    [item] = (await client.get("/v1/admin/reports", headers=ADMIN)).json()["items"]
    labelled = await client.post(f"/v1/admin/reports/{item['id']}/label", json={"label": "scam"}, headers=ADMIN)
    assert labelled.json()["gap"] == 0  # the model was right: `make reports` offers the promotion

    # `make reports` promotes it: only the (scrubbed) address goes to the server, which checks the page again.
    bridge = AppBridge(client, asyncio.get_running_loop())
    with httpx.Client(transport=bridge, base_url="http://test", headers=ADMIN) as api:
        promoted = await asyncio.to_thread(promote, api, item["payload"]["url"])

    assert promoted.status_code == 201, promoted.text
    assert shop["calls"] == ["https://shop.example/products/led-mask"]  # fetched afresh, no query string
    row = dict(await pool.fetchrow("SELECT * FROM showcase_cases"))
    assert row["origin"] == "report" and row["status"] == "draft"
    everything = json.dumps(row, default=str)
    assert address not in everything and result["report_token"] not in everything
    assert "utm_source" not in everything and "session" not in everything
    assert await pool.fetchval("SELECT COUNT(*) FROM reports") == 0  # the report is gone, as always
