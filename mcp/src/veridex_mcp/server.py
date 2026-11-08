"""
MCP server exposing Veridex's pay-per-check store risk API as tools for AI shopping agents.

Configuration (environment):
    VERIDEX_API_URL                   default https://veridex.kroshtan.com
    VERIDEX_MCP_KEYPAIR               path to the agent's Solana CLI keypair JSON (or VERIDEX_MCP_SECRET_KEY, base58)
    VERIDEX_MCP_NETWORK               solana-devnet (default) or solana-mainnet
    VERIDEX_MCP_RPC_URL               defaults to the public RPC of that network
    VERIDEX_MCP_MAX_PRICE_LAMPORTS    refuse any single check above this (default 2_000_000 = 0.002 SOL)
    VERIDEX_MCP_SESSION_CAP_LAMPORTS  refuse once a session has spent this (default 20_000_000 = 0.02 SOL)
"""

import json
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from solders.keypair import Keypair

from veridex_mcp.explain import explain
from veridex_mcp.payer import Payer, PaymentFailedError, SpendGuard, SpendRefusedError
from veridex_mcp.solana import Rpc

_DEFAULT_RPC = {
    "solana-devnet": "https://api.devnet.solana.com",
    "solana-mainnet": "https://api.mainnet-beta.solana.com",
}

server = MCPServer(
    "veridex",
    instructions=(
        "Check a store or product listing for scam, dropshipping and dark-pattern signals before buying. Each check "
        "is paid from your own Solana wallet in wSOL (quick 0.0001 SOL, deep 0.0015 SOL) within a spend cap. "
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
                max_per_call=int(os.environ.get("VERIDEX_MCP_MAX_PRICE_LAMPORTS", "2000000")),
                session_cap=int(os.environ.get("VERIDEX_MCP_SESSION_CAP_LAMPORTS", "20000000")),
            ),
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
    Check a store or product page for scam, dropshipping and dark-pattern risk. Paid per call in wSOL.

    Pass page_content (the page's HTML) for marketplaces such as Amazon, which block server-side fetches.

    :param url: The store or product page URL.
    :param tier: quick (cheap checks, 0.0001 SOL) or deep (everything incl. reverse image search, 0.0015 SOL).
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
