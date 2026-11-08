# Veridex

[![CI](https://github.com/kroshtan/veridex_backend/actions/workflows/ci.yaml/badge.svg)](https://github.com/kroshtan/veridex_backend/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)

**Veridex scores e-commerce product pages for scam and dropshipping signals.** A browser extension sends the HTML of
the page the user is viewing; the backend runs a [LangGraph](https://github.com/langchain-ai/langgraph) pipeline of
independent analysis "skills" in parallel and has an LLM judge condense their evidence into a 0–100 reliability score
with a one-line explanation.

```json
POST /v1/analyze  →  { "score": 18, "explanation": "New domain, AliExpress-sourced images and templated 5-star reviews." }
```

> **Project status: prototype.** Veridex is an exploratory prototype, not a finished product. It was built to try out
> what an agentic LLM pipeline can do for shopping-fraud detection, and to put a modern Python stack to work
> (FastAPI, LangGraph, structured LLM output, asyncpg, Playwright, uv). Accounts, quotas and Paddle billing are
> included to show how it would fit into a real service. It is not maintained for production use.

## How it works

```mermaid
flowchart LR
    A[POST /v1/analyze] --> P[preprocess<br/><sub>strip HTML to text</sub>]
    P --> C{classify<br/><sub>LLM</sub>}
    C -- article / non-product --> X[early exit<br/><sub>score = -1</sub>]
    C -- marketplace listing / storefront --> S1[page_content]
    C --> S2[html_source_signals]
    C --> S3[review_integrity]
    C --> S4[claims_verification]
    C --> S5[domain_age]
    C --> S6[exif_check]
    C --> S7[reverse_image_search]
    S1 & S2 & S3 & S4 & S5 & S6 & S7 --> J[judge<br/><sub>LLM → score + explanation</sub>]
```

Each skill is a small class that reads the shared graph state and appends a plain-text finding. The graph fans out to
all active skills with LangGraph's `Send` API, so they run concurrently, and a reducer merges their findings before
the judge runs.

| Skill | What it checks |
| --- | --- |
| `page_content` | LLM scan of the listing text for scam copy, fake urgency, missing return policy, white-label branding |
| `html_source_signals` | Regex match on known dropship apps (Oberlo, DSers, CJ Dropshipping, …) and supplier CDNs, plus an LLM pass over script sources, meta tags and comments |
| `review_integrity` | Extracts reviews from JSON-LD / microdata (following a "see all reviews" link if needed) and looks for templated praise, rating skew and date bursts |
| `claims_verification` | Extracts concrete product claims ("waterproof to 50 m", "FDA approved") and rates their plausibility against price tier and known standards |
| `domain_age` | WHOIS lookup; flags newly registered, short-lived or privacy-hidden domains typical of pop-up stores |
| `exif_check` | Downloads the main product images and compares EXIF metadata (dates, GPS, camera, editing software) with the listing |
| `reverse_image_search` | Reverse-searches product images on Google and reports hits on AliExpress, Temu, DHgate and similar sources |

### Adding a skill

```python
class ShippingTimeSkill(Skill):
    name = "shipping_time"
    description = "Flags multi-week shipping estimates."
    always_run = False  # only runs when "shipping_time" is in the request's `flags`

    async def run(self, state: AnalysisState) -> dict[str, list[str]]:
        ...
        return {"skill_results": ["[shipping_time]\n<finding>"]}
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
| `GET` | `/v1/health/ready` | Readiness probe (checks the database) |

Accounts are `free` (5 analyses/day), `premium` (25/day), `admin` (unlimited) or `blocked`. Pages that are not
analysed (score `-1`) don't count towards the quota.

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

- Scores are LLM judgements over heuristic evidence. They are a signal, not a verdict, and can be wrong in both
  directions.
- Page content is passed to the LLM, so a hostile page can attempt prompt injection to influence its own score.
- `reverse_image_search` automates Google Search through a headless browser. That is against Google's Terms of Service
  and may be blocked at any time. If you run this yourself, replace it with a licensed reverse-image API or remove
  it from `SKILLS`.
- WHOIS data is often redacted and inconsistently formatted across registries.

## License

[MIT](LICENSE)
