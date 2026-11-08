"""The MCP client's payment transactions must pass the server's verification (and be co-signable)."""

import base64
import json
import sys
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from tests.unit.x402_helpers import FakeRpc
from veridex.x402.payment import confirm_by_simulation, precheck
from veridex.x402.solana import CAIP2
from veridex.x402.verdict import QUICK_SKILLS
from veridex.x402.wallet import HotWallet

# The MCP package lives in mcp/ with its own environment; its payment module needs only solders and httpx.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp" / "src"))
from veridex_mcp.payer import build_payment, choose_requirement  # noqa: E402

from veridex.x402.payment import requirements  # noqa: E402

HOT = Keypair()
AGENT = Keypair()
COLD = Keypair().pubkey()


async def test_mcp_payment_passes_server_verification() -> None:
    issued = requirements(network=CAIP2["solana-devnet"], amount=100_000, pay_to=HOT.pubkey(), fee_payer=HOT.pubkey())
    chosen = choose_requirement({"accepts": [issued]}, CAIP2["solana-devnet"])
    tx = build_payment(chosen, AGENT, "EZ3rST5dvHmbanh75jc4PuLfV96vp9fEYBVeNk4FfM1k")
    header = base64.b64encode(
        json.dumps(
            {"x402Version": 2, "accepted": chosen, "payload": {"transaction": base64.b64encode(bytes(tx)).decode()}}
        ).encode()
    ).decode()

    pre = precheck(header, issued, fee_payer=HOT.pubkey(), max_compute_units=400_000, max_compute_unit_price=50_000)
    payment = await confirm_by_simulation(pre, rpc=FakeRpc(), pay_to=HOT.pubkey())
    signed = VersionedTransaction.from_bytes(HotWallet(HOT, COLD).sign(payment))

    assert payment.payer == AGENT.pubkey()
    assert signed.signatures[0].verify(HOT.pubkey(), to_bytes_versioned(signed.message))
    assert QUICK_SKILLS  # the tiers the MCP README advertises exist
