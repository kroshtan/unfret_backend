"""
MCP server exposing Veridex's pay-per-check store risk API as tools for AI shopping agents.

Configuration (environment):
    VERIDEX_API_URL                   default https://veridex.kroshtan.com
    VERIDEX_MCP_KEYPAIR               path to the agent's Solana CLI keypair JSON (or VERIDEX_MCP_SECRET_KEY, base58)
    VERIDEX_MCP_NETWORK               solana-devnet (default) or solana-mainnet
    VERIDEX_MCP_RPC_URL               defaults to the public RPC of that network
    VERIDEX_MCP_PAY_WITH              USDC (default: USDC when offered and held, else SOL) or SOL (always SOL)
    VERIDEX_MCP_MAX_PRICE_USD         refuse any single check worth more than this many dollars (default 0.25)
    VERIDEX_MCP_SESSION_CAP_USD       refuse once a session has spent this many dollars (default 2.50)
    VERIDEX_MCP_PRICE_RPC_URL         mainnet RPC for the agent's own Pyth SOL/USD reading, which values wSOL
                                      payments (default: mainnet's public RPC)
"""

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from solders.keypair import Keypair

from veridex_mcp.explain import explain
from veridex_mcp.payer import Payer, PaymentFailedError, SpendGuard, SpendRefusedError
from veridex_mcp.price import SolUsd
from veridex_mcp.solana import Rpc

_DEFAULT_RPC = {
    "solana-devnet": "https://api.devnet.solana.com",
    "solana-mainnet": "https://api.mainnet-beta.solana.com",
}

server = MCPServer(
    "veridex",
    instructions=(
        "Check a store or product listing for scam, dropshipping and dark-pattern signals before buying. Each check "
        "is paid from your own Solana wallet in USDC or wSOL (quick $0.01, deep $0.15; wSOL at the current SOL "
        "price) within a dollar spend cap. "
        "Act on `action`: allow = proceed, warn = ask your human first, block = don't buy."
    ),
)
_payer: Payer | None = None


def _load_agent() -> Keypair:
    if path := os.environ.get("VERIDEX_MCP_KEYPAIR"):
        return Keypair.from_bytes(bytes(json.loads(Path(path).expanduser().read_text())))
    if secret := os.environ.get("VERIDEX_MCP_SECRET_KEY"):
        return Keypair.from_base58_string(secret)
    raise RuntimeError("set VERIDEX_MCP_KEYPAIR (keypair file) or VERIDEX_MCP_SECRET_KEY")


def _usd_micros(name: str, default: str) -> int:
    return int(Decimal(os.environ.get(name, default)) * 1_000_000)


def payer() -> Payer:
    """
    Return the session's payer, configured from the environment on first use.

    :return: The payer.
    """
    global _payer  # noqa: PLW0603
    if _payer is None:
        network = os.environ.get("VERIDEX_MCP_NETWORK", "solana-devnet")
        _payer = Payer(
            api_url=os.environ.get("VERIDEX_API_URL", "https://veridex.kroshtan.com"),
            agent=_load_agent(),
            rpc=Rpc(os.environ.get("VERIDEX_MCP_RPC_URL", _DEFAULT_RPC[network])),
            network=network,
            guard=SpendGuard(
                max_per_call=_usd_micros("VERIDEX_MCP_MAX_PRICE_USD", "0.25"),
                session_cap=_usd_micros("VERIDEX_MCP_SESSION_CAP_USD", "2.50"),
            ),
            sol_usd=SolUsd(Rpc(os.environ.get("VERIDEX_MCP_PRICE_RPC_URL", _DEFAULT_RPC["solana-mainnet"]))),
            prefer="SOL" if os.environ.get("VERIDEX_MCP_PAY_WITH", "USDC").upper() == "SOL" else "USDC",
        )
    return _payer


@server.tool()
async def check_store(
    url: str,
    tier: Literal["quick", "deep"] = "quick",
    policy: Literal["strict", "balanced", "lenient"] = "balanced",
    page_content: str | None = None,
) -> dict[str, Any]:
    """
    Check a store or product page for scam, dropshipping and dark-pattern risk. Paid per call in USDC or wSOL.

    Pass page_content (the page's HTML) for marketplaces such as Amazon, which block server-side fetches.

    :param url: The store or product page URL.
    :param tier: quick (cheap checks, $0.01) or deep (everything incl. reverse image search, $0.15).
    :param policy: How cautious the returned action is: strict, balanced or lenient.
    :param page_content: The page's HTML, if available.
    :return: The verdict (action, risk_score, confidence, flags) plus the payment transaction.
    """
    body: dict[str, Any] = {"url": url, "tier": tier, "policy": policy}
    if page_content:
        body["page_content"] = page_content
    try:
        return await payer().check(body)
    except (SpendRefusedError, PaymentFailedError) as exc:
        return {"status": "not_paid", "error": str(exc)}


@server.tool()
def explain_flag(flag_type: str) -> str:
    """
    Explain what a flag in a Veridex verdict means, in one paragraph for the end user.

    :param flag_type: The flag's ``type``.
    :return: The explanation.
    """
    return explain(flag_type)


def main() -> None:
    """Run the server over stdio."""
    server.run()


if __name__ == "__main__":
    main()
