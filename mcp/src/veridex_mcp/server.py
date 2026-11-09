"""
MCP server exposing Unfret's pay-per-check store risk API as tools for AI shopping agents.

Configuration (environment; the older VERIDEX_* names still work):
    UNFRET_API_URL                    default https://unfret.kroshtan.com
    UNFRET_MCP_KEYPAIR                path to the agent's Solana CLI keypair JSON (or UNFRET_MCP_SECRET_KEY, base58)
    UNFRET_MCP_NETWORK                solana-devnet (default) or solana-mainnet
    UNFRET_MCP_RPC_URL                defaults to the public RPC of that network
    UNFRET_MCP_PAY_WITH               USDC (default: USDC when offered and held, else SOL) or SOL (always SOL)
    UNFRET_MCP_MAX_PRICE_USD          refuse any single check worth more than this many dollars (default 0.25)
    UNFRET_MCP_SESSION_CAP_USD        refuse once a session has spent this many dollars (default 2.50)
    UNFRET_MCP_PRICE_RPC_URL          mainnet RPC for the agent's own Pyth SOL/USD reading, which values wSOL
                                      payments (default: mainnet's public RPC)
"""

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from pydantic import Field
from solders.keypair import Keypair

from veridex_mcp.explain import explain
from veridex_mcp.payer import Payer, PaymentFailedError, SpendGuard, SpendRefusedError, UnavailableError
from veridex_mcp.price import SolUsd
from veridex_mcp.solana import Rpc

_DEFAULT_RPC = {
    "solana-devnet": "https://api.devnet.solana.com",
    "solana-mainnet": "https://api.mainnet-beta.solana.com",
}

server = MCPServer(
    "unfret",
    instructions=(
        "Check a store or product listing for scam, dropshipping and dark-pattern signals before buying. Each check "
        "is paid from your own Solana wallet in USDC or wSOL (quick $0.01, deep $0.20; get_pricing has the live "
        "prices; wSOL at the current SOL price) within a dollar spend cap. "
        "Act on `action`: allow = proceed, warn = ask your human first, block = don't buy."
    ),
)
_payer: Payer | None = None


def _env(name: str, default: str | None = None) -> str | None:
    """
    Read a setting by its ``UNFRET_`` name, falling back to the name from before the rename (``VERIDEX_``).

    :param name: The name without its prefix, e.g. ``MCP_NETWORK``.
    :param default: The value when neither is set.
    :return: The value.
    """
    return os.environ.get(f"UNFRET_{name}") or os.environ.get(f"VERIDEX_{name}") or default


def _load_agent() -> Keypair:
    if path := _env("MCP_KEYPAIR"):
        return Keypair.from_bytes(bytes(json.loads(Path(path).expanduser().read_text())))
    if secret := _env("MCP_SECRET_KEY"):
        return Keypair.from_base58_string(secret)
    raise RuntimeError("set UNFRET_MCP_KEYPAIR (keypair file) or UNFRET_MCP_SECRET_KEY")


def _usd_micros(name: str, default: str) -> int:
    return int(Decimal(_env(name, default) or default) * 1_000_000)


def payer() -> Payer:
    """
    Return the session's payer, configured from the environment on first use.

    :return: The payer.
    """
    global _payer  # noqa: PLW0603
    if _payer is None:
        network = _env("MCP_NETWORK", "solana-devnet") or "solana-devnet"
        _payer = Payer(
            api_url=_api_url(),
            agent=_load_agent(),
            rpc=Rpc(_env("MCP_RPC_URL") or _DEFAULT_RPC[network]),
            network=network,
            guard=SpendGuard(
                max_per_call=_usd_micros("MCP_MAX_PRICE_USD", "0.25"),
                session_cap=_usd_micros("MCP_SESSION_CAP_USD", "2.50"),
            ),
            sol_usd=SolUsd(Rpc(_env("MCP_PRICE_RPC_URL") or _DEFAULT_RPC["solana-mainnet"])),
            prefer="SOL" if (_env("MCP_PAY_WITH") or "USDC").upper() == "SOL" else "USDC",
        )
    return _payer


def _api_url() -> str:
    return (_env("API_URL") or "https://unfret.kroshtan.com").rstrip("/")


@server.tool()
async def check_store(
    url: Annotated[str, Field(description="The store or product page URL (https).")],
    tier: Annotated[
        Literal["quick", "deep"],
        Field(description="quick: page and domain checks, $0.01. deep: all checks, reverse image search too, $0.20."),
    ] = "quick",
    policy: Annotated[
        Literal["strict", "balanced", "lenient"],
        Field(description="How cautious the returned action is; strict warns and blocks soonest."),
    ] = "balanced",
    page_content: Annotated[
        str | None,
        Field(
            description="The page's HTML, if you have it. Needed for marketplaces such as Amazon, which block fetches."
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Check a store or product page for scam, dropshipping and dark-pattern risk. Paid per call in USDC or wSOL.

    Pass page_content (the page's HTML) for marketplaces such as Amazon, which block server-side fetches.

    :param url: The store or product page URL.
    :param tier: quick (cheap checks, $0.01) or deep (everything incl. reverse image search, $0.20).
    :param policy: How cautious the returned action is: strict, balanced or lenient.
    :param page_content: The page's HTML, if available.
    :return: ``status`` (ok, not_paid, pending, unknown, unavailable), ``ok``, the ``verdict`` (action, risk_score,
        confidence, flags) or ``error``, the payment, and ``next_step`` when there's something to do.
    """
    body: dict[str, Any] = {"url": url, "tier": tier, "policy": policy}
    if page_content:
        body["page_content"] = page_content
    try:
        return await payer().check(body)
    except UnavailableError as exc:
        wait = f"in {exc.retry_after} s" if exc.retry_after else "later"
        return {
            "status": "unavailable",
            "ok": False,
            "error": str(exc),
            "next_step": f"Nothing was paid; retry {wait}.",
        }
    except SpendRefusedError as exc:
        return {
            "status": "not_paid",
            "ok": False,
            "error": str(exc),
            "next_step": "Nothing was paid: the spend cap refused it. Ask your human before raising the cap.",
        }
    except PaymentFailedError as exc:
        return {"status": "not_paid", "ok": False, "error": str(exc), "next_step": "Nothing was paid."}


@server.tool()
async def get_pricing() -> dict[str, Any]:
    """
    Unfret's current prices (free, no payment): the check tiers in dollars, and the live USDC and SOL amounts.

    :return: ``status``, ``ok`` and the price list (``x402``: ``quick_usd_micros``, ``deep_usd_micros``; ``usdc``;
        ``sol``), or ``error``.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(f"{_api_url()}/v1/pricing")
            response.raise_for_status()
            prices = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "unavailable", "ok": False, "error": type(exc).__name__, "next_step": "Retry later."}
    return {"status": "ok", "ok": True, "pricing": prices}


@server.tool()
def explain_flag(flag_type: Annotated[str, Field(description="A flag's `type` from a verdict.")]) -> str:
    """
    Explain what a flag in an Unfret verdict means, in one paragraph for the end user.

    :param flag_type: The flag's ``type``.
    :return: The explanation.
    """
    return explain(flag_type)


def main() -> None:
    """Run the server over stdio."""
    server.run()


if __name__ == "__main__":
    main()
