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
> (FastAPI, LangGraph, structured LLM output, asyncpg, uv). Accounts, quotas and Paddle billing are
> included to show how it would fit into a real service. It is not maintained for production use.

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
| `reverse_image` | The main product photo on TinEye: copies on Temu, AliExpress & co. and how long it has been online (paid, off unless configured) |

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

Interactive docs are served at `/docs`. All `/v1/accounts/me*`, `/v1/analyze` and `/v1/admin/*` routes use HTTP Basic
auth.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/analyze` | Analyse a page: `{ "page_content": "<html>…", "url": "https://…", "flags": [] }` |
| `POST` | `/v1/accounts` | Create an account (server-to-server; requires the `X-Signup-Key` header) |
| `GET` | `/v1/accounts/me` | Current account |
| `GET` | `/v1/accounts/me/usage` | Analyses used today and remaining quota |
| `GET` | `/v1/admin/accounts` | List/search accounts (admin only) |
| `POST` | `/v1/admin/accounts/{username}/downgrade` \| `/block` | Revoke premium / block, cancelling any Paddle subscription |
| `POST` | `/v1/paddle/webhook` | Paddle Billing webhook (`subscription.activated` / `subscription.canceled`) |
| `GET` | `/v1/health` | Liveness probe |
| `GET` | `/v1/health/ready` | Readiness probe (checks the database; reports x402 status, the hot wallet's balances and alerts) |
| `POST` | `/v1/check` | Pay-per-check for AI agents over [x402](https://www.x402.org), see below |
| `GET` | `/llms.txt` \| `/.well-known/x402` | Machine-readable description of `/v1/check`, its tiers and prices |

Accounts are `free` (5 analyses/day), `premium` (25/day), `admin` (unlimited) or `blocked`. Pages that are not
analysed (score `-1`) don't count towards the quota.

### Pay-per-check for agents (x402)

`POST /v1/check` takes `{url, page_content?, tier: quick|deep, policy: strict|balanced|lenient}` and is paid per call
over x402 v2 with the `exact` scheme on Solana, in **wSOL** (wrapped SOL; no stablecoins). Without a
`PAYMENT-SIGNATURE` header it answers `402` with a `PAYMENT-REQUIRED` header. The client signs a transaction that
pays exactly the price in wSOL into the **hot wallet**'s wSOL account: a plain `TransferChecked` from a standard
wallet, or **a smart wallet** (Squads and other program-controlled wallets) paying by CPI. Veridex co-signs as **fee
payer** with the same hot wallet (agents pay only the price: quick 0.0001 SOL, deep 0.0015 SOL), submits it and waits
for confirmation (`upfront` flow), then runs the analysis and answers with `{action: allow|warn|block, risk_score,
confidence, flags, model_version, …}` plus `PAYMENT-RESPONSE`, scored by the trained scorer. `warn` means "ask your
human". A page that can't be fetched is refused before any money moves. Nothing about the payment or the page is
stored.

Verification is behaviour-based, so any program instructions are accepted as long as: the hot wallet appears in no
instruction (it only pays the fee); every other required signature is present and valid; compute units and priority
fee stay within `VERIDEX_X402_MAX_COMPUTE_UNITS` / `VERIDEX_X402_MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS`; there is exactly
one memo; no signature precompiles; the transaction fits a packet; and a simulation shows the hot wallet's wSOL
account growing by exactly the price with the hot wallet paying no more than the fee. After settlement the real
on-chain delta is checked again; a mismatch gets no analysis and raises an alert. If the analysis fails after a
payment settled, the response says so; refunding such payments as credits to the paying key is planned for phase 4.

Money flows through: a background sweep (one atomic transaction) unwraps the hot wallet's wSOL, recreates its wSOL
account, and sends everything above `VERIDEX_SWEEP_RESERVE_LAMPORTS` (0.01 SOL) to `VERIDEX_COLD_ADDRESS` once the hot
wallet holds more than `VERIDEX_SWEEP_THRESHOLD_LAMPORTS` (0.1 SOL). All signing goes through one function that accepts
only verified payments (as fee payer, never referenced by an instruction) and sweeps to the cold address. Alerts are
logged at error level and shown in readiness: value stuck above 1.5x the threshold (sweeps failing), SOL for fees
below `VERIDEX_X402_MIN_FEE_LAMPORTS` (payments pause), and any transfer from the hot wallet to anywhere but cold,
checked from its recent transactions (payments pause; a compromise signal).

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

Create an account and analyse a page:

```bash
curl -X POST localhost:8080/v1/accounts -H "X-Signup-Key: change-me" -H "Content-Type: application/json" \
  -d '{"username": "demo", "password": "correct-horse", "contact_email": "demo@example.com"}'

curl -s https://example-shop.com/product/123 > page.html
jq -n --rawfile html page.html '{page_content: $html, url: "https://example-shop.com/product/123"}' \
  | curl -X POST localhost:8080/v1/analyze -u demo:correct-horse -H "Content-Type: application/json" -d @-
```

Promote an account to admin with `uv run scripts/promote_admin.py <username>`.

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
| `VERIDEX_TINEYE_API_KEY` / `VERIDEX_TINEYE_MONTHLY_BUDGET` | — / `1000` | Reverse image search; off while the key is empty |
| `VERIDEX_MAX_PAGE_BYTES` | `2097152` | Larger pages are rejected with 413/422 |
| `VERIDEX_SKILL_TIMEOUT_S` / `VERIDEX_ANALYSIS_TIMEOUT_S` | `45` / `90` | A slow skill is skipped; a slow analysis returns 504 |
| `VERIDEX_POSTGRES_URI` | `postgresql://veridex:veridex@localhost:5432/veridex` | Tables are created on startup |
| `VERIDEX_SIGNUP_SECRET` | — | Account creation is disabled while empty |
| `VERIDEX_FREE_DAILY_LIMIT` / `VERIDEX_PREMIUM_DAILY_LIMIT` | `5` / `25` | Per UTC day |
| `VERIDEX_PADDLE_WEBHOOK_SECRET` | — | Webhook requests are rejected while empty |
| `VERIDEX_PADDLE_API_KEY` / `VERIDEX_PADDLE_ENVIRONMENT` | — / `production` | Used to cancel subscriptions from the admin routes |

## Development

```bash
make install           # uv sync + pre-commit hooks
make lint              # ruff, mypy (strict), pydoclint
make test-unit         # fast, no services needed
make test              # unit + integration tests against a throwaway Postgres container
make audit             # pip-audit over the locked dependencies
make run               # uvicorn with auto-reload
```

Unit tests cover the deterministic parts of every skill, the graph routing (with a stubbed LLM), SSRF protection and
webhook signature verification. Integration tests exercise the HTTP API end-to-end against a real Postgres, with the
analysis graph replaced by a test double so no LLM calls are made.

```
src/veridex/
├── main.py            # FastAPI app, lifespan (graph + DB pool), middleware
├── config.py          # typed settings (pydantic-settings)
├── middleware.py      # body size cap, x-process-time header
├── routes/            # analyze, accounts, admin, paddle webhook, health
├── graph/
│   ├── __init__.py    # graph wiring + skill registry
│   ├── nodes.py       # preprocess, classify, early_exit, judge
│   ├── state.py       # shared LangGraph state
│   └── skills/        # one module per skill
├── auth.py            # Basic auth, account tiers
├── db.py              # schema + queries (asyncpg)
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
- **Auth.** Passwords are bcrypt-hashed off the event loop; unknown usernames are checked against a dummy hash so
  they can't be distinguished from wrong passwords by timing.
- **Billing webhooks** fail closed without a configured secret, reject signatures older than five minutes, never
  modify admin or blocked accounts, and only honour a cancellation for the subscription currently linked to the account.

## Limitations and responsible use

- Scores come from a logistic regression over skill features (`src/veridex/scoring/model.json`, trained with
  `python -m evals.train` on a private labelled corpus). They are a calibrated signal, not a verdict, and can be wrong
  in both directions. Part of the training data is synthetic (marketplace pages rendered from public review data),
  so marketplace accuracy on real pages is likely lower than measured.
- Page content is passed to the LLM, so a hostile page can attempt prompt injection to influence its own score. It is
  delimited as untrusted data, and attempts to address the model are detected and count against the page.
- Registration data (RDAP) is often redacted; the first-certificate date is a second, registry-independent age.
- Reverse image search uses the licensed TinEye API, paid per search, with a hard monthly budget
  (`VERIDEX_TINEYE_MONTHLY_BUDGET`). Only the public product image URL is sent.

## License

[MIT](LICENSE)
