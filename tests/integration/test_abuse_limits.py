"""Limits per visitor network on the cheap, unauthenticated or new-account routes that cost us something."""

import httpx
from solders.keypair import Keypair

from unfret.config import settings


async def test_price_quotes_are_limited_per_visitor_network(client: httpx.AsyncClient) -> None:
    # Each quote is a stored row and an RPC call, and needs no sign-in.
    body = {"address": str(Keypair().pubkey()), "checks": 25, "country": "NL"}
    for _ in range(settings.quotes_per_ip_per_minute):
        assert (await client.post("/v1/credits/purchase-tx", json=body)).status_code != 429
    refused = await client.get("/v1/credits/solana-pay.svg", params={"checks": 25, "country": "NL"})
    assert (refused.status_code, refused.json()["error_code"]) == (429, "TOO_MANY_REQUESTS")
