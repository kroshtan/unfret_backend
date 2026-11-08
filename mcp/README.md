# veridex-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI shopping agents ask
[Veridex](https://veridex.kroshtan.com) whether a store or product listing shows signs of scams, dropshipping of
generic goods, fake reviews or dark patterns, before they buy. MIT licensed.

Each check is paid per call over [x402](https://www.x402.org) from the **agent's own Solana wallet**, in USDC or
wSOL. Prices are set in SOL; the USDC amount is the SOL price converted at quote time (Pyth SOL/USD). By default the
server pays in USDC when Veridex offers it and the wallet holds enough, otherwise in wSOL (`VERIDEX_MCP_PAY_WITH=SOL`
to always use SOL); if the wallet holds plain SOL, it wraps what it needs first. Spend caps apply per currency
(`VERIDEX_MCP_MAX_PRICE_USDC`, `VERIDEX_MCP_SESSION_CAP_USDC` next to the lamport ones). Veridex pays the network
fee, so the agent pays only the price. The server never holds anyone else's funds.

| Tool | Input | Output |
| --- | --- | --- |
| `check_store` | `url`, `tier` (`quick` 0.0001 SOL \| `deep` 0.0015 SOL), `policy` (`strict` \| `balanced` \| `lenient`), `page_content` (optional HTML) | `{action, risk_score, confidence, flags, …}` plus the payment transaction |
| `explain_flag` | `flag_type` | A one-paragraph explanation for the end user |

`action` is `allow`, `warn` (ask your human first) or `block`, at thresholds set by the chosen `policy`. Flags are
factual evidence, never a "scam" label. Send `page_content` for marketplaces such as Amazon, which block server-side
fetches.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `VERIDEX_MCP_KEYPAIR` | — | Path to the agent's Solana CLI keypair JSON (or `VERIDEX_MCP_SECRET_KEY`, base58) |
| `VERIDEX_MCP_NETWORK` | `solana-devnet` | `solana-mainnet` once Veridex accepts mainnet payments |
| `VERIDEX_MCP_RPC_URL` | the network's public RPC | |
| `VERIDEX_MCP_MAX_PRICE_LAMPORTS` | `2000000` (0.002 SOL) | Refuse any single check priced above this |
| `VERIDEX_MCP_SESSION_CAP_LAMPORTS` | `20000000` (0.02 SOL) | Refuse once the session has spent this |
| `VERIDEX_API_URL` | `https://veridex.kroshtan.com` | |

```json
{"mcpServers": {"veridex": {"command": "uvx", "args": ["veridex-mcp"],
  "env": {"VERIDEX_MCP_KEYPAIR": "~/.config/solana/agent.json"}}}}
```

Deep checks are synchronous today, so there is no polling tool yet.
