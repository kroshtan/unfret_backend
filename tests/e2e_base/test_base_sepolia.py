"""
x402 on Base against Base Sepolia (``make e2e-base``; marker ``e2e_base``, so the dev sweep and its production gate
never select it). Each test skips without its environment; nothing here touches Base mainnet.

``UNFRET_E2E_BASE_FACILITATOR_URL`` (with ``UNFRET_E2E_CDP_API_KEY_ID`` and ``UNFRET_E2E_CDP_API_KEY_SECRET`` for
Coinbase's facilitator): Unfret's facilitator client verifies a payment signed by a throwaway key made here, which
holds nothing. The facilitator must get past the signature to the balance: the request, the EIP-712 domain and the
signature are all as the facilitator expects, and nothing can move.

``UNFRET_E2E_BASE_PAYER_KEY`` (a Base Sepolia key holding test USDC, at least $0.02; never a mainnet key): a quick
check on dev (``UNFRET_E2E_URL``) paid in USDC on Base Sepolia, confirmed on chain, and answered again, without a
second charge, for the same payment.
"""

import asyncio
import os
from typing import Any

import httpx
import pytest
from eth_account import Account

from tests.unit.x402_base_helpers import authorization
from unfret.config import Settings
from unfret.x402.base import BaseX402
from unfret.x402.evm import CAIP2, PUBLIC_RPC, TRANSFER_TOPIC, USDC, BaseRpc, address_from_topic, word
from unfret.x402.facilitator import Facilitator, facilitator_auth
from unfret.x402.payment import PaymentError, decode_header, encode_header

pytestmark = pytest.mark.e2e_base

SEPOLIA = CAIP2["base-sepolia"]
BODY = {"url": "https://example.com/", "page_content": "<h1>Wireless earbuds</h1><p>$19.99</p>", "tier": "quick"}


def env(name: str) -> str:
    return os.environ.get(f"UNFRET_E2E_{name}", "").strip()


async def test_the_facilitator_takes_unfrets_payment_shape() -> None:
    url = env("BASE_FACILITATOR_URL")
    if not url:
        pytest.skip("UNFRET_E2E_BASE_FACILITATOR_URL is not set")
    auth = facilitator_auth(url, env("CDP_API_KEY_ID"), env("CDP_API_KEY_SECRET"))
    pay_to = Account.create()  # throwaway: nothing will reach it
    settings = Settings(x402_base_enabled=True, base_pay_to=pay_to.address, base_facilitator_url=url)
    base = BaseX402(settings, rpc=BaseRpc(PUBLIC_RPC["base-sepolia"]), facilitator=Facilitator(url, auth))
    option = base.requirements("quick")
    auth_fields, signature = authorization(Account.create(), to=pay_to.address, value=int(option["amount"]))
    payload = {"x402Version": 2, "accepted": option, "payload": {"signature": signature, "authorization": auth_fields}}
    pre = base.precheck(payload, [option])
    resource = {"url": "https://dev.unfret.net/v1/check", "description": "e2e", "mimeType": "application/json"}
    with pytest.raises(PaymentError) as refused:
        await base.verify(pre, resource, {})
    assert "balance" in refused.value.reason or "insufficient" in refused.value.reason, refused.value.reason
    await base.aclose()


async def test_a_paid_check_on_dev_in_usdc_on_base_sepolia() -> None:
    key = env("BASE_PAYER_KEY")
    if not key:
        pytest.skip("UNFRET_E2E_BASE_PAYER_KEY is not set")
    payer = Account.from_key(key)
    base_url = (env("URL") or "https://dev.unfret.net").rstrip("/")
    rpc = BaseRpc(env("BASE_RPC_URL") or PUBLIC_RPC["base-sepolia"])
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(150, connect=15)) as api:
        discovery = (await api.get("/.well-known/x402")).json()
        networks = {n.get("network") for n in discovery.get("networks", [])}
        assert "eip155:8453" not in networks, "refusing: the target offers Base mainnet"
        offer = await api.post("/v1/check", json=BODY)
        assert offer.status_code == 402, offer.text
        options = [o for o in decode_header(offer.headers["PAYMENT-REQUIRED"])["accepts"] if o["network"] == SEPOLIA]
        if not options:
            pytest.skip("the target doesn't offer USDC on Base Sepolia (x402_base_enabled off)")
        (option,) = options
        assert option["asset"].lower() == USDC["base-sepolia"].lower()
        balance = await rpc.token_balance(USDC["base-sepolia"], payer.address)
        if balance < int(option["amount"]):
            pytest.fail(f"fund {payer.address} with Base Sepolia USDC (has {balance} units)")

        fields, signature = authorization(payer, to=option["payTo"], value=int(option["amount"]))
        header = encode_header(
            {"x402Version": 2, "accepted": option, "payload": {"signature": signature, "authorization": fields}}
        )
        paid = await api.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
        assert paid.status_code == 200, paid.text
        receipt = decode_header(paid.headers["PAYMENT-RESPONSE"])
        assert (receipt["success"], receipt["network"]) == (True, SEPOLIA)

        on_chain = await _receipt(rpc, receipt["transaction"])
        transfers = [
            log
            for log in on_chain["logs"]
            if log["topics"][0].lower() == TRANSFER_TOPIC
            and address_from_topic(log["topics"][1]) == payer.address.lower()
        ]
        assert [(address_from_topic(t["topics"][2]), word(t["data"])) for t in transfers] == [
            (option["payTo"].lower(), int(option["amount"]))
        ]

        again = await api.post("/v1/check", json=BODY, headers={"PAYMENT-SIGNATURE": header})
        assert (again.status_code, again.json()) == (200, paid.json())
        assert await rpc.token_balance(USDC["base-sepolia"], payer.address) == balance - int(option["amount"])
    await rpc.aclose()


async def _receipt(rpc: BaseRpc, transaction: str) -> dict[str, Any]:
    for _ in range(30):
        if (found := await rpc.receipt(transaction)) is not None:
            return found
        await asyncio.sleep(1)
    pytest.fail(f"no receipt for {transaction}")
