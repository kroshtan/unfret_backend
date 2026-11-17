# Changelog (unfret-mcp)

## [0.2.0]

* **Pay from an Unfret pass instead of a wallet**: set `UNFRET_PASS` (`ufk_…`) and `check_store` sends it to
  `/v1/check` as `Authorization: Bearer`; no keypair is needed or read. Quick checks are free, deep checks use one of
  the pass's checks; the result adds `charged`, `free_check`, `free_checks_left` and `balance_usd_micros`, and an
  empty pass gets `status: "not_paid"` with what to do. `UNFRET_MCP_KEYPAIR` is no longer required in the registry
  entry.
* Every tool has a title and explicit hints: `check_store` is not read-only (it spends), not destructive, not
  idempotent and open-world (it reads a third-party page); `get_pricing` and `explain_flag` are read-only,
  idempotent and closed-world.
* The registry entry (`server.json`) lists the remote server, `https://unfret.net/mcp` (streamable HTTP,
  `Authorization: Bearer <Unfret pass>`), next to the PyPI package.
* `check_store`'s description names the verdict's `band` and the words a shopper sees for it: `reliable` "Looks
  fine", `concerns` "Worth a look", `careful` "Think twice", `junk_or_scam` "Serious concerns". The band values are
  unchanged.

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
