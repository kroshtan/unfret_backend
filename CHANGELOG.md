# Changelog

## Unreleased

Open-source release.

### Features

* Credits door replaces accounts: Sign-In With Solana (stateless nonces and session tokens; the signature grants no spending rights) and a lamport balance per wallet, the only user state stored. `/v1/analyze` (and its stream) debits atomically and refunds pages that can't be analysed and failed analyses
* Buying credits with SOL: `/v1/pricing`, `/v1/credits`, `/v1/credits/purchase-tx` (unsigned transfer + Solana Pay link) and a QR endpoint; a scanner credits finalized incoming transfers to the hot wallet exactly once (cursor moved by compare-and-swap), with purchase bonuses
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

* Paddle webhook now fails closed when no secret is configured, rejects stale signatures, and can no longer modify admin or blocked accounts
* Image and review-page downloads are protected against SSRF (private/link-local addresses blocked, redirects re-validated, size capped)
* Login no longer leaks whether a username exists through response timing; bcrypt runs off the event loop
* Internal error details are no longer returned to API clients
* Outbound fetches connect to the address that passed the SSRF check, closing a DNS-rebinding gap
* Request bodies are capped (`VERIDEX_MAX_PAGE_BYTES`, default 2 MiB) before parsing
* All dependencies upgraded; `pip-audit` reports no known vulnerabilities (was ~180 across 20 packages, including starlette, Pillow and python-multipart)

### Bug Fixes

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
