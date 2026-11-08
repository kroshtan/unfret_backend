# Changelog

## Unreleased

Open-source release.

### Features

* Agent experience: every error is JSON with `detail` and `error_code` (request validation errors too, as `VALIDATION_ERROR` with `errors`, and unknown routes as `NOT_FOUND`); an errors table (what each code means, whether you were charged, what to do) in `/llms.txt` and the README; each 402 option states what the payment transaction must look like in `extra.constraints` (memo, compute budget and priority-fee caps, allowed programs, no lookup tables). MCP: a free `get_pricing` tool, described parameters, and `status: unavailable` with a retry hint when Veridex is paused or rate-limiting
* Performance: payments settle in parallel (the wallet lock covers verification and signing only, not the wait for confirmation); the hot wallet's balances are read in one `getMultipleAccounts` call
* **Prices in US dollars**: x402 quick $0.01, deep $0.15, an analysis $0.15; purchase bonuses ≥$5 +10%, ≥$25 +20%; packs of 10/50/200 checks. USDC pays exactly; SOL is converted at quote time with Pyth's SOL/USD price read on chain (fully verified, ≤60 s old, confidence ≤1%; charges round up, credits round down) and refused while the oracle fails those checks, when only USDC is offered. x402 offers wSOL (≤2% drift from the quote) then USDC once the hot wallet's USDC account exists. Balances are micro-dollars (`balance_usd_micros`; migration 5 converts lamport balances so every wallet keeps its number of checks). `/v1/pricing`, `/checkout`, `/llms.txt` and `/.well-known/x402` show dollars with live SOL amounts. All prices come from one `PriceBook`
* USDC alongside SOL on both payment tracks
* USDC→SOL swaps before sweeping (mainnet): Jupiter routes, verified by simulation before the hot wallet signs (exact USDC out, at least the slippage bound and a Pyth floor in, fee-only SOL debit, no delegate/close authority/owner change); the cold wallet only ever receives SOL. The sweep job also creates the hot wallet's USDC account; the outflow audit recognises swaps by their net effect
* The MCP server pays in USDC when offered and held, otherwise SOL, with per-currency spend caps; `/checkout` has a SOL/USDC switch
* Minimum credit purchase raised to one check ($0.15)
* Credits door replaces accounts: Sign-In With Solana (stateless nonces and session tokens; the signature grants no spending rights) and a lamport balance per wallet, the only user state stored. `/v1/analyze` (and its stream) debits atomically and refunds pages that can't be analysed and failed analyses
* Buying credits with SOL: `/v1/pricing`, `/v1/credits`, `/v1/credits/purchase-tx` (unsigned transfer + Solana Pay link) and a QR endpoint; a scanner credits finalized incoming transfers to the hot wallet exactly once (cursor moved by compare-and-swap), with purchase bonuses
* `/connect` (sign in, hand the session to the browser extension, which confirms receipt) and `/checkout` (buy credits from a browser wallet or a phone via Solana Pay QR): static HTML and vanilla JS with a strict CSP, Wallet Standard discovery without dependencies
* Smart-wallet purchases: `purchase-tx` takes a `beneficiary`, recorded in a `veridex:credit:<pubkey>` memo the scanner credits (exactly one well-formed memo, else the sender), so a vault can buy for the key that signs in
* `/v1/analyze/stream` emits contract `step` events (`{"skill", "findings"}`); the scanner's first start waits for the newest hot-wallet transaction to finalize, so funding just before start-up is never credited
* Free trial on a wallet's first sign-in, only for wallets with on-chain history, within a per-visitor limit and a daily cap kept in memory
* Paid x402 checks whose analysis fails are refunded as credits to the paying wallet
* `/v1/schema/page-facts` serves the `PageFacts` JSON schema; `/v1/analyze` accepts `page_facts` without `page_content` and returns the scorer's assessment and the balance
* `POST /v1/check`: pay-per-check for AI agents over x402 v2 (`exact` scheme, wSOL on Solana; a hot wallet receives payments and pays their fees), with quick/deep tiers, caller-chosen policies and a machine-actionable verdict; `/llms.txt` and `/.well-known/x402` describe it
* Background sweep from the hot wallet to a cold address above a threshold, through a single signing choke point that only accepts verified payments and sweeps to cold
* Smart-wallet payments (Squads and other CPI-paying wallets): behaviour-based verification by simulation, capped compute budget, and a post-settlement check of the real wSOL delta
* x402 verdicts use the trained scorer's assessment (`model_version` in the response)
* `mcp/`: an MCP server (`check_store`, `explain_flag`) that pays checks from the agent's own wallet, wraps SOL into wSOL when needed, and enforces a per-call and per-session spend cap
* Readiness reports the x402 status, the hot wallet's balances and alerts (sweeps failing, low fee reserve, outflows to anywhere but cold); payments pause on the last two

### Security

* x402 fee drain closed: payments may not reference slot-dependent sysvars or use address lookup tables, may run only known wallet programs (System, Token, ATA, Compute Budget, Memo, Squads v3/v4, Lighthouse; checked at top level and, from the simulation, by CPI), and may set a priority fee of at most 5% of the tier's price. A failed-settlement breaker (per payer, per visitor network, global, per hour) answers `503 SETTLEMENTS_PAUSED` once failures that cost the hot wallet a fee pile up
* Readiness shows the hot wallet's balances and the alert names only with `VERIDEX_ADMIN_TOKEN` (others get the alert count)
* Paddle webhook now fails closed when no secret is configured, rejects stale signatures, and can no longer modify admin or blocked accounts
* Image and review-page downloads are protected against SSRF (private/link-local addresses blocked, redirects re-validated, size capped)
* Login no longer leaks whether a username exists through response timing; bcrypt runs off the event loop
* Internal error details are no longer returned to API clients
* Outbound fetches connect to the address that passed the SSRF check, closing a DNS-rebinding gap
* Request bodies are capped (`VERIDEX_MAX_PAGE_BYTES`, default 2 MiB) before parsing
* All dependencies upgraded; `pip-audit` reports no known vulnerabilities (was ~180 across 20 packages, including starlette, Pillow and python-multipart)

### Bug Fixes

* x402 correctness: the post-settlement check uses only the landed transaction's metadata (the token balance can't tell a payment from a concurrent credit purchase; an unindexed transaction stands on its confirmation, logged); a payment that already landed (or failed) is looked up before it would be sent again; every client signature is kept when co-signing (multi-signer payments used to lose all but the first); non-ASCII session tokens and nonces answer 401 instead of 500; trial and pairing limits count an IPv6 /64 as one visitor, and expired pairings no longer count towards the pairing cap
* MCP: the spend guard gives a reservation back only when Veridex refused before taking the money; a timeout is retried once with the same payment (Veridex answers it idempotently), and a timeout, 5xx/524 or unreadable answer is reported as `status: unknown` with the payment kept counted. `check_store` results now have `status` (`ok`, `not_paid`, `pending`, `unknown`), `ok`, `http_status`, `payment` and, when it matters, `next_step`
* x402 settlement robustness: retrying with the same payment returns the same answer instead of `duplicate_settlement` (and a payment the network never accepted can be retried); a payment still pending at the timeout is credited if it lands later; a paid page that can't be assessed (score `-1`) is refunded; the analysis is capped at 60 s; bot-check pages are refused before payment (`PAGE_BLOCKED`); `/v1/check` is rate-limited per visitor network (`429 RATE_LIMITED`); page fetches have a total timeout; RPC transport errors are `RpcError`s and confirmation polling (every 0.4 s) survives them; one pooled RPC connection; database queries time out after 10 s
* Swaps: only Jupiter's aggregator, the token programs and compute budget may run at top level of a swap transaction
* The outflow audit no longer raises a compromise signal for failed transactions or ones the hot wallet didn't sign, pages back to the last audited transaction instead of looking at the newest 20 (a burst of payments could hide an outflow), keeps a bounded memory, and its sticky alerts can be cleared by the operator (`POST /v1/admin/x402/clear-alerts`) instead of only by a restart
* A credit purchase, or a memo naming a wallet, no longer uses up that wallet's free trial (a `trial_granted` flag replaces "has a row")
* Passwords longer than 72 bytes caused a 500 on login
* Relative "see reviews" links were resolved against the site root instead of the page
* Daily quota now resets at midnight UTC regardless of database timezone
* Reverse image search reported "no dropship domains" when every search had failed
* `httpx` was only a dev dependency but is used at runtime

### Maintenance

* **Breaking:** removed username/password accounts, the signup key, Paddle billing and webhooks, admin routes, tiers and daily limits, and the analysis log (migration 4 drops `accounts` and `analyze_log`); `bcrypt` and the email-validation extra are gone. `scripts/grant_credits.py` replaces `promote_admin.py`
* LLM provider is configurable: any OpenAI-compatible API (`VERIDEX_LLM_BASE_URL`), Scaleway Generative APIs with `mistral-small-3.2-24b-instruct-2506` by default, replacing `gpt-4o-mini` (removed from OpenAI's API on 2027-01-14). `VERIDEX_OPENAI_API_KEY` is now `VERIDEX_LLM_API_KEY`
* Structured LLM output uses tool calling
* A failing or hanging skill degrades to "unavailable" instead of failing the analysis; analyses have an overall deadline (504)
* Token usage per model is logged for every analysis
* `GET /v1/health/ready` readiness probe
* Python 3.14; `uv_build` build backend; pydantic-settings replaces Dynaconf and `settings.toml`; pure ASGI middleware replaces `BaseHTTPMiddleware`; `python-multipart` dropped (unused)
* CI actions pinned by commit SHA, `pip-audit` in CI and `make audit`, Forgejo CI workflow; local Postgres bound to 127.0.0.1

* Removed the Reddit brand-reputation skill and the `praw` dependency
* Shared, configurable LLM client (`VERIDEX_LLM_MODEL`) replaces twelve hard-coded instances
* Unit and Postgres-backed integration test suites; strict mypy passes
* Non-root Docker image, docker compose with Postgres, simplified CI
* MIT license

## [0.5.0](https://github.com/kroshtan/veridex_backend/compare/0.4.0...0.5.0) (2026-03-02)

### Features

* add admin functionality ([3a42239](https://github.com/kroshtan/veridex_backend/commit/3a42239e62b8f9be4c4ac1a247bfe6e400668f06))

## [0.4.0](https://github.com/kroshtan/veridex_backend/compare/0.3.0...0.4.0) (2026-03-01)

### Features

* Add paddle for payment ([c8380f4](https://github.com/kroshtan/veridex_backend/commit/c8380f4878e07839156484f48d8f25bb95a82704))

## [0.3.0](https://github.com/kroshtan/veridex_backend/compare/0.2.5...0.3.0) (2026-03-01)

### Features

* protect signup endpoint + lowercase usernames ([44c2486](https://github.com/kroshtan/veridex_backend/commit/44c248637b827a5a2cc54547aaaa8bb23cccfad2))

## [0.2.5](https://github.com/kroshtan/veridex_backend/compare/0.2.4...0.2.5) (2026-02-28)

## [0.2.4](https://github.com/kroshtan/veridex_backend/compare/0.2.3...0.2.4) (2026-02-27)

### Bug Fixes

* imports ([eb84617](https://github.com/kroshtan/veridex_backend/commit/eb8461728d50812b5f93d6609a00c115fc19e3b0))
* imports ([8ace246](https://github.com/kroshtan/veridex_backend/commit/8ace2463eaafe916726aa9c93779e1f37b278cc8))

## [0.2.3](https://github.com/kroshtan/veridex_backend/compare/0.2.2...0.2.3) (2026-02-27)

### Bug Fixes

* Import error ([16cd4a6](https://github.com/kroshtan/veridex_backend/commit/16cd4a6ab67c780fd359e613750ae984fbc4a54e))

## [0.2.2](https://github.com/kroshtan/veridex_backend/compare/0.2.1...0.2.2) (2026-02-27)

### Bug Fixes

* Actually fix dockerfile ([f4d703c](https://github.com/kroshtan/veridex_backend/commit/f4d703cc9941f57d3f3f27da97bc84f93b3b4896))

## [0.2.1](https://github.com/kroshtan/veridex_backend/compare/0.2.0...0.2.1) (2026-02-27)

### Bug Fixes

* Dockerfile properly installs project package ([3751677](https://github.com/kroshtan/veridex_backend/commit/3751677fcebe481f00846bb0b1d4b8d6e1848de3))

## [0.2.0](https://github.com/kroshtan/veridex_backend/compare/0.1.1...0.2.0) (2026-02-27)

### Features

* add account access from web portal ([50d200e](https://github.com/kroshtan/veridex_backend/commit/50d200e800d94efa145faa43f5c5cf14487127b9))

## [0.1.1](https://github.com/kroshtan/veridex_backend/compare/0.1.0...0.1.1) (2026-02-27)

## [0.1.0](https://github.com/kroshtan/veridex_backend/compare/0.0.1...0.1.0) (2026-02-26)

### Features

* First release ([a295da4](https://github.com/kroshtan/veridex_backend/commit/a295da439eda14ccab931ddcc73f623e140e2b5c))

## 0.0.1 (2026-02-26)
