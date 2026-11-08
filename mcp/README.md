# veridex-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI shopping agents ask
[Veridex](https://veridex.kroshtan.com) whether a store or product listing shows signs of scams, dropshipping of
generic goods, fake reviews or dark patterns, before they buy. MIT licensed.

Each check is paid per call over [x402](https://www.x402.org) from the **agent's own Solana wallet**, in USDC or
wSOL. Prices are in US dollars (quick $0.01, deep $0.15): USDC pays them exactly, wSOL at Veridex's SOL/USD quote. By
default the server pays in USDC when Veridex offers it and the wallet holds enough, otherwise in wSOL
(`VERIDEX_MCP_PAY_WITH=SOL` to always use SOL); if the wallet holds plain SOL, it wraps what it needs first. The spend
caps are in dollars whatever the currency: the server values a wSOL charge with **its own** Pyth SOL/USD reading
(mainnet, fully verified, ≤2 min old, confidence ≤2%) rather than Veridex's word, and refuses SOL when it has no such
price. Veridex pays the network fee, so the agent pays only the price. The server never holds anyone else's funds.

| Tool | Input | Output |
| --- | --- | --- |
| `check_store` | `url`, `tier` (`quick` $0.01 \| `deep` $0.15), `policy` (`strict` \| `balanced` \| `lenient`), `page_content` (optional HTML) | `{action, risk_score, confidence, flags, …}` plus the payment transaction |
| `explain_flag` | `flag_type` | A one-paragraph explanation for the end user |
| `get_pricing` | — | The current prices (free): tiers in dollars, live USDC and SOL amounts |

Every result has `status` and `ok`: `ok` (a verdict), `not_paid` (refused before any money moved), `unavailable`
(Veridex is paused or rate-limiting; retry later), `pending` or `unknown` (the payment may have been taken: don't pay
again for that page; if it landed without a verdict Veridex credits its price to the paying wallet). A `next_step`
says what to do whenever there is something to do.

`action` is `allow`, `warn` (ask your human first) or `block`, at thresholds set by the chosen `policy`. Flags are
factual evidence, never a "scam" label. Send `page_content` for marketplaces such as Amazon, which block server-side
fetches.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `VERIDEX_MCP_KEYPAIR` | — | Path to the agent's Solana CLI keypair JSON (or `VERIDEX_MCP_SECRET_KEY`, base58) |
| `VERIDEX_MCP_NETWORK` | `solana-devnet` | `solana-mainnet` once Veridex accepts mainnet payments |
| `VERIDEX_MCP_RPC_URL` | the network's public RPC | |
| `VERIDEX_MCP_MAX_PRICE_USD` | `0.25` | Refuse any single check worth more than this many dollars |
| `VERIDEX_MCP_SESSION_CAP_USD` | `2.50` | Refuse once the session has spent this many dollars |
| `VERIDEX_MCP_PRICE_RPC_URL` | mainnet's public RPC | Where the agent reads Pyth SOL/USD to value wSOL payments |
| `VERIDEX_API_URL` | `https://veridex.kroshtan.com` | |

```json
{"mcpServers": {"veridex": {"command": "uvx", "args": ["veridex-mcp"],
  "env": {"VERIDEX_MCP_KEYPAIR": "~/.config/solana/agent.json"}}}}
```

Deep checks are synchronous today, so there is no polling tool yet.
