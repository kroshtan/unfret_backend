# Unfret from TypeScript: a pass, or x402 on Solana or Base

Three minimal, copy-paste clients for Unfret's `POST /v1/check` (is this store or product page safe to buy from?
`allow`, `warn` or `block`, with the evidence). MIT licensed. Python versions: [`../python`](../python).

| Script | Pays with | Library |
| --- | --- | --- |
| [`pass_check.ts`](pass_check.ts) | An Unfret pass (`Authorization: Bearer ufk_…`); makes a free developer pass if you have none | `fetch` only |
| [`x402_solana.ts`](x402_solana.ts) | x402, `exact` on Solana: USDC (default) or wSOL | [`@x402/fetch`](https://www.npmjs.com/package/@x402/fetch) + [`@x402/svm`](https://www.npmjs.com/package/@x402/svm) (Coinbase's official SDK), `@solana/kit` |
| [`x402_base.ts`](x402_base.ts) | x402, `exact` on Base: USDC, EIP-3009 `transferWithAuthorization` | `@x402/fetch` + [`@x402/evm`](https://www.npmjs.com/package/@x402/evm), `viem` |

The x402 scripts use the official SDK as is: Unfret's 402 is standard x402 v2 (`accepts`, `PAYMENT-SIGNATURE`,
`PAYMENT-RESPONSE`). On Solana, Unfret is the fee payer (`extra.feePayer`) and submits the transaction itself, so you
pay only the price; on Base the facilitator pays the gas and Unfret settles only once the answer is ready. Each script
pays only the asset you chose and refuses anything above `MAX_AMOUNT`.

## Run

Needs Node 22.18 or later (it runs `.ts` files directly); dependencies are pinned in `package.json` and
`package-lock.json`.

```bash
cd examples/typescript
npm ci
node pass_check.ts                                         # a deep check with a (new) developer pass
SOLANA_KEYPAIR=~/.config/solana/id.json node x402_solana.ts  # a quick check paid in USDC on Solana devnet
EVM_PRIVATE_KEY=0x… node x402_base.ts                        # a quick check paid in USDC on Base Sepolia
npm run typecheck                                          # tsc --noEmit
```

Keys come from the environment only; never put them in the code.

| Setting | Default | |
| --- | --- | --- |
| `UNFRET_ENV` | `dev` | `production`: unfret.net and mainnet (Solana; Base once it is on there), real money |
| `UNFRET_API_URL` | from `UNFRET_ENV` | `https://dev.unfret.net` (test server) or `https://unfret.net` |
| `UNFRET_CHECK_URL` | the test shop on dev | The page to check |
| `UNFRET_TIER` | `deep` (pass), `quick` (x402) | `quick` ($0.01; free with a pass) or `deep` ($0.20; one check with a pass) |
| `UNFRET_PASS` | a new developer pass | `pass_check.ts`: your pass (`ufk_…`) |
| `SOLANA_KEYPAIR` / `SOLANA_PRIVATE_KEY` | `~/.config/solana/id.json` | `x402_solana.ts`: a `solana-keygen` JSON file, or a base58 secret key |
| `SOLANA_ASSET` | `usdc` | `wsol` to pay in wrapped SOL |
| `SOLANA_RPC_URL` | the network's public RPC | Used to read the mint and a recent blockhash |
| `EVM_PRIVATE_KEY` | — | `x402_base.ts`: `0x` and 64 hex digits |
| `MAX_AMOUNT` | $0.25 (USDC), 0.003 SOL (wSOL) | The most one call may pay, in the asset's base units |

**Dev uses test money.** Solana devnet and Base Sepolia USDC come from [faucet.circle.com](https://faucet.circle.com),
devnet SOL from [faucet.solana.com](https://faucet.solana.com). To pay in wSOL, wrap some first
(`spl-token wrap 0.01`). The Base wallet needs USDC only, no ETH. **Production** (`UNFRET_ENV=production`) pays real
USDC (or wSOL) on Solana mainnet. Base on unfret.net (Base mainnet) is not switched on yet: until it is, its 402 lists
Solana only and the Base script stops before signing anything (no matching payment option).

## What you get

`200` with `{action, band, risk_score, confidence, flags, …}` and a `PAYMENT-RESPONSE` header (the transaction). A
refused payment answers `402` again with `error`, and on Solana `reason` (e.g. `source_token_account_missing`: the
wallet has no account for that token, `insufficient_funds`); nothing was paid. The same payment always gets the same
answer and is never charged twice. All error codes: [unfret.net/llms.txt](https://unfret.net/llms.txt); the HTTP
API: [unfret.net/developers](https://unfret.net/developers).

For a dependency-free client with an allow/warn/block policy, copy the [TypeScript reference
client](../../clients/typescript); to pay from an agent with a spend cap, the MCP server [`unfret-mcp`](../../mcp).
