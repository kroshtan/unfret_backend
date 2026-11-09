"""One setting, every route: changing the price of a check moves every price the service shows or charges."""

from decimal import Decimal
from typing import Any

import httpx
import pytest
from solders.keypair import Keypair

from tests.integration.test_credits import shop  # noqa: F401 - the fixture
from veridex.config import settings

PAGE = {"page_content": "<h1>Earbuds</h1>", "url": "https://shop.test/p/1"}


@pytest.mark.usefixtures("shop")
@pytest.mark.parametrize("price", ["0.25", "0.20"])
async def test_every_sales_route_follows_the_price_of_a_check(
    client: httpx.AsyncClient, wallet: Any, monkeypatch: pytest.MonkeyPatch, price: str
) -> None:
    monkeypatch.setattr(settings, "price_per_check_usd", Decimal(price))
    check = int(Decimal(price) * 1_000_000)

    # The price list (also what the MCP server's get_pricing and the checkout page show).
    listed = (await client.get("/v1/pricing")).json()
    assert listed["price_per_check_usd_micros"] == listed["analysis_usd_micros"] == check
    assert listed["x402"]["deep_usd_micros"] == check
    assert [p["crypto_usd_micros"] for p in listed["packs"]] == [25 * check, 50 * check, 100 * check]

    # x402 discovery and llms.txt (agents).
    discovery = (await client.get("/.well-known/x402")).json()
    assert discovery["tiers"]["deep"]["price_usd_micros"] == check
    assert f"deep = ${Decimal(check) / 1_000_000:.2f}" in (await client.get("/llms.txt")).text

    # A crypto purchase: the pack costs checks × price (USDC, outside the EU: no VAT).
    body = {"address": str(Keypair().pubkey()), "checks": 50, "country": "US", "currency": "USDC"}
    assert (await client.post("/v1/credits/purchase-tx", json=body)).json()["amount"] == 50 * check

    # An analysis costs one check, and the balance counts checks at that price.
    address, headers = await wallet(3 * check)
    assert (await client.get("/v1/credits", headers=headers)).json()["checks_remaining"] == 3
    assert (await client.post("/v1/analyze", json=PAGE, headers=headers)).json()["balance_usd_micros"] == 2 * check
