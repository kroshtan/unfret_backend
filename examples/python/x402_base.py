"""
Pay one Unfret check per call over x402 on Base, in USDC, with the official ``x402`` package.

The first ``POST /v1/check`` answers ``402``; the x402 client signs an EIP-3009 ``transferWithAuthorization`` of
exactly the price to Unfret's ``payTo`` (the ``exact`` scheme) and sends the request again with
``PAYMENT-SIGNATURE``. The facilitator pays the gas, so the wallet needs USDC and no ETH, and Unfret settles only once
the answer is ready: a failed check costs nothing. Run: ``EVM_PRIVATE_KEY=0x… uv run x402_base.py``.
"""

import base64
import json
import os
import sys

from eth_account import Account
from x402 import x402ClientSync
from x402.http.clients import x402_requests
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import register_exact_evm_client

PRODUCTION = os.environ.get("UNFRET_ENV") == "production"
API = os.environ.get("UNFRET_API_URL") or ("https://unfret.net" if PRODUCTION else "https://dev.unfret.net")
NETWORK = "eip155:8453" if PRODUCTION else "eip155:84532"  # Base mainnet / Base Sepolia
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" if PRODUCTION else "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PAGE = os.environ.get("UNFRET_CHECK_URL") or f"{API}/test-shop"
TIER = os.environ.get("UNFRET_TIER", "quick")  # quick: $0.01, deep: $0.20
MAX_AMOUNT = int(os.environ.get("MAX_AMOUNT", "250000"))  # refuse more than $0.25 (USDC has 6 decimals)


def main() -> int:
    """
    Run one paid check and print the verdict and the payment's receipt.

    :return: The exit status.
    """
    account = Account.from_key(os.environ["EVM_PRIVATE_KEY"])
    client = x402ClientSync()
    register_exact_evm_client(
        client,
        EthAccountSigner(account),
        networks=NETWORK,
        # Pay only Circle's USDC, and never more than MAX_AMOUNT.
        policies=[
            lambda _v, reqs: [r for r in reqs if r.asset.lower() == USDC.lower() and int(r.get_amount()) <= MAX_AMOUNT]
        ],
    )
    print(f"Paying from {account.address} in USDC on {NETWORK}")
    session = x402_requests(client)
    response = session.post(f"{API}/v1/check", json={"url": PAGE, "tier": TIER}, timeout=100)

    body = response.json()
    if receipt := response.headers.get("PAYMENT-RESPONSE"):
        print("Payment:", json.loads(base64.b64decode(receipt)))  # success, transaction, network, payer
    if response.status_code != 200:
        # 402 again: the facilitator refused the authorization (e.g. not enough USDC); nothing was paid.
        print(f"HTTP {response.status_code} {body.get('error_code')}: {body.get('detail') or body.get('error')}")
        if reason := body.get("reason"):
            print(f"reason: {reason}")
        return 1
    print(f"{body['action'].upper()} ({body['band']}, risk {body['risk_score']:.2f}) for {PAGE}")
    for flag in body["flags"]:
        print(f"  - {flag['type']}: {flag['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
