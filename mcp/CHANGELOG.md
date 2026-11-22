# Changelog (unfret-mcp)

## [0.3.0]

* **Pays USDC on Base** (x402 `exact`, an EIP-3009 `transferWithAuthorization`), next to Solana. Set
  `UNFRET_MCP_BASE_KEY` to the agent's EVM private key (hex; never logged, printed or put in an error). The agent
  signs an authorization of exactly the offered amount to Unfret's `payTo`, valid from 10 minutes back until the
  offer's `maxTimeoutSeconds` ahead (at most 10 minutes), with a random 32-byte nonce and the EIP-712 domain this
  package knows for Circle's USDC (`USDC`/`2` on Sepolia, `USD Coin`/`2` on mainnet). Unfret's facilitator pays the
  gas, so the wallet needs USDC only, no ETH. Only Circle's USDC on the configured network is paid.
* **One Base network, as on Solana**: `UNFRET_MCP_BASE_NETWORK` is `base-mainnet` by default (real USDC on
  unfret.net); set `base-sepolia` for test USDC with `UNFRET_API_URL=https://dev.unfret.net`. Only that network is
  paid: an offer on the other one is refused, and the error names the setting.
* **Choosing the network**: with both wallets, `UNFRET_MCP_PREFER` (`solana` by default, or `base`) is tried first
  and the other is the fallback when the preferred one can't be paid (not offered, or no affordable Solana option). A
  spend-cap refusal never falls back. With one wallet, only that network is used. A Solana keypair is no longer
  required: a Base key alone (or a pass) is enough.
* The same dollar spend caps (`UNFRET_MCP_MAX_PRICE_USD`, `UNFRET_MCP_SESSION_CAP_USD`) cover Base payments, at face
  value. Results add `network` (the CAIP-2 network paid on); for Base, `payment` is the authorization's nonce.
* New dependency: `eth-account` (EIP-712 signing), a normal dependency rather than an extra, so `uvx unfret-mcp` and
  the registry entry work with a Base key as they are.

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
