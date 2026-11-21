"""
The test shop never reaches what Unfret keeps about shops: fingerprints, prices, reviews, photos, verdicts, reports.

The real analysis graph and the real Postgres stores (as in ``test_privacy``), with the LLM, the embeddings and outside
lookups stubbed. A real shop's page, analysed the same way, is the control: it does get recorded.
"""

import sys
from typing import Any

import asyncpg
import httpx
import pytest
from solders.keypair import Keypair

from tests.conftest import page_fetcher
from tests.integration.test_privacy import PAGE, URL, dump, outside, stores, x402_client  # noqa: F401 - fixtures
from tests.unit.x402_helpers import expected, payment_header, payment_instructions, sign_payment
from unfret import reports, shop_store
from unfret.config import settings
from unfret.graph import SKILLS, build_graph
from unfret.graph.skills import reverse_image
from unfret.main import app
from unfret.routes.test_shop import render
from unfret.schemas.requests import AnalyzeRequest

TEST_SHOP = "https://unfret.net/test-shop"
# Everything that learns or keeps facts about shops, products, reviews, photos and verdicts, and the review queue.
SHOP_DATA = (
    "shops",
    "shop_links",
    "price_observations",
    "market_prices",
    "review_fingerprints",
    "image_prints",
    "listing_verdicts",
    "reports",
    "labelled_pages",
)


async def kept(pool: asyncpg.Pool) -> dict[str, str]:
    await shop_store.drain()
    await reverse_image.drain()
    rows = await dump(pool)
    return {table: rows[table] for table in SHOP_DATA if rows.get(table)}


async def test_an_analysis_of_the_test_shop_keeps_nothing(
    client: httpx.AsyncClient,
    stores: asyncpg.Pool,  # noqa: F811
    wallet: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app, "graph", build_graph(SKILLS))
    _, headers = await wallet(2 * settings.analysis_price_usd_micros)

    response = await client.post(
        "/v1/analyze", json={"page_content": render("product.html"), "url": TEST_SHOP}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["page_type"] == "storefront" and response.json()["charged"] is True
    assert await kept(stores) == {}

    control = await client.post("/v1/analyze", json={"page_content": PAGE, "url": URL}, headers=headers)
    assert control.status_code == 200
    assert "shops" in await kept(stores), "a real shop is recorded (else this test checks nothing)"


@pytest.mark.parametrize(("tier", "lamports"), [("quick", 100_000), ("deep", 1_500_000)])
async def test_a_paid_check_of_the_test_shop_keeps_nothing(
    *,
    x402_client: httpx.AsyncClient,  # noqa: F811
    stores: asyncpg.Pool,  # noqa: F811
    outside: list[str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    lamports: int,
) -> None:
    async def fetch_public(url: str, **_: Any) -> bytes:
        outside.append(url)
        if url == TEST_SHOP:
            return render("product.html").encode()
        raise httpx.ConnectError("no network in tests")

    for name, module in list(sys.modules.items()):
        if name.startswith("unfret") and hasattr(module, "fetch_public"):
            monkeypatch.setattr(module, "fetch_public", fetch_public)
        if name.startswith("unfret") and hasattr(module, "fetch_public_page"):
            monkeypatch.setattr(module, "fetch_public_page", page_fetcher(fetch_public))
    hot: Keypair = x402_client.hot  # type: ignore[attr-defined]
    x402_client.rpc.sim_delta = lamports  # type: ignore[attr-defined]
    payer = Keypair()
    tx = sign_payment(payment_instructions(payer.pubkey(), hot.pubkey(), lamports), hot.pubkey(), [payer])
    header = payment_header(tx, expected(hot.pubkey(), lamports))

    # URL only: the server fetches the page, as an agent's check does (and a verdict would be kept for the listing).
    response = await x402_client.post(
        "/v1/check", json={"url": TEST_SHOP, "tier": tier}, headers={"PAYMENT-SIGNATURE": header}
    )

    assert response.status_code == 200, response.text
    assert outside[0] == TEST_SHOP
    assert response.json()["band"] == "junk_or_scam"
    assert await kept(stores) == {}


async def test_a_report_of_the_test_shop_is_never_queued(stores: asyncpg.Pool) -> None:  # noqa: F811
    body = AnalyzeRequest(url=TEST_SHOP, page_content=render("product.html"))

    done = await reports.submit(
        stores,
        settings,
        account="reporter",
        claim=reports.Claim(account="reporter", score=3),
        body=body,
        reason="real_brand",
        explanation="Fake countdown.",
        verdict=None,
    )

    assert (done.outcome, done.used) == ("already_known", 0)
    assert await kept(stores) == {}
    assert not await stores.fetchval("SELECT COUNT(*) FROM report_quota")
