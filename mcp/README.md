# unfret-mcp

<!-- mcp-name: net.unfret/mcp -->

An [MCP](https://modelcontextprotocol.io) server that lets AI shopping agents ask
[Unfret](https://unfret.net) whether a store or product listing shows signs of scams, dropshipping of
generic goods, fake reviews or dark patterns, before they buy. MIT licensed.

Each check is paid per call over [x402](https://www.x402.org) from the **agent's own Solana wallet**, in USDC or
wSOL. Prices are in US dollars (quick $0.01, deep $0.20; `get_pricing` has the live prices): USDC pays them exactly,
wSOL at Unfret's SOL/USD quote. By default the server pays in USDC when Unfret offers it and the wallet holds enough,
otherwise in wSOL (`UNFRET_MCP_PAY_WITH=SOL` to always use SOL); if the wallet holds plain SOL, it wraps what it needs
first. The spend
caps are in dollars whatever the currency: the server values a wSOL charge with **its own** Pyth SOL/USD reading
(mainnet, fully verified, ≤2 min old, confidence ≤2%) rather than Unfret's word, and refuses SOL when it has no such
price. Unfret pays the network fee, so the agent pays only the price. The server never holds anyone else's funds.

| Tool | Input | Output |
| --- | --- | --- |
| `check_store` | `url`, `tier` (`quick` $0.01 \| `deep` $0.20), `policy` (`strict` \| `balanced` \| `lenient`), `page_content` (optional HTML) | `{action, risk_score, confidence, flags, …}` plus the payment transaction |
| `explain_flag` | `flag_type` | A one-paragraph explanation for the end user |
| `get_pricing` | — | The current prices (free): tiers in dollars, live USDC and SOL amounts |

Every result has `status` and `ok`: `ok` (a verdict), `refunded` (paid, but Unfret's analysis failed: the price is
credited back to the paying wallet as Unfret credits), `not_paid` (refused before any money moved), `unavailable`
(Unfret is paused or rate-limiting; retry later), `pending` or `unknown` (the payment may have been taken: don't pay
again for that page; if it landed without a verdict Unfret credits its price to the paying wallet). A `next_step`
says what to do whenever there is something to do.

`action` is `allow`, `warn` (ask your human first) or `block`, at thresholds set by the chosen `policy`. Flags are
factual evidence, never a "scam" label. Send `page_content` for marketplaces such as Amazon, which block server-side
fetches.

## Configuration

| Variable | Default | |
| --- | --- | --- |
| `UNFRET_MCP_KEYPAIR` | — | Path to the agent's Solana CLI keypair JSON (or `UNFRET_MCP_SECRET_KEY`, base58) |
| `UNFRET_MCP_NETWORK` | `solana-mainnet` | `solana-devnet` to try it with test money against `https://dev.unfret.net` |
| `UNFRET_MCP_RPC_URL` | the network's public RPC | The public RPCs are rate-limited; use your own for anything busy |
| `UNFRET_MCP_PAY_WITH` | USDC when offered and held, else SOL | `SOL` to always pay in wSOL |
| `UNFRET_MCP_MAX_PRICE_USD` | `0.25` | Refuse any single check worth more than this many dollars |
| `UNFRET_MCP_SESSION_CAP_USD` | `2.50` | Refuse once the session has spent this many dollars |
| `UNFRET_MCP_PRICE_RPC_URL` | mainnet's public RPC | Where the agent reads Pyth SOL/USD to value wSOL payments |
| `UNFRET_API_URL` | `https://unfret.net` | |

## Install

Needs [uv](https://docs.astral.sh/uv/) (Python 3.12 or newer) and a Solana wallet holding a little USDC or SOL. Give
the agent a wallet of its own with only what it may spend, never your main wallet: the caps above are the server's,
the wallet balance is the real limit.

```json
{"mcpServers": {"unfret": {"command": "uvx", "args": ["unfret-mcp"],
  "env": {"UNFRET_MCP_KEYPAIR": "~/.config/solana/agent.json"}}}}
```

To try it without real money, use devnet (free test SOL from `solana airdrop`, test USDC from
[faucet.circle.com](https://faucet.circle.com)):

```json
{"mcpServers": {"unfret": {"command": "uvx", "args": ["unfret-mcp"],
  "env": {"UNFRET_MCP_KEYPAIR": "~/.config/solana/devnet-agent.json", "UNFRET_MCP_NETWORK": "solana-devnet",
          "UNFRET_API_URL": "https://dev.unfret.net"}}}}
```

Deep checks are synchronous today, so there is no polling tool yet.

Source: [git.kroshtan.com/kroshtan/unfret_backend](https://git.kroshtan.com/kroshtan/unfret_backend/src/branch/main/mcp)
(mirror on [GitHub](https://github.com/kroshtan/unfret_backend/tree/main/mcp)). MIT licensed.
