"""
Nothing that names the page a shopper or agent checked is kept or logged: the real analysis graph and the real
Postgres stores, with only the LLM, the embeddings and outside lookups stubbed, then every table dumped as text.

By design the database may hold facts about a shop (its domain, in ``shops``, ``shop_links`` and the fact cache),
never the page's address (host + path) or its query string; logs hold neither.
"""

import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel
from solders.keypair import Keypair
from structlog.testing import capture_logs

from tests.unit.x402_helpers import (
    FakeRpc,
    configured,
    expected,
    make_x402,
    payment_header,
    payment_instructions,
    sign_payment,
)
from unfret import llm, scoring, shop_store
from unfret.config import settings
from unfret.graph import SKILLS, build_graph, nodes, triage
from unfret.graph.skills import content, reverse_image, reviews
from unfret.main import app, use_postgres_stores
from unfret.x402.verdict import skills_for

HOST = "privacy-probe.shop"
SLUG = "tree-runner-probe-7f3a"
QUERY = "ref=probe-share-91c"
URL = f"https://{HOST}/products/{SLUG}?{QUERY}"
# The page names its own address (canonical link, og:url, JSON-LD), as real shop pages do.
PAGE = (
    (Path(__file__).parents[1] / "fixtures" / "pages" / "shopify-productgroup-us.html")
    .read_text()
    .replace("www.allbirds.com", HOST)
    .replace("mens-tree-runners", SLUG)
)
NEEDLES = (f"{HOST}/products/{SLUG}", SLUG, QUERY)
_LOAD_MODEL = scoring.load_model  # the real one (tests/conftest.py stubs it per test)

ANSWERS: dict[type[BaseModel], BaseModel] = {
    nodes._ClassifyOutput: nodes._ClassifyOutput(page_type="storefront", platform="", reasoning="A shop."),
    nodes._JudgeOutput: nodes._JudgeOutput(score=40, explanation="Some concerns."),
    nodes._Explanation: nodes._Explanation(explanation="Some concerns."),
    triage._TriageAnswer: triage._TriageAnswer(run_reverse_image=True, reason="Generic product."),
    content._Signals: content._Signals(
        scam_copy=0.2,
        generic_product=0.6,
        brand_recognized=False,
        implausible_claims=0.1,
        missing_trust_info=0.5,
        addresses_the_reader_ai=False,
        evidence=["Tree Runner"],
    ),
}


class FakeChatModel:
    """Every structured LLM call (classify, content read, triage, judge, explanation) gets a canned answer."""

    def __init__(self) -> None:
        self.schemas: list[type[BaseModel]] = []

    def with_structured_output(self, schema: type[BaseModel], **_: Any) -> RunnableLambda:
        def answer(messages: Any) -> BaseModel:
            self.schemas.append(schema)
            return ANSWERS[schema]

        return RunnableLambda(answer)


@pytest.fixture
def outside(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Stub the LLM and the embeddings; the network answers only the page itself (for URL-only checks).

    :return: The URLs the analysis tried to fetch.
    """
    fetched: list[str] = []

    async def fetch_public(url: str, **_: Any) -> bytes:
        fetched.append(url)
        if url == URL:
            return PAGE.encode()
        raise httpx.ConnectError("no network in tests")

    for name, module in list(sys.modules.items()):
        if name.startswith("unfret") and hasattr(module, "fetch_public"):
            monkeypatch.setattr(module, "fetch_public", fetch_public)

    async def no_network(*_: Any, **__: Any) -> httpx.Response:
        raise httpx.ConnectError("no network in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)  # e.g. VIES
    chat = FakeChatModel()
    monkeypatch.setattr(llm, "get_llm", lambda: chat)
    monkeypatch.setattr(reviews, "get_embeddings", lambda: DeterministicFakeEmbedding(size=16))
    # The real scorers and stores, not the unit tests' defaults; the module stores are put back afterwards.
    monkeypatch.setattr(scoring, "load_model", _LOAD_MODEL)
    for module in (shop_store, reverse_image):
        monkeypatch.setattr(module, "_store", module._store)
    for module_name in ("fact_cache", "price_store", "verdict_store"):
        module = sys.modules[f"unfret.{module_name}"]
        monkeypatch.setattr(module, "_store", module._store)
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(settings, "price_observations", True)
    return fetched


async def dump(pool: asyncpg.Pool) -> dict[str, str]:
    """Every row of every table, as text."""
    tables = [r["tablename"] for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    return {t: "\n".join(r[0] for r in await pool.fetch(f'SELECT t::text FROM "{t}" t')) for t in tables}  # noqa: S608


async def assert_nothing_names_the_page(pool: asyncpg.Pool, logs: list[Any], records: list[Any]) -> None:
    await shop_store.drain()  # fingerprints and image prints are written in the background
    await reverse_image.drain()
    rows = await dump(pool)
    assert rows["shops"], "the analysis should have recorded the shop (else this test checks nothing)"
    for needle in NEEDLES:
        for text in (needle, needle.encode().hex()):  # bytea columns print as hex
            assert [t for t, data in rows.items() if text in data] == [], f"{needle!r} is stored"
        assert [e for e in logs if needle in str(e)] == [], f"{needle!r} is logged"
        assert [r for r in records if needle in r.getMessage()] == [], f"{needle!r} is logged"


@pytest.fixture
async def stores(pool: asyncpg.Pool, outside: list[str]) -> asyncpg.Pool:
    use_postgres_stores(pool)
    return pool


async def test_an_analysis_keeps_nothing_that_names_the_page(
    client: httpx.AsyncClient,
    stores: asyncpg.Pool,
    wallet: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(app, "graph", build_graph(SKILLS))
    _, headers = await wallet(settings.analysis_price_usd_micros)
    caplog.set_level(logging.DEBUG)

    with capture_logs() as logs:
        response = await client.post("/v1/analyze", json={"page_content": PAGE, "url": URL}, headers=headers)
        await assert_nothing_names_the_page(stores, logs, caplog.records)

    assert response.status_code == 200
    assert response.json()["charged"] is True
    assert 0 <= response.json()["score"] <= 100  # assessed, so every store had its chance to write


@pytest.fixture
async def x402_client(stores: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    hot = Keypair()
    rpc = FakeRpc()
    monkeypatch.setattr(app, "x402", make_x402(configured(hot, Keypair().pubkey()), rpc))
    monkeypatch.setattr(app, "db_pool", stores)
    monkeypatch.setattr(app, "graph", build_graph(SKILLS))
    monkeypatch.setattr(
        app,
        "quick_graph",
        build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.hot = hot  # type: ignore[attr-defined]
        c.rpc = rpc  # type: ignore[attr-defined]
        yield c


@pytest.mark.parametrize(("tier", "lamports"), [("quick", 100_000), ("deep", 1_500_000)])
async def test_a_paid_check_keeps_nothing_that_names_the_page(
    *,
    x402_client: httpx.AsyncClient,
    stores: asyncpg.Pool,
    outside: list[str],
    caplog: pytest.LogCaptureFixture,
    tier: str,
    lamports: int,
) -> None:
    hot: Keypair = x402_client.hot  # type: ignore[attr-defined]
    x402_client.rpc.sim_delta = lamports  # type: ignore[attr-defined]
    payer = Keypair()
    tx = sign_payment(payment_instructions(payer.pubkey(), hot.pubkey(), lamports), hot.pubkey(), [payer])
    header = payment_header(tx, expected(hot.pubkey(), lamports))
    caplog.set_level(logging.DEBUG)

    with capture_logs() as logs:
        # URL only: the server fetches the page, and the verdict is kept for the listing (by a key, not the URL).
        response = await x402_client.post(
            "/v1/check", json={"url": URL, "tier": tier}, headers={"PAYMENT-SIGNATURE": header}
        )
        await assert_nothing_names_the_page(stores, logs, caplog.records)

    assert response.status_code == 200, response.text
    assert response.json()["tier"] == tier
    assert outside[0] == URL
    assert await stores.fetchval("SELECT COUNT(*) FROM listing_verdicts") == 1  # it was kept, without the URL
