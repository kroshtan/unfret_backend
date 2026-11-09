# Veridex

[![CI](https://github.com/kroshtan/veridex_backend/actions/workflows/ci.yaml/badge.svg)](https://github.com/kroshtan/veridex_backend/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)

**Veridex scores e-commerce product pages for scam and dropshipping signals.** A browser extension sends the HTML of
the page the user is viewing; the backend runs a [LangGraph](https://github.com/langchain-ai/langgraph) pipeline of
independent analysis "skills" in parallel, and a small trained model turns their features into a calibrated 0–100
reliability score; an LLM writes the one-line explanation from the signals that moved it.

```json
POST /v1/analyze  →  { "score": 18, "explanation": "New domain, AliExpress-sourced images and templated 5-star reviews." }
```

> **Project status: prototype.** Veridex is an exploratory prototype, not a finished product. It was built to try out
> what an agentic LLM pipeline can do for shopping-fraud detection, and to put a modern Python stack to work
> (FastAPI, LangGraph, structured LLM output, asyncpg, uv). Payment is SOL-native: wallets sign in with Solana and
> spend prepaid credits, and agents pay per check over x402. It is not maintained for production use.

## How it works

```mermaid
flowchart LR
    A[POST /v1/analyze] --> P[preprocess<br/><sub>PageFacts + text, one parse</sub>]
    P --> C{classify<br/><sub>LLM, or known marketplace</sub>}
    C -- article / non-product --> X[early exit<br/><sub>score = -1</sub>]
    C -- marketplace listing / storefront --> S1[content]
    C --> S2[listing]
    C --> S3[reviews]
    C --> S4[html_source_signals]
    C --> S5[dark_patterns]
    C --> S6[injection]
    C --> S7[eu_registry / domain_age<br/><sub>storefronts</sub>]
    S1 & S2 & S3 & S4 & S5 & S6 & S7 --> J[judge<br/><sub>LLM → score + explanation</sub>]
```

Preprocessing parses the page once into typed **`PageFacts`** (title, brand, prices, seller, rating histogram, dated
reviews, hidden text, checkboxes, timers, VAT IDs, policy links, …); a browser extension can send them directly
(`page_facts`). Each skill reads the facts and reports numeric **features** plus factual **findings** (evidence, never a
verdict). The graph fans out to the skills that handle the page type with LangGraph's `Send` API, so they run
concurrently. All page content is passed to the LLM as clearly delimited untrusted data.

| Skill | What it checks |
| --- | --- |
| `content` | One LLM read of the listing text: scam copy and fake urgency, generic white-label product, whether the brand is established, implausible claims, missing trust information |
| `listing` | Generated-looking brand names, inflated "was" prices, keyword-stuffed titles, seller ≠ brand, ships from China |
| `reviews` | Reviews per day since the listing appeared, rating shape, duplicate and very short reviews, bursts, and review/title embedding similarity (reviews borrowed from other products) |
| `html_source_signals` | Known dropship apps, supplier CDNs, AliExpress review importers, shipping-protection and countdown apps |
| `dark_patterns` | Countdown timers (and whether they reset per visitor), pre-ticked paid add-ons, fee wording, and checkout total vs listed price when the checkout page is supplied |
| `injection` | Hidden text and instructions addressed to an AI, which honest shops have no reason to include |
| `eu_registry` | Storefronts: VAT ID checked against the EU VIES register (name match), withdrawal right and imprint |
| `domain_age` | Storefronts: domain registration via RDAP and the first certificate in the CT logs |
| `shop_links` | Storefronts: other web shops with the same analytics/pixel IDs, payment account or (hashed) contact details, one hop, from the shop fingerprint store; how many of them are young (their own registration dates). Local lookup, also in the quick tier; not in the scorer yet |

### Adding a skill

```python
class ShippingTimeSkill(Skill):
    name = "shipping_time"
    description = "Flags multi-week shipping estimates."
    features = ("weeks",)

    async def run(self, state: AnalysisState) -> SkillOutput:
        weeks = ...  # from state["facts"] / state["cleaned_content"]
        return self.output({"weeks": weeks}, [Observation(type="shipping", evidence=f"Ships in {weeks} weeks.")])
```

Register an instance in `SKILLS` in [`src/veridex/graph/__init__.py`](src/veridex/graph/__init__.py).

## API

Interactive docs are served at `/docs`. `/v1/analyze*` and `/v1/credits` take `Authorization: Bearer <session token>`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/v1/auth/nonce` | Start a Sign-In With Solana: nonce and message fields |
| `POST` | `/v1/auth/verify` | Finish it: `{address, signed_message, signature}` → session token (30 days) |
| `POST` | `/v1/analyze` | Analyse a page, paid from credits: `{ "url", "page_content"?, "page_facts"?, "checkout_content"? }` |
| `POST` | `/v1/analyze/stream` | The same, streamed as server-sent events (`page_type`, `finding`…, `result`) |
| `GET` | `/v1/credits` | The signed-in wallet's balance |
| `GET` | `/v1/pricing` | Analysis price, x402 prices, purchase bonuses, suggested packs, hot wallet |
| `POST` | `/v1/credits/purchase-tx` | Unsigned SOL or USDC transfer that buys credits, plus a Solana Pay link |
| `GET` | `/v1/credits/solana-pay.svg` | The Solana Pay request as a QR code |
| `GET` | `/v1/schema/page-facts` | JSON schema of `PageFacts`, for clients that extract facts themselves |
| `GET` | `/v1/health` | Liveness probe |
| `GET` | `/v1/health/ready` | Readiness probe (checks the database; reports x402 and credits status, hot wallet balances, alerts) |
| `POST` | `/v1/check` | Pay-per-check for AI agents over [x402](https://www.x402.org), see below |
| `GET` | `/llms.txt` \| `/.well-known/x402` | Machine-readable description of `/v1/check`, its tiers and prices |

### Credits (browser extension and platforms)

There are no accounts. A wallet signs in by signing a plain-text [SIWS](https://github.com/phantom/sign-in-with-solana)
message (no spending rights; the nonce is stateless and expires in five minutes) and gets a session token. **The only
user state stored is `wallet → balance in micro-dollars`** (and whether its trial was granted): no emails, no purchase history (the chain is the ledger), no
record of what was analysed.

Credits are dollars and are bought by sending SOL or USDC to the hot wallet (at least one check's worth, $0.15). USDC
is credited at face value; SOL at its dollar value at crediting time (Pyth SOL/USD, rounded down), and the SOL scanner
waits rather than guesses while the price is stale. A second scanner watches the hot wallet's USDC account. For SOL:
`/v1/credits/purchase-tx` builds the transfer with a
`veridex:credit:<beneficiary>` memo (any wallet may fund any wallet, so a smart-wallet vault can buy for the key that
signs in), and [`/checkout`](https://unfret.kroshtan.com/checkout) does it from a browser wallet or a phone (Solana
Pay QR). A background scanner walks the hot wallet's finalized transactions and credits every incoming System
transfer, top-level or inside a program call, to the memo's beneficiary (exactly one well-formed memo) or else the
sender, with the purchase bonus (≥$5 +10%, ≥$25 +20%). It moves its cursor in the same database
transaction by compare-and-swap, so each transfer is credited exactly once. Sweeps, fees and x402 settlements are
ignored; any other SOL sent to the hot wallet, a manual top-up included, counts as a purchase.

[`/connect`](https://unfret.kroshtan.com/connect) signs a wallet in and hands the session to the browser extension
(`window.postMessage`, relayed by the extension's content script). Both pages are static HTML and vanilla JS with a
strict Content Security Policy: no framework, no third-party requests, Wallet Standard discovery written directly. An analysis costs `VERIDEX_ANALYSIS_PRICE_USD_MICROS` ($0.15),
taken atomically before it runs and refunded when the page can't be analysed (score `-1`) or the analysis fails.

A wallet's first sign-in grants a free trial (5 analyses) only if it has real on-chain history (≥10 transactions or a
first one ≥30 days ago), within a per-visitor limit and a daily cap kept in memory only. Grant credits by hand with
`uv run scripts/grant_credits.py <pubkey> --checks <n>`.

### Pay-per-check for agents (x402)

`POST /v1/check` takes `{url, page_content?, tier: quick|deep, policy: strict|balanced|lenient}` and is paid per call
over x402 v2 with the `exact` scheme on Solana, in **wSOL** (wrapped SOL) or **USDC**. Without a
`PAYMENT-SIGNATURE` header it answers `402` with a `PAYMENT-REQUIRED` header listing wSOL first, then USDC. The client
signs a transaction that pays the price into the **hot wallet**'s token account for that mint: a plain `TransferChecked` from a standard
wallet, or **a smart wallet** (Squads and other program-controlled wallets) paying by CPI. Veridex co-signs as **fee
payer** with the same hot wallet (agents pay only the price: quick $0.01, deep $0.15), submits it and waits
for confirmation (`upfront` flow), then runs the analysis and answers with `{action: allow|warn|block, risk_score,
confidence, flags, model_version, …}` plus `PAYMENT-RESPONSE`, scored by the trained scorer. `warn` means "ask your
human". A page that can't be fetched is refused before any money moves. Nothing about the payment or the page is
stored.

Verification is behaviour-based, so any program instructions are accepted as long as: the hot wallet appears in no
instruction (it only pays the fee); every other required signature is present and valid; compute units and priority
fee stay within `VERIDEX_X402_MAX_COMPUTE_UNITS` / `VERIDEX_X402_MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS`; there is exactly
one memo; no signature precompiles; the transaction fits a packet; and a simulation shows the hot wallet's token
account growing by exactly the accepted amount with the hot wallet paying no more than the fee.

The hot wallet pays the fee of every transaction it submits, even one that fails on chain, so a payment must not be
able to pass the simulation and then fail. It may reference no slot-dependent sysvar (`Clock`, `SlotHashes`,
`SlotHistory`, `RecentBlockhashes`, `EpochRewards`) and use no address lookup table, and every program it runs, top
level or by CPI (from the simulation), must be a known wallet program: System, Token, ATA, Compute Budget, Memo,
Squads v3 and v4, Lighthouse, plus `VERIDEX_X402_EXTRA_PROGRAMS`. An allowlist is needed because any other program
could branch on the slot through the `Clock` syscall, which no account check sees. Its priority fee is capped at
`VERIDEX_X402_MAX_PRIORITY_FEE_BPS` (5%) of the tier's price. What remains, such as a payer racing its own payment, is
bounded by a breaker: after `VERIDEX_X402_BREAKER_PER_PAYER` (3) failed settlements in an hour from one wallet,
`VERIDEX_X402_BREAKER_PER_IP` (5) from one visitor network (IPv6 per /64) or `VERIDEX_X402_BREAKER_GLOBAL` (20) in
total, `/v1/check` answers `503 SETTLEMENTS_PAUSED` with `Retry-After` (a global trip also raises the
`settlements_failing` alert).

**Prices are canonical in US dollars** (`VERIDEX_X402_PRICE_*_USD_MICROS`, `VERIDEX_ANALYSIS_PRICE_USD_MICROS`; one
dollar = 1,000,000, which is also USDC's base unit). USDC pays them exactly. SOL amounts are those prices converted at
quote time with Pyth's SOL/USD price (rounded up), read on chain from the Pyth receiver's `PriceUpdateV2` account
(`VERIDEX_PYTH_SOL_USD_ACCOUNT`): fully verified, at most `VERIDEX_PYTH_MAX_AGE_S` (60 s) old, confidence at most
`VERIDEX_PYTH_MAX_CONF_BPS` (1%). wSOL isn't offered while the oracle fails those checks (the 402 then lists USDC
only, or answers 503 `PRICE_UNAVAILABLE` if the USDC account doesn't exist yet); a wSOL payment may be up to
`VERIDEX_QUOTE_TOLERANCE_BPS` (2%) off the current quote, since the price moves between the 402 and the payment. On
devnet the oracle is read from mainnet (Pyth's devnet copy updates too rarely).
All prices come from one place, `veridex.prices.PriceBook`. After settlement the real
on-chain delta is checked again; a mismatch gets no analysis and raises an alert. If the analysis fails or takes
longer than `VERIDEX_X402_ANALYSIS_TIMEOUT_S` (60 s) after a payment settled, or the page can't be assessed (score
`-1`), the price is refunded as credits to the paying wallet (never as an outgoing transfer), usable after signing in
with it. A payment submitted but not confirmed in time answers `402 SETTLEMENT_PENDING` with its signature; if it
lands within two minutes its price is credited the same way. Retrying with the same payment returns the same answer
for 15 minutes (concurrent retries wait for the first) and is never charged twice. Bot-check interstitials
(Cloudflare, DataDome, PerimeterX, Amazon) are refused as `422 PAGE_BLOCKED` before any money moves, and
`/v1/check` takes `VERIDEX_X402_CHECK_PER_MINUTE` (30) requests per visitor network.

Every error from the API is JSON with `detail` and `error_code` (validation errors add `errors`). For `/v1/check`:

| error_code | HTTP | charged | what to do |
| --- | --- | --- | --- |
| `PAYMENT_REQUIRED` | 402 | no | pay one of `accepts` and retry with PAYMENT-SIGNATURE; `error` says why |
| `INVALID_PAYMENT` | 400 | no | the PAYMENT-SIGNATURE header couldn't be decoded |
| `VALIDATION_ERROR` | 422 | no | fix the request body (see `errors`) |
| `PAGE_UNAVAILABLE` | 422 | no | the page couldn't be fetched: send page_content |
| `PAGE_BLOCKED` | 422 | no | the page is a bot check: send the real page as page_content |
| `RATE_LIMITED` | 429 | no | wait Retry-After seconds |
| `PRICE_UNAVAILABLE` | 503 | no | nothing can be priced right now: retry after Retry-After |
| `SETTLEMENTS_PAUSED` | 503 | no | too many failed settlements: retry after Retry-After |
| `SERVICE_UNAVAILABLE` | 503 | no | payments or analysis are off: retry later |
| `SETTLEMENT_FAILED` | 402 | no | the network refused the payment: pay again |
| `SETTLEMENT_PENDING` | 402 | maybe | don't pay again; if it lands within 2 min its price is credited |
| `SETTLEMENT_MISMATCH` | 402 | yes | it landed differently than simulated: no verdict (alerted) |
| `GATEWAY_TIMEOUT` | 504 | refunded | the analysis timed out: the price is credited to the paying wallet |
| `INTERNAL_ERROR` | 500 | refunded | the analysis failed: the price is credited to the paying wallet |

Money flows through, and the cold wallet only ever receives SOL. On mainnet, USDC the hot wallet received is swapped to
wSOL once it holds `VERIDEX_SWAP_THRESHOLD_USDC` (5 USDC), or dust older than a day: Jupiter's swap API
(`VERIDEX_SWAP_API_URL`, self-hostable) builds the route, and the transaction is **verified by simulation before the
hot wallet signs it**: the hot wallet is the only signer and fee payer; its USDC account loses exactly the amount; its
wSOL account gains at least the stricter of the route's slippage bound (`VERIDEX_SWAP_MAX_SLIPPAGE_BPS`, 1%) and a floor
3% under the Pyth-implied amount (no swap without a fresh price); its SOL drops by no more than the capped fee; and it
still owns both token accounts with no delegate or close authority, and is still a plain System account. A background
sweep (one atomic transaction) then unwraps the hot wallet's wSOL, recreates its wSOL account, creates its USDC account
if missing, and sends everything above `VERIDEX_SWEEP_RESERVE_LAMPORTS` (0.01 SOL) to `VERIDEX_COLD_ADDRESS` once the
hot wallet holds more than `VERIDEX_SWEEP_THRESHOLD_LAMPORTS` (0.1 SOL). All signing goes through one function that
accepts only verified payments (as fee payer, never referenced by an instruction), sweeps to the cold address and
verified swaps. Alerts are logged at error level and counted in readiness (named, with the balances, for
`Authorization: Bearer $VERIDEX_ADMIN_TOKEN`): value stuck above 1.5x the threshold (sweeps failing), SOL for fees
below `VERIDEX_X402_MIN_FEE_LAMPORTS` (payments pause), swaps failing repeatedly or USDC piling up beyond 3x the swap
threshold, and any value leaving the hot wallet except to cold or in a USDC→SOL swap (recognised by its net effect:
USDC down, wSOL up at a sane rate, nothing else lost; payments pause, a compromise signal). The audit pages through
every transaction since the last one it checked, skipping failed ones and ones the hot wallet didn't sign (neither can
move its value). A compromise signal and post-settlement mismatches stay raised until the operator, having checked
the logged transactions (and rotated the hot wallet if it was real), clears them:
`curl -X POST -H "Authorization: Bearer $VERIDEX_ADMIN_TOKEN" https://…/v1/admin/x402/clear-alerts`.

The feature is off (`503`) until `VERIDEX_HOT_WALLET_KEY` and `VERIDEX_COLD_ADDRESS` (a cold wallet; the server never
holds its key) are set. Devnet is the default, and mainnet needs `VERIDEX_X402_ALLOW_MAINNET=true`. Run one instance:
the sweeper isn't coordinated across processes. [`mcp/`](mcp/) is an MIT-licensed MCP server that pays these checks from an agent's own wallet within a
spend cap.

## Running it

**Requirements:** [uv](https://docs.astral.sh/uv/), Docker, and an API key for an OpenAI-compatible LLM API
([Scaleway Generative APIs](https://www.scaleway.com/en/generative-apis/) by default).

```bash
cp .env.example .env       # add your VERIDEX_LLM_API_KEY
docker compose up --build  # API on http://localhost:8080, Postgres alongside
```

Set `VERIDEX_SESSION_SECRET` (e.g. `openssl rand -hex 32`), sign in with a wallet at
`http://localhost:8080/connect`, give it credits and analyse a page with the session token:

```bash
uv run scripts/grant_credits.py <your wallet address> --checks 10

curl -s https://example-shop.com/product/123 > page.html
jq -n --rawfile html page.html '{page_content: $html, url: "https://example-shop.com/product/123"}' \
  | curl -X POST localhost:8080/v1/analyze -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d @-
```

### Configuration

Settings are defined and validated in [`src/veridex/config.py`](src/veridex/config.py) (with
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)). Override any of them with a
`VERIDEX_`-prefixed environment variable or a git-ignored `.env` file.

| Variable | Default | |
| --- | --- | --- |
| `VERIDEX_LLM_API_KEY` | — | Required |
| `VERIDEX_LLM_BASE_URL` | `https://api.scaleway.ai/v1` | Any OpenAI-compatible chat completions API |
| `VERIDEX_LLM_MODEL` | `mistral-small-3.2-24b-instruct-2506` | Used by every node and skill; must support tool calling |
| `VERIDEX_EMBEDDING_MODEL` | `bge-multilingual-gemma2` | Review/title similarity, same provider |
| `VERIDEX_NETWORK_LOOKUPS` | `true` | RDAP, certificate transparency and EU VIES lookups about the shop |
| `VERIDEX_FINGERPRINTS` / `VERIDEX_CONTACT_HASH_KEY` | `true` / derived from the session secret | Shop fingerprint store; the key for contact hashes (no key and no session secret: contacts aren't stored) |
| `VERIDEX_MAX_PAGE_BYTES` | `2097152` | Larger pages are rejected with 413/422 |
| `VERIDEX_SKILL_TIMEOUT_S` / `VERIDEX_ANALYSIS_TIMEOUT_S` | `45` / `90` | A slow skill is skipped; a slow analysis returns 504 |
| `VERIDEX_POSTGRES_URI` | `postgresql://veridex:veridex@localhost:5432/veridex` | Tables are created on startup |
| `VERIDEX_SESSION_SECRET` | — | Signs nonces and session tokens; sign-in and credits are off (503) while empty |
| `VERIDEX_SIWS_DOMAIN` | `unfret.kroshtan.com` | Sign-in messages must be bound to this domain |
| `VERIDEX_ANALYSIS_PRICE_USD_MICROS` | `150000` | One analysis ($0.15) |
| `VERIDEX_X402_PRICE_QUICK_USD_MICROS` / `VERIDEX_X402_PRICE_DEEP_USD_MICROS` | `10000` / `150000` | x402 tiers ($0.01 / $0.15) |
| `VERIDEX_CREDIT_BONUS_TIERS` / `VERIDEX_SUGGESTED_PACKS` | `[[5000000,10],[25000000,20]]` / `[10,50,200]` | Purchase bonuses (min micro-dollars, %); packs in checks |
| `VERIDEX_PURCHASE_MIN_USD_MICROS` / `VERIDEX_PURCHASE_MAX_USD_MICROS` | `150000` / `1000000000` | Credit purchase limits ($0.15 … $1,000) |
| `VERIDEX_QUOTE_TOLERANCE_BPS` | `200` | How far a wSOL payment may drift from the quote |
| `VERIDEX_TRIAL_CHECKS` | `5` | Free analyses on a first sign-in (wallets with history only) |
| `VERIDEX_TRIAL_PER_IP_PER_DAY` / `VERIDEX_TRIAL_DAILY_CAP` | `2` / `50` | Trial limits, kept in memory |
| `VERIDEX_HOT_WALLET_KEY` / `VERIDEX_COLD_ADDRESS` | — | x402 and credit purchases (see above); off while empty |
| `VERIDEX_ADMIN_TOKEN` | — | Bearer token for `/v1/admin/…` and the readiness details (balances, alert names); off while empty |

## Development

```bash
make install           # uv sync + pre-commit hooks
make lint              # ruff, mypy (strict), pydoclint
make test-unit         # fast, no services needed
make test              # unit + integration tests against a throwaway Postgres container
make audit             # pip-audit over the locked dependencies
make run               # uvicorn with auto-reload
```

Unit tests cover the deterministic parts of every skill, the graph routing (with a stubbed LLM), SSRF protection, x402
payment verification and SIWS. Integration tests exercise the HTTP API end-to-end against a real Postgres (sign-in,
concurrent debits, the credits scanner), with the analysis graph replaced by a test double so no LLM calls are made.

```
src/veridex/
├── main.py            # FastAPI app, lifespan (graph + DB pool), middleware
├── config.py          # typed settings (pydantic-settings)
├── middleware.py      # body size cap, x-process-time header
├── routes/            # analyze, sign-in, credits, x402 check, health
├── graph/
│   ├── __init__.py    # graph wiring + skill registry
│   ├── nodes.py       # preprocess, classify, early_exit, judge
│   ├── state.py       # shared LangGraph state
│   └── skills/        # one module per skill
├── credits/           # SIWS, the dollar ledger, pricing, trial rules, purchase scanner
├── x402/              # pay-per-check: verification, hot wallet, sweeps
├── auth.py            # bearer sessions
├── db.py, migrations.py  # versioned schema + queries (asyncpg)
├── llm.py             # shared chat model
└── net.py             # SSRF-safe HTTP fetching
```

## Design notes

- **Untrusted input fetching.** Skills download URLs taken from user-submitted HTML. `net.fetch_public` only allows
  http(s), resolves the host and refuses loopback, private, link-local (cloud metadata) and reserved addresses,
  then connects to the address it checked (Host header and TLS SNI keep the original name), so a second DNS answer
  cannot rebind the request. Every redirect hop is re-validated and response size is capped.
- **Fail soft.** Each skill runs under its own timeout; a skill that fails or hangs reports "unavailable" to the
  judge instead of failing the analysis. The whole analysis has a deadline too (504 when exceeded).
- **Bounded input.** Request bodies over `VERIDEX_MAX_PAGE_BYTES` are refused before they are parsed.
- **Sign-in grants no spending power.** Wallets only ever sign a readable message (to sign in) or a transfer the
  user sees (to buy). The server holds no user keys; a session token can spend Veridex credits, never SOL.
- **Exactly-once crediting.** Credits and the scanner cursor move in one transaction, by compare-and-swap; debits are
  a single conditional `UPDATE`, so concurrent analyses never overspend.

## Limitations and responsible use

- Scores come from a logistic regression over skill features (`src/veridex/scoring/model.json`, trained with
  `python -m evals.train` on a private labelled corpus). They are a calibrated signal, not a verdict, and can be wrong
  in both directions. Part of the training data is synthetic (marketplace pages rendered from public review data),
  so marketplace accuracy on real pages is likely lower than measured.
- Page content is passed to the LLM, so a hostile page can attempt prompt injection to influence its own score. It is
  delimited as untrusted data, and attempts to address the model are detected and count against the page.
- Registration data (RDAP) is often redacted; the first-certificate date is a second, registry-independent age.

## License

[MIT](LICENSE)
