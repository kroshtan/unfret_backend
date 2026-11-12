# Changelog (unfret-mcp)

## [0.1.1]

* A check that was paid but whose analysis failed on Unfret's side now has `status: "refunded"` (was `"ok"` with
  `ok: false`): the price was credited back to the paying wallet as Unfret credits, and `error` says how much.
  `status: "ok"` now always means a verdict.

## [0.1.0]

First public release, on PyPI as `unfret-mcp` and in the MCP Registry as `net.unfret/mcp`.

* Tools `check_store` (quick $0.01 or deep $0.20, paid per call over x402 from the agent's own Solana wallet in USDC
  or wSOL), `explain_flag` and `get_pricing`.
* Spend caps in dollars (`UNFRET_MCP_MAX_PRICE_USD`, `UNFRET_MCP_SESSION_CAP_USD`); wSOL charges are valued with the
  server's own Pyth SOL/USD reading.
* Defaults to Solana mainnet and https://unfret.net; `UNFRET_MCP_NETWORK=solana-devnet` with
  `UNFRET_API_URL=https://dev.unfret.net` for test money. An unknown network is refused at start, and a payment
  offer on the wrong network names the setting to fix.
