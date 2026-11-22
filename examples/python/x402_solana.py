"""
Pay one Unfret check per call over x402 on Solana, in USDC (or wSOL), with the official ``x402`` package.

The first ``POST /v1/check`` answers ``402``; the x402 client signs a ``TransferChecked`` of the price to Unfret's
``payTo`` (Unfret is the fee payer and adds its signature, so you pay only the price) and sends the request again with
``PAYMENT-SIGNATURE``. Run: ``SOLANA_KEYPAIR=~/.config/solana/id.json uv run x402_solana.py``.
"""

import base64
import json
import os
import sys
from pathlib import Path

from solders.keypair import Keypair
from x402 import x402ClientSync
from x402.http.clients import x402_requests
from x402.mechanisms.svm import KeypairSigner
from x402.mechanisms.svm.exact import register_exact_svm_client

PRODUCTION = os.environ.get("UNFRET_ENV") == "production"
API = os.environ.get("UNFRET_API_URL") or ("https://unfret.net" if PRODUCTION else "https://dev.unfret.net")
NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" if PRODUCTION else "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
MINTS = {  # what each option of the 402 is paid in
    "usdc": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    if PRODUCTION
    else "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
    "wsol": "So11111111111111111111111111111111111111112",  # wrapped SOL: wrap some first (README)
}
ASSET = os.environ.get("SOLANA_ASSET", "usdc")
PAGE = os.environ.get("UNFRET_CHECK_URL") or f"{API}/test-shop"
TIER = os.environ.get("UNFRET_TIER", "quick")  # quick: $0.01, deep: $0.20
# Refuse to pay more than this, in the asset's base units (USDC: 6 decimals, wSOL: 9).
MAX_AMOUNT = int(os.environ.get("MAX_AMOUNT") or (250_000 if ASSET == "usdc" else 3_000_000))


def load_keypair() -> Keypair:
    """
    The payer: a ``solana-keygen`` JSON file (``SOLANA_KEYPAIR``) or a base58 secret key (``SOLANA_PRIVATE_KEY``).

    :return: The keypair.
    """
    if secret := os.environ.get("SOLANA_PRIVATE_KEY"):
        return Keypair.from_base58_string(secret)
    path = Path(os.environ.get("SOLANA_KEYPAIR", "~/.config/solana/id.json")).expanduser()
    return Keypair.from_bytes(bytes(json.loads(path.read_text())))


def main() -> int:
    """
    Run one paid check and print the verdict and the payment's receipt.

    :return: The exit status.
    """
    keypair = load_keypair()
    client = x402ClientSync()
    register_exact_svm_client(
        client,
        KeypairSigner(keypair),
        networks=NETWORK,
        rpc_url=os.environ.get("SOLANA_RPC_URL"),  # default: the network's public RPC
        # Pay only in the chosen asset, and never more than MAX_AMOUNT.
        policies=[lambda _v, reqs: [r for r in reqs if r.asset == MINTS[ASSET] and int(r.get_amount()) <= MAX_AMOUNT]],
    )
    print(f"Paying from {keypair.pubkey()} in {ASSET.upper()} on {NETWORK}")
    session = x402_requests(client)
    response = session.post(f"{API}/v1/check", json={"url": PAGE, "tier": TIER}, timeout=100)

    body = response.json()
    if receipt := response.headers.get("PAYMENT-RESPONSE"):
        print("Payment:", json.loads(base64.b64decode(receipt)))  # success, transaction, network, payer
    if response.status_code != 200:
        # 402 again: the payment was refused (`error`, and `reason` when its simulation failed); nothing was paid.
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
