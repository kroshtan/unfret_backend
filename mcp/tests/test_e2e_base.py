"""
A real quick check on dev, paid by this package in USDC on Base Sepolia. Skipped unless ``UNFRET_E2E_BASE_PAYER_KEY``
is set: a Base Sepolia key holding at least $0.01 of Circle's test USDC (faucet.circle.com), never a mainnet key. The
same variable the backend's ``make e2e-base`` reads. ``UNFRET_E2E_URL`` (default https://dev.unfret.net) and
``UNFRET_E2E_BASE_RPC_URL`` (default Base Sepolia's public RPC) are optional. Nothing here can pay on Base mainnet:
the wallet is configured for Base Sepolia only.

Run: ``UNFRET_E2E_BASE_PAYER_KEY=… uv run --project mcp pytest mcp/tests/test_e2e_base.py -v``
"""

import os

import httpx
import pytest

from unfret_mcp.base import CAIP2, BaseWallet
from unfret_mcp.payer import Payer, SpendGuard
from unfret_mcp.solana import Rpc

KEY = os.environ.get("UNFRET_E2E_BASE_PAYER_KEY", "").strip()
pytestmark = pytest.mark.skipif(not KEY, reason="UNFRET_E2E_BASE_PAYER_KEY is not set (a funded Base Sepolia key)")

SEPOLIA = CAIP2["base-sepolia"]
BODY = {"url": "https://example.com/", "page_content": "<h1>Wireless earbuds</h1><p>$19.99</p>", "tier": "quick"}


async def _usdc_balance(rpc_url: str, token: str, owner: str) -> int:
    data = "0x70a08231" + owner.lower().removeprefix("0x").rjust(64, "0")  # balanceOf(address)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": token, "data": data}, "latest"]},
        )
    return int(response.json()["result"], 16)


async def test_a_quick_check_on_dev_paid_in_usdc_on_base_sepolia() -> None:
    api_url = (os.environ.get("UNFRET_E2E_URL") or "https://dev.unfret.net").rstrip("/")
    rpc_url = os.environ.get("UNFRET_E2E_BASE_RPC_URL") or "https://sepolia.base.org"
    wallet = BaseWallet(KEY, "base-sepolia")
    async with httpx.AsyncClient(timeout=30) as client:
        discovery = (await client.get(f"{api_url}/.well-known/x402")).json()
    networks = {n.get("network") for n in discovery.get("networks", [])}
    assert CAIP2["base-mainnet"] not in networks, "refusing: the target offers Base mainnet"
    if SEPOLIA not in networks:
        pytest.skip(f"{api_url} doesn't offer USDC on Base Sepolia (x402_base off)")
    before = await _usdc_balance(rpc_url, wallet.usdc, wallet.address)
    if before < 10_000:
        pytest.fail(f"fund {wallet.address} with Base Sepolia USDC (has {before} units)")

    spend = SpendGuard(max_per_call=50_000, session_cap=50_000)
    payer = Payer(
        api_url=api_url,
        agent=None,
        rpc=Rpc("http://solana.invalid"),  # unused: no Solana wallet
        network="solana-devnet",
        guard=spend,
        base=wallet,
        prefer_network="base",
    )
    result = await payer.check(BODY)

    assert result["status"] == "ok", result
    assert result["network"] == SEPOLIA
    assert result["transaction"] and str(result["transaction"]).startswith("0x")
    assert result["verdict"]["action"] in {"allow", "warn", "block"}
    paid = result["paid"]
    assert paid["currency"] == "USDC" and paid["amount"] == paid["usd_micros"] == spend.spent
    assert KEY.removeprefix("0x") not in repr(result)
