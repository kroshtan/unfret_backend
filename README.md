# Unfret

[![CI](https://github.com/kroshtan/unfret_backend/actions/workflows/ci.yaml/badge.svg)](https://github.com/kroshtan/unfret_backend/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)

**Unfret scores e-commerce product pages for scam and dropshipping signals.** A browser extension sends the HTML of
the page the user is viewing; the backend runs a [LangGraph](https://github.com/langchain-ai/langgraph) pipeline of
independent analysis "skills" in parallel, and a small trained model turns their features into a calibrated 0–100
reliability score; an LLM writes the one-line explanation from the signals that moved it.

```json
POST /v1/analyze  →  { "score": 18, "explanation": "New domain, AliExpress-sourced images and templated 5-star reviews." }
```

> **Project status: prototype.** Unfret is an exploratory prototype, not a finished product. It was built to try out
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
| `eu_registry` | Storefronts: VAT ID checked against the EU VIES register (name match) and its check digit (a number that can't exist earns no credit and makes the verdict "careful" at best; a bank account that starts like a VAT ID is ignored), withdrawal right and imprint; outside the EU (a US shop, or one on a non-European country domain or currency) a named company or US business address counts as its imprint, and a missing imprint, VAT ID or return policy is not applicable (neutral), since no law there requires them |
| `us_identity` | US storefronts: a named company (LLC, Inc.), a US business address and phone number; an address that is only a mailbox (PO Box, PMB). Not in the scorer yet |
| `shop_policies` | Storefronts: delivery ranges of 15+ days, returns sent abroad, a return address given only on request, restocking fees above 20%, and "Made in USA" on goods shipped from overseas. Not in the scorer yet |
| `domain_age` | Storefronts: domain registration via RDAP and the first certificate in the CT logs |
| `shop_links` | Storefronts: other web shops with the same analytics/pixel IDs, payment account or (hashed) contact details, one hop, from the shop fingerprint store; how many of them are young (their own registration dates). Local lookup, also in the quick tier; not in the scorer yet |
| `reverse_image` | The main product photo in Unfret's own image index: the same photo on other shops, or in the ABO catalogue. pHash, dHash, a DINOv2-small embedding (run locally) and a colour grid must all agree; own shop and same listing ignored; stock images skipped. See [docs/image-index.md](docs/image-index.md). Deep tier only; not in the scorer yet |

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

Register an instance in `SKILLS` in [`src/unfret/graph/__init__.py`](src/unfret/graph/__init__.py).

## API

Interactive docs are served at `/docs`. `/v1/analyze*` and `/v1/credits` take `Authorization: Bearer <session token>`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/v1/auth/nonce` | Start a Sign-In With Solana: nonce and message fields |
| `POST` | `/v1/auth/verify` | Finish it: `{address, signed_message, signature}` → session token (30 days) |
| `POST` | `/v1/analyze` | Analyse a page, paid from credits: `{ "url", "page_content"?, "page_facts"?, "checkout_content"? }` |
| `POST` | `/v1/analyze/stream` | The same, streamed as server-sent events (`page_type`, `finding`…, `result`) |
| `GET` | `/v1/credits` | The signed-in wallet's balance |
| `GET` | `/v1/pricing` | The price of a check, x402 prices, the packs (by card and with crypto), VAT rules, hot wallet |
| `POST` | `/v1/credits/purchase-tx` | Unsigned SOL or USDC transfer that buys a pack (VAT included), plus a Solana Pay link |
| `GET` | `/v1/credits/solana-pay.svg` | The same purchase as a Solana Pay QR code |
| `GET` | `/v1/credits/quotes/{id}` | Whether a crypto purchase is paid; with its `X-Unfret-Claim`, its invite codes once |
| `POST` | `/v1/invites/redeem` | Redeem an invite `{code}` for the signed-in new wallet or API key: 3 checks |
| `GET` | `/v1/schema/page-facts` | JSON schema of `PageFacts`, for clients that extract facts themselves |
| `GET` | `/v1/showcase` \| `/v1/showcase/{id}` | "Spotted recently": published operator-run checks, see below |
| `POST` | `/v1/shares/preview` \| `/v1/shares` | Share a result as a link, anonymously (no `Authorization`), see below |
| `GET` `DELETE` | `/v1/shares/{id}` | A shared result; delete it with `X-Delete-Code` |
| `POST` | `/v1/shares/{id}/object` | A shop owner's objection: hides the shared result until reviewed |
| `GET` | `/v1/health` | Liveness probe |
| `GET` | `/v1/health/ready` | Readiness probe (checks the database; reports x402 and credits status, hot wallet balances, alerts) |
| `POST` | `/v1/check` | Pay-per-check for AI agents over [x402](https://www.x402.org), see below |
| `GET` | `/llms.txt` \| `/.well-known/x402` | Machine-readable description of `/v1/check`, its tiers and prices |

### Credits (browser extension and platforms)

There are no accounts. A wallet signs in by signing a plain-text [SIWS](https://github.com/phantom/sign-in-with-solana)
message (no spending rights; the nonce is stateless and expires in five minutes) and gets a session token. **The only
user state stored is `wallet → balance in micro-dollars`** (and whether it redeemed an invite): no emails, no
purchase history (the chain is the ledger), no record of what was analysed.

**Pricing.** One setting, `UNFRET_PRICE_PER_CHECK_USD` ($0.20), sets every price: an analysis costs one check in
credits, an x402 deep check costs one check, and credits are sold in packs of 25, 50 and 100 checks
(`src/unfret/credits/packs.py`). A pack's price is its card price ($6, $11.50, $22: the pack's value plus the card
provider's worst-case cut of 6.5% + $0.50, rounded up to $0.50, before the provider's sales tax/VAT); paying with SOL
or USDC costs less, just the pack's value ($5, $10, $20) plus VAT where it applies. The card price is shown as *the*
price and crypto as the discount, never as a card surcharge (EU rules forbid surcharging consumer card payments).
Either way the account is credited the pack's value, so a check costs the same in credits however it was bought.

**VAT on crypto purchases** (`src/unfret/credits/vat.py`). The buyer states their country; the checkout prefills it
from Cloudflare's `CF-IPCountry`, trusted only when Caddy saw the request come from Cloudflare's edge (it forwards
that peer as `X-Edge-Peer`, checked against the vendored ranges). The IP address is never stored or logged. With
`UNFRET_VAT_MODE=nl_under_threshold` (default, while EU cross-border sales stay under €10,000 a year) every EU buyer
pays Dutch VAT (21%) and everyone else none; `per_country` uses each member state's standard rate, vendored from the
EU's TEDB (`make vat-rates-check` compares it and the Cloudflare ranges with their sources, run monthly by
`.forgejo/workflows/vat-rates.yaml`, which fails naming the changed file and the script that rewrites it; the
service logs `vat_rates_stale` 45 days after the table was last rewritten). Card
purchases are sold by the payment provider, which handles their tax.

**Buying.** `/v1/credits/purchase-tx` (and the Solana Pay QR) prices a pack for the buyer's country and stores that as
a quote; the transaction's memo is `unfret:quote:<id>`, so the scanner credits the right account (the payer, another
wallet, or an API key) and records the right VAT. [`/checkout`](https://unfret.kroshtan.com/checkout) does it from a
browser wallet or a phone. A background scanner walks the hot wallet's finalized transactions (a second one its USDC
account) and credits every incoming transfer, top-level or inside a program call: what arrived (USDC at face value,
SOL at its dollar value at crediting time, Pyth SOL/USD, rounded down; it waits rather than guesses while the price is
stale) minus its VAT. Each credit leaves a sales record (day, country, net, rate, VAT, asset, amount, signature; no
IP, no wallet column) in the same database transaction, kept ten years; `GET /v1/admin/sales/<YYYY-Qn>` exports a
quarter as CSV. A transfer without a quote, a manual one, counts as a sale to an unknown country at the Dutch rate;
the older `unfret:credit:<beneficiary>` memo still names who gets it. The cursor moves by compare-and-swap, so each
transfer is credited exactly once. Sweeps, fees and x402 settlements are ignored.

[`/connect`](https://unfret.kroshtan.com/connect) signs a wallet in and hands the session to the browser extension
(`window.postMessage`, relayed by the extension's content script). Both pages are static HTML and vanilla JS with a
strict Content Security Policy: no framework, no third-party requests, Wallet Standard discovery written directly. An analysis costs one check ($0.20),
taken atomically before it runs and refunded when the page can't be analysed (score `-1`) or the analysis fails.

[`/check`](https://unfret.kroshtan.com/check) is the link checker for phones and browsers without the extension: it
sends only the link (`POST /v1/analyze/stream` with `{"url"}`), the server fetches the page itself before charging,
and a page it can't read (an error, a shop that refuses automatic visitors, a bot check, an empty shell that needs a
browser) is a free `422` that the page shows as such, never as a verdict. It installs as a web app with an Android
share target; the sign-in or API key is kept in the browser until sign-out, and there is no history.

There is no free trial: every new wallet or API key starts at zero checks and buys a pack. Grant an API key credit by
hand (support, a store reviewer's test checks) with `POST /v1/admin/keys/<key_id>/grant` and `{"usd_micros": n}`, at
most $10 a grant, with the admin token.

**Invites** (`src/unfret/credits/invites.py`): every credited pack comes with invite codes to give away, one per 25
checks (25 → 1, 50 → 2, 100 → 4), each worth 3 checks, so the 25-pack brings 28 checks in all. An account that never
had any credit (a wallet we've never credited, an API key never topped up) redeems one, once, with its own sign-in
(`POST /v1/invites/redeem`). Only each code's SHA-256 is stored, with the day issued and the day redeemed: no buyer,
no redeemer, no IP. The buyer sees the codes once: the purchase comes with a random claim token (never on chain or
sent to the card provider, stored only hashed); the codes are kept with the quote or card checkout until the first
status request carrying that token takes them and wipes them in one statement. Invites are a gift: no VAT, no sales
record. `GET /v1/admin/invites` counts those outstanding.

### Card payments for API keys (Polar)

API keys can also be topped up by card through [Polar](https://polar.sh), the Merchant of Record: Polar sells the pack,
collects the buyer's details and sales tax/VAT, and issues the invoice; this service only learns that a key was paid
for (`src/unfret/credits/polar.py`, `src/unfret/credits/fiat.py`). Card payments are **off** (`/v1/pricing` says
`fiat.available: false`, `/v1/credits/fiat-checkout` answers 503, readiness says `"fiat": "off"`) until all of these
are set:

| Variable | |
| --- | --- |
| `UNFRET_POLAR_ACCESS_TOKEN` | An **Organization Access Token** (Polar → Settings → Developers) with the single scope **`checkouts:write`**. Nothing else is called: no product, order, customer or refund reads |
| `UNFRET_POLAR_ENVIRONMENT` | `production` (https://api.polar.sh, real money) or `sandbox` (https://sandbox-api.polar.sh; the default, also when empty). The token, products and webhook must come from the same environment |
| `UNFRET_POLAR_PRODUCTS` | The Polar product of every pack: `25=<product id>,50=<product id>,100=<product id>` |
| `UNFRET_FIAT_WEBHOOK_SECRET` | The webhook endpoint's `whsec_…` secret |

**In Polar:** one one-time product per pack, priced at the pack's card price **in USD** before tax ($6.00, $11.50,
$22.00 at $0.20 a check). A checkout is created for the pack's product with `currency: "usd"`, discount codes off,
our account id (`key:<id>`) in its metadata and `/checkout?paid=1` as the success URL; nothing about the buyer is
sent. If Polar answers with another currency or price, the checkout is refused (503) before anyone pays.

**Webhook:** add an endpoint at `https://<host>/v1/webhooks/fiat` (format *Raw*), subscribed to **`order.paid`**,
**`order.refunded`**, **`refund.created`** and **`refund.updated`**. A paid order is credited the pack's value once,
and only if it is in USD, for a configured pack's product, at exactly that pack's card price before tax
(`net_amount`); anything else credits nothing and logs `polar_order_not_credited` (order id, reason, currency and
amount; never the buyer) for a person to sort out in Polar. Refunds take back the refunded share of the credits
(`refunded_amount`, before tax); a refund that settles a dispute Polar lost takes back the whole order. Polar has no
webhook for disputes themselves. Other events are acknowledged and ignored.

### "Spotted recently": the showcase

No demo data: the site shows **real checks of real product pages**, run by the operator and each
reviewed by the owner before it is published (`unfret.showcase`, migration 13). Never a user's check: a case is
always a fresh server-side check of a public page (the same `fetch_page` + full graph path as a URL-only check,
without any account or charge), and the `showcase_cases` table has no column that could hold an account, payer or
report. A case stores the product title, the shop's host, the product URL, when it was checked, the analysis
response as a paying user gets it (without balance or report token) and optionally a screenshot (in the row, at most
800 kB, since the container's files go with each deploy). It is a `draft` until published; `removed` takes it down
for good, keeps the reason and drops the screenshot.

```bash
make showcase ARGS="add https://shop.example/product"   # the server fetches and checks it: a draft
make showcase ARGS="list --status draft"
make showcase ARGS="show 3"                              # incl. exactly what the public would see
make showcase ARGS="screenshot 3 shot.png"               # optional: PNG, JPEG or WebP
make showcase ARGS="publish 3"
make showcase ARGS="remove 3 --reason 'shop owner asked'"
```

The CLI talks to the running service's admin API (`/v1/admin/showcase…`, `UNFRET_URL` and the admin token as for
`make reports`). In `make reports`, when a label agrees with the model, `p` promotes the page: only its address is
sent, and the server checks it again as the operator; nothing from the report goes into the case.

The public sees published cases only: `GET /v1/showcase` (newest first, `Cache-Control: public, max-age=300`), a
"Spotted recently" section on `/` and `/check`, and a page per case, `/showcase/{id}`, in the link checker's
presentation (the same renderer, `static/check-render.js`). Each case shows the day it was checked ("the page may
have changed since"), the shop, a `rel="nofollow noopener"` link to the product page, the screenshot if any, and a
route for shop owners to object. The wording is factual: the bands are phrased as what Unfret would tell a shopper
about the listing ("Unfret would warn you about this listing"), there is no score, and the words "scam" and "fraud"
never appear (a headline or quote containing one is left out; `show` lists what was).

### Sharing a result

A shopper can make their own check public as a link (`unfret.shares`, migration 16), and nothing on it says who
they are. Every assessed result carries a `share_token`: an HMAC over the product address (without fragment,
credentials and tracking parameters), a hash of the result as a share shows it, whether the check included the
checkout page, the product title and an expiry (7 days). It names no account. `POST /v1/shares/preview` shows
exactly what would be public; `POST /v1/shares` stores it. Both are sent **without `Authorization`** (a request with
one is refused) and the token must match the url and the result exactly, so a share can only show a verdict Unfret
really gave for that page. A check that included the checkout page is never shared.

What is public, and stored in `shared_results`: the showcase's wording of the verdict (no score, never "scam" or
"fraud"), without the page's own words (a reason whose detail quotes the page keeps only its title, a quoting
headline becomes the band's catalogue sentence, flags keep their labels but not their evidence), the product title,
the shop, the product link and the day of the check. Plus a SHA-256 hash of the delete code (shown once), the expiry
(90 days, on a day boundary) and, after an objection, why it is hidden. No account, wallet, IP address, time of day
or view count; expired rows go in the hourly housekeeping. Limits: 20 shares per visitor network per day (in
memory) and a daily cap.

`/r/{id}` shows it in the link checker's presentation (the showcase renderer): "Checked by an Unfret user on …; the
page may have changed", a `nofollow` link to the product, `X-Robots-Tag: noindex` and a robots meta tag. The sharer
deletes it with `DELETE /v1/shares/{id}` and `X-Delete-Code`; a shop owner's `POST /v1/shares/{id}/object` hides it
at once, and the operator reviews it under the admin token: `GET /v1/admin/shares` (hidden ones),
`POST /v1/admin/shares/{id}/unhide`, `DELETE /v1/admin/shares/{id}`. On `/check`, "Share this result" shows the
preview, then shares through the phone's share sheet or copies the link, and shows the delete code once.

### Pay-per-check for agents (x402)

`POST /v1/check` takes `{url, page_content?, tier: quick|deep, policy: strict|balanced|lenient}` and is paid per call
over x402 v2 with the `exact` scheme on Solana, in **wSOL** (wrapped SOL) or **USDC**. Without a
`PAYMENT-SIGNATURE` header it answers `402` with a `PAYMENT-REQUIRED` header listing wSOL first, then USDC. The client
signs a transaction that pays the price into the **hot wallet**'s token account for that mint: a plain `TransferChecked` from a standard
wallet, or **a smart wallet** (Squads and other program-controlled wallets) paying by CPI. Unfret co-signs as **fee
payer** with the same hot wallet (agents pay only the price: quick $0.01, deep one check, $0.20), submits it and waits
for confirmation (`upfront` flow), then runs the analysis and answers with `{action: allow|warn|block, risk_score,
confidence, flags, model_version, …}` plus `PAYMENT-RESPONSE`, scored by the trained scorer. `warn` means "ask your
human". Under `balanced` and `lenient` an agent is never warned about a page a shopper would see as "reliable" or
"concerns" (the `band`); `strict` warns from risk 0.25 whatever the band. A page that can't be fetched is refused
before any money moves. Nothing about the payment or the page is stored.

The two tiers (`QUICK_SKILLS` in `src/unfret/x402/verdict.py`; each 402 option says what it buys in
`extra.includes`, as do `/.well-known/x402` and `/llms.txt`):

| | quick ($0.01) | deep ($0.20) |
| --- | --- | --- |
| What runs | The page itself, by rules: `listing`, `pricing` (on-page discount checks, no price history), `html_source_signals`, `dark_patterns`, `injection`, `eu_registry` (no VIES), `us_identity`, `shop_policies`, `shop_links` (our own fingerprint store) | Everything: the same analysis as `/v1/analyze` and the extension, adding the LLM read (`content`), `reviews`, `reverse_image`, the price history, `domain_age` and VIES |
| LLM / image index | Never (classification by rules: a page without a product and a price is not assessed, and refunded) | Yes |
| Scorer | `model-quick.json`, trained on the quick features only (`evals.train --quick`, variant C) | `model.json` |
| TEST (333 pages) | AUC 0.746 (US 0.782, EU 0.673; storefronts 0.769); legit flagged 29/143; recall 0.60 | AUC 0.894 (US 0.964, EU 0.828; storefronts 0.836); legit flagged 25/143; recall 0.82 |

Quick is a first-pass filter: it catches what a page shows by itself (dropshipping and review-import apps, fake
discounts, timers, pre-ticked add-ons, text aimed at AI, missing legal information, dropshipping terms), and misses
what only reading the listing, its reviews or its photo reveals. Check what matters with deep.

Each `accepts` option names the network in CAIP-2 form (`solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1` on devnet), as do
`/.well-known/x402` (`network`, with the cluster name in `network_name`) and `/llms.txt`. Its `extra` carries the
mint's `decimals` (9 for wSOL, 6 for USDC), `quoteToleranceBps` (how far a payment may be from this quote: 200 for
wSOL, 0 for USDC; clients should re-fetch the 402 right before paying), `constraints` (below) and `feeCapNote`:
`constraints.maxPriorityFeeLamports` is in lamports on every option, USDC included, because the hot wallet pays the
fee in SOL; it is `UNFRET_X402_MAX_PRIORITY_FEE_BPS` of the tier's dollar price converted at the SOL/USD quote. A
refused payment answers `402` again with `error` saying why; when the simulation failed (`simulation_failed`) it adds
`reason`, a short cause derived from the simulation's error and logs: `insufficient_funds`,
`source_token_account_missing`, `mint_mismatch`, `owner_mismatch`, `account_not_found`, `blockhash_not_found`, the
error class in snake case (`invalid_account_data`) or `custom_program_error_<code>` for another program's own error.

Verification is behaviour-based, so any program instructions are accepted as long as: the hot wallet appears in no
instruction (it only pays the fee); every other required signature is present and valid; compute units and priority
fee stay within `UNFRET_X402_MAX_COMPUTE_UNITS` / `UNFRET_X402_MAX_COMPUTE_UNIT_PRICE_MICROLAMPORTS`; there is exactly
one memo; no signature precompiles; the transaction fits a packet; and a simulation shows the hot wallet's token
account growing by exactly the accepted amount with the hot wallet paying no more than the fee.

The hot wallet pays the fee of every transaction it submits, even one that fails on chain, so a payment must not be
able to pass the simulation and then fail. It may reference no slot-dependent sysvar (`Clock`, `SlotHashes`,
`SlotHistory`, `RecentBlockhashes`, `EpochRewards`) and use no address lookup table, and every program it runs, top
level or by CPI (from the simulation), must be a known wallet program: System, Token, ATA, Compute Budget, Memo,
Squads v3 and v4, Lighthouse, plus `UNFRET_X402_EXTRA_PROGRAMS`. An allowlist is needed because any other program
could branch on the slot through the `Clock` syscall, which no account check sees. Its priority fee is capped at
`UNFRET_X402_MAX_PRIORITY_FEE_BPS` (5%) of the tier's price. What remains, such as a payer racing its own payment, is
bounded by a breaker: after `UNFRET_X402_BREAKER_PER_PAYER` (3) failed settlements in an hour from one wallet,
`UNFRET_X402_BREAKER_PER_IP` (5) from one visitor network (IPv6 per /64) or `UNFRET_X402_BREAKER_GLOBAL` (20) in
total, `/v1/check` answers `503 SETTLEMENTS_PAUSED` with `Retry-After` (a global trip also raises the
`settlements_failing` alert).

**Prices are canonical in US dollars** (`UNFRET_X402_PRICE_*_USD_MICROS`, `UNFRET_ANALYSIS_PRICE_USD_MICROS`; one
dollar = 1,000,000, which is also USDC's base unit). USDC pays them exactly. SOL amounts are those prices converted at
quote time with Pyth's SOL/USD price (rounded up), read on chain from the Pyth receiver's `PriceUpdateV2` account
(`UNFRET_PYTH_SOL_USD_ACCOUNT`): fully verified, at most `UNFRET_PYTH_MAX_AGE_S` (60 s) old, confidence at most
`UNFRET_PYTH_MAX_CONF_BPS` (1%). wSOL isn't offered while the oracle fails those checks (the 402 then lists USDC
only, or answers 503 `PRICE_UNAVAILABLE` if the USDC account doesn't exist yet); a wSOL payment may be up to
`UNFRET_QUOTE_TOLERANCE_BPS` (2%) off the current quote, since the price moves between the 402 and the payment. On
devnet the oracle is read from mainnet (Pyth's devnet copy updates too rarely).
All prices come from one place, `unfret.prices.PriceBook`. After settlement the real
on-chain delta is checked again; a mismatch gets no analysis and raises an alert. If the analysis fails or takes
longer than `UNFRET_X402_ANALYSIS_TIMEOUT_S` (60 s) after a payment settled, or the page can't be assessed (score
`-1`), the price is refunded as credits to the paying wallet (never as an outgoing transfer), usable after signing in
with it. A payment submitted but not confirmed in time answers `402 SETTLEMENT_PENDING` with its signature; if it
lands within two minutes its price is credited the same way. Retrying with the same payment returns the same answer
for 15 minutes (concurrent retries wait for the first) and is never charged twice. Bot-check interstitials
(Cloudflare, DataDome, PerimeterX, Amazon) are refused as `422 PAGE_BLOCKED` before any money moves, and
`/v1/check` takes `UNFRET_X402_CHECK_PER_MINUTE` (30) requests per visitor network.

Every error from the API is JSON with `detail` and `error_code` (validation errors add `errors`). For `/v1/check`:

| error_code | HTTP | charged | what to do |
| --- | --- | --- | --- |
| `PAYMENT_REQUIRED` | 402 | no | pay one of `accepts` and retry with PAYMENT-SIGNATURE; `error` says why (`reason`: why a simulation failed) |
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

Money flows through, and nothing is ever swapped: SOL reaches the cold wallet as SOL and USDC as USDC (converting it is
the operator's business, outside the product). A background sweep (one atomic transaction) unwraps the hot wallet's
wSOL, recreates its wSOL account, creates its USDC account if missing, and sends everything above
`UNFRET_SWEEP_RESERVE_LAMPORTS` (0.01 SOL) to `UNFRET_COLD_ADDRESS` once the hot wallet holds more than
`UNFRET_SWEEP_THRESHOLD_LAMPORTS` (0.1 SOL). A second sweep moves all of the hot wallet's USDC into the cold address's
USDC account once it holds `UNFRET_USDC_SWEEP_THRESHOLD_UNITS` (5 USDC), or at least 0.5 USDC older than a day: an
idempotent create of that account (the hot wallet pays its rent, and waits while that would take its SOL under the fee
reserve) and one `TransferChecked`, on mainnet and devnet alike. All signing goes through one function that accepts only
verified payments (as fee payer, never referenced by an instruction) and those two sweep shapes, re-checked instruction
by instruction against the cold address. Alerts are logged at error level and counted in readiness (named, with the
balances, for `Authorization: Bearer $UNFRET_ADMIN_TOKEN`): value stuck above 1.5x the threshold (sweeps failing), SOL
for fees below `UNFRET_X402_MIN_FEE_LAMPORTS` (payments pause), USDC piling up beyond 3x its sweep threshold, and any
value leaving the hot wallet except to cold or cold's USDC account (a token transfer by the hot wallet is accepted only
as a `transferChecked` of USDC into cold's USDC account, so a swap is flagged too; payments pause, a compromise
signal). The audit pages through
every transaction since the last one it checked, skipping failed ones and ones the hot wallet didn't sign (neither can
move its value). A compromise signal and post-settlement mismatches stay raised until the operator, having checked
the logged transactions (and rotated the hot wallet if it was real), clears them:
`curl -X POST -H "Authorization: Bearer $UNFRET_ADMIN_TOKEN" https://…/v1/admin/x402/clear-alerts`.

The feature is off (`503`) until `UNFRET_HOT_WALLET_KEY` and `UNFRET_COLD_ADDRESS` (a cold wallet; the server never
holds its key) are set. Devnet is the default, and mainnet needs `UNFRET_X402_ALLOW_MAINNET=true`. Run one instance:
the sweeper isn't coordinated across processes. [`mcp/`](mcp/) is an MIT-licensed MCP server that pays these checks from an agent's own wallet within a
spend cap.

## Running it

**Requirements:** [uv](https://docs.astral.sh/uv/), Docker, and an API key for an OpenAI-compatible LLM API
([Scaleway Generative APIs](https://www.scaleway.com/en/generative-apis/) by default).

```bash
cp .env.example .env       # add your UNFRET_LLM_API_KEY
docker compose up --build  # API on http://localhost:8080, Postgres alongside
```

Set `UNFRET_SESSION_SECRET` and `UNFRET_ADMIN_TOKEN` (e.g. `openssl rand -hex 32` each), create an API key at
`http://localhost:8080/checkout` (`TOKEN` below; a key is `ufk_<key_id>_<secret>`), give it credits and analyse a page:

```bash
curl -X POST localhost:8080/v1/admin/keys/<key_id>/grant -H "Authorization: Bearer $UNFRET_ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{"usd_micros": 2000000}'

curl -s https://example-shop.com/product/123 > page.html
jq -n --rawfile html page.html '{page_content: $html, url: "https://example-shop.com/product/123"}' \
  | curl -X POST localhost:8080/v1/analyze -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d @-
```

### Configuration

Settings are defined and validated in [`src/unfret/config.py`](src/unfret/config.py) (with
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)). Override any of them with a
`UNFRET_`-prefixed environment variable or a git-ignored `.env` file.

| Variable | Default | |
| --- | --- | --- |
| `UNFRET_LLM_API_KEY` | — | Required |
| `UNFRET_LLM_BASE_URL` | `https://api.scaleway.ai/v1` | Any OpenAI-compatible chat completions API |
| `UNFRET_LLM_MODEL` | `mistral-small-3.2-24b-instruct-2506` | Used by every node and skill; must support tool calling |
| `UNFRET_EMBEDDING_MODEL` | `bge-multilingual-gemma2` | Review/title similarity, same provider |
| `UNFRET_NETWORK_LOOKUPS` | `true` | RDAP, certificate transparency and EU VIES lookups about the shop |
| `UNFRET_FINGERPRINTS` / `UNFRET_CONTACT_HASH_KEY` | `true` / derived from the session secret | Shop fingerprint store; the key for contact hashes (no key and no session secret: contacts aren't stored) |
| `UNFRET_IMAGE_MODEL_PATH` / `UNFRET_IMAGE_THREADS` | `models/dinov2-small-int8.onnx` / `1` | The image index's embedding model (baked into the container image, pinned by SHA-256) and its CPU threads; the photo check is off while the model file is missing. The catalogue seed is imported by hand: see docs/image-index.md |
| `UNFRET_MAX_PAGE_BYTES` | `2097152` | Larger pages are rejected with 413/422 |
| `UNFRET_SKILL_TIMEOUT_S` / `UNFRET_ANALYSIS_TIMEOUT_S` | `45` / `90` | A slow skill is skipped; a slow analysis returns 504 |
| `UNFRET_ANALYZE_FETCH_PER_MINUTE` | `10` | Pages fetched per account per minute for URL-only analyses (the `/check` link checker); unreadable pages are free, so this bounds them |
| `UNFRET_POSTGRES_URI` | `postgresql://unfret:unfret@localhost:5432/unfret` | Tables are created on startup |
| `UNFRET_SESSION_SECRET` | — | Signs nonces and session tokens; sign-in and credits are off (503) while empty |
| `UNFRET_SIWS_DOMAIN` | `unfret.kroshtan.com` | Sign-in messages must be bound to this domain |
| `UNFRET_PRICE_PER_CHECK_USD` | `0.20` | The price of a check: an analysis, an x402 deep check, and what packs are made of |
| `UNFRET_X402_PRICE_QUICK_USD_MICROS` | `10000` | The x402 quick tier ($0.01) |
| `UNFRET_PACKS` | `[25,50,100]` | Pack sizes in checks |
| `UNFRET_INVITE_PER_CHECKS` / `UNFRET_INVITE_CHECKS` | `25` / `3` | One invite per this many checks in a pack (0: none issued), worth this many checks |
| `UNFRET_CARD_FEE_PCT` / `UNFRET_CARD_FEE_FLAT_USD` / `UNFRET_CARD_PRICE_STEP_USD` | `6.5` / `0.50` / `0.50` | How a pack's card price is derived |
| `UNFRET_VAT_MODE` | `nl_under_threshold` | VAT on crypto purchases: Dutch VAT for all EU buyers, or `per_country` |
| `UNFRET_QUOTE_TOLERANCE_BPS` | `200` | How far a wSOL payment may drift from the quote |
| `UNFRET_HOT_WALLET_KEY` / `UNFRET_COLD_ADDRESS` | — | x402 and credit purchases (see above); off while empty |
| `UNFRET_SHARES_PER_VISITOR_PER_DAY` / `UNFRET_SHARES_DAILY_CAP` / `UNFRET_SHARE_DAYS` | `20` / `1000` / `90` | Shared results: per visitor network per day, in total per day, and how long one stays up |
| `UNFRET_ADMIN_TOKEN` | — | Bearer token for `/v1/admin/…` and the readiness details (balances, alert names); off while empty |
| `UNFRET_POLAR_ACCESS_TOKEN` / `UNFRET_POLAR_ENVIRONMENT` / `UNFRET_POLAR_PRODUCTS` / `UNFRET_FIAT_WEBHOOK_SECRET` | — / `sandbox` / — / — | Card top-ups for API keys through Polar (see above); off unless all are set |

## Development

```bash
make install           # uv sync + pre-commit hooks
make lint              # ruff, mypy (strict), pydoclint
make test-unit         # fast, no services needed
make test              # unit + integration tests against a throwaway Postgres container
make e2e               # the end-to-end sweep against dev (devnet; needs the UNFRET_E2E_* settings, see Deploying)
make audit             # pip-audit over the locked dependencies
make vat-rates-check   # vendored EU VAT rates and Cloudflare ranges vs their sources (CI runs it monthly)
make run               # uvicorn with auto-reload
```

Unit tests cover the deterministic parts of every skill, the graph routing (with a stubbed LLM), SSRF protection, x402
payment verification and SIWS. Integration tests exercise the HTTP API end-to-end against a real Postgres (sign-in,
concurrent debits, the credits scanner), with the analysis graph replaced by a test double so no LLM calls are made.

```
src/unfret/
├── main.py            # FastAPI app, lifespan (graph + DB pool), middleware
├── config.py          # typed settings (pydantic-settings)
├── middleware.py      # body size cap, x-process-time header
├── routes/            # analyze, sign-in, credits, x402 check, health
├── graph/
│   ├── __init__.py    # graph wiring + skill registry
│   ├── nodes.py       # preprocess, classify, early_exit, judge
│   ├── state.py       # shared LangGraph state
│   └── skills/        # one module per skill
├── credits/           # SIWS, the dollar ledger, pricing, purchase scanner
├── showcase/          # "Spotted recently": operator-run cases, public wording, review CLI
├── shares.py          # shared results: share tokens, public wording, storage
├── x402/              # pay-per-check: verification, hot wallet, sweeps
├── auth.py            # bearer sessions
├── db.py, migrations.py  # versioned schema + queries (asyncpg)
├── llm.py             # shared chat model
└── net.py             # SSRF-safe HTTP fetching
```

## Deploying

Two environments, both deployed with [Kamal](https://kamal-deploy.org) to the same server (behind the forge's Caddy
and kamal-proxy), one Kamal service (`unfret`) with a destination each, and each destination with its own database accessory, hot
wallet and session secret:

| | dev | production |
| --- | --- | --- |
| URL | https://dev.unfret.kroshtan.com | https://unfret.kroshtan.com |
| Solana | devnet (test money) | **mainnet** |
| Kamal destination | `config/deploy.dev.yml` (containers `unfret-web-dev-<sha>`, DB `unfret-dev-db`) | `config/deploy.production.yml` (`unfret-web-production-<sha>`, `unfret-prod-db`) |
| Deployed | **automatically** on every push to `main`, after the checks (`.forgejo/workflows/ci.yaml`, job `deploy-dev`) | **only by hand** (`.forgejo/workflows/deploy-production.yaml`) |
| Repo secrets | `DEV_POSTGRES_PASSWORD`, `DEV_SESSION_SECRET`, `DEV_HOT_WALLET_KEY`, `DEV_COLD_ADDRESS`; optional `DEV_ADMIN_TOKEN`, and for card payments `DEV_POLAR_ACCESS_TOKEN`, `DEV_POLAR_ENVIRONMENT`, `DEV_POLAR_PRODUCTS`, `DEV_FIAT_WEBHOOK_SECRET`; for the e2e sweep (production's gate) `DEV_E2E_PAYER_KEY`, `DEV_E2E_KEY` and `DEV_ADMIN_TOKEN` | `PROD_POSTGRES_PASSWORD`, `PROD_SESSION_SECRET`, `PROD_HOT_WALLET_KEY`, `PROD_COLD_ADDRESS`, `PROD_SOLANA_RPC_URL` (all required) |

The `LLM_API_KEY` repo secret is shared. The Kamal secrets files map these onto the app's `UNFRET_*` settings
(`.kamal/secrets-common`, `.kamal/secrets.dev`, `.kamal/secrets.production`).

**Releasing to production:**

1. Bump the version and changelog (`make bump VERSION=x.y.z`) in a PR, merge it to `main`, and let it deploy to dev.
   Test it there.
2. In Forgejo: **Actions → Deploy production → Run workflow**, leaving `ref` at `main`. It deploys main's newest
   commit and tags it `v<version>`; if that tag already names another commit, it stops (bump the version first).

The production workflow refuses unless:
- the commit is on `main`;
- its `CI / check` and `CI / deploy-dev` statuses are green;
- dev reports that very commit (`/v1/health` → `commit`; the workflow waits up to 10 minutes for it) and passes the
  [end-to-end sweep](#end-to-end-sweep) against it;
- every production secret is set, and the RPC URL isn't a test network.

Nothing is tagged or deployed when the sweep fails.

It then deploys **the exact image dev ran** (`kamal deploy -d production --skip-push --version <sha>`), with no
rebuild. Deploys never run in parallel and are never cancelled half-way.

**Rolling back production:** run the workflow again with `ref` = the previous release tag (`v0.8.0` or later). The e2e
gate runs against dev, so it refuses a release that dev no longer runs (an older tag once main has moved on); for such
a rollback, deploy that release's image by hand with `kamal deploy -d production --skip-push --version <its commit>`
(like any production deploy, only with the owner's go).

### End-to-end sweep

`tests/e2e/test_dev_sweep.py` (`make e2e`, marker `e2e`; never part of `make test`) checks every path a customer or
an agent takes against a deployed dev, with real infrastructure: health and readiness, the x402 discovery document and
`llms.txt`; API keys (proof-of-work creation, an empty key's 402, an operator grant, recovery); analyses charged once,
served free from the cache, streamed, and every page the server can't read (`PAGE_BLOCKED`, `PAGE_UNAVAILABLE`,
`INVALID_URL`, `PAGE_TOO_THIN`, a validation error) free; sign-in with the payer's wallet; x402 with the MCP client
(quick in USDC, deep in wSOL, confirmed on devnet, a replayed payment answered once and debited once, an underpayment
and a malformed header refused, a bot-check page free); credit packs bought in USDC (memo) and in SOL by the Solana Pay
reference alone; card top-ups (a Polar checkout page, then made-up signed `order.paid`/`order.refunded` webhooks: dev
runs **live** Polar, so no real order is ever completed); the showcase and the operator endpoints' 401. Fixture pages
come from the forge at the commit dev runs, each URL carrying a per-run marker that must not appear in dev's app logs
or database afterwards (over ssh). It refuses to run against anything whose `/.well-known/x402` isn't devnet, and
checks that the payer holds at least 1.5 SOL and 5.01 devnet USDC before spending (it never uses a faucet: "fund
<address>"). The e2e key gets a working credit for the run and ends at exactly its starting balance (operator grant
or debit). A run takes about five minutes and spends about $10.21 of devnet USDC and SOL (two packs and two x402
checks), which goes to dev's hot wallet and is swept to dev's cold wallet.

| Setting | Repo secret (production gate) | |
| --- | --- | --- |
| `UNFRET_E2E_PAYER_KEY` | `DEV_E2E_PAYER_KEY` | the devnet payer: a Solana CLI keypair (JSON byte array) or base58 secret; devnet only |
| `UNFRET_E2E_KEY` | `DEV_E2E_KEY` | a long-lived API key on dev (`ufk_…`, made once at dev's `/checkout`), so a run creates only one key (5 per address per day) |
| `UNFRET_E2E_ADMIN_TOKEN` | `DEV_ADMIN_TOKEN` | dev's operator token |
| `UNFRET_E2E_FIAT_WEBHOOK_SECRET` | `DEV_FIAT_WEBHOOK_SECRET` | dev's Polar webhook secret, to sign the made-up order events |
| `UNFRET_E2E_POLAR_PRODUCTS` | `DEV_POLAR_PRODUCTS` | the packs' Polar products (`25=<id>,…`) |
| `UNFRET_E2E_EXPECT_FIAT` | (from `DEV_POLAR_ENVIRONMENT`) | readiness's `fiat`, default `polar:production`; `off` skips the card tests |
| `UNFRET_E2E_SSH` | `kamal@git.kroshtan.com` | where to read dev's logs and dump its database; without it the privacy check is skipped |
| `UNFRET_E2E_URL`, `UNFRET_E2E_COMMIT`, `UNFRET_E2E_FIXTURE_REF`, `UNFRET_E2E_RPC_URL`, `UNFRET_E2E_NO_SKIP` | | the target (default dev), the commit it must run, the commit to serve the fixtures from (default: the one dev runs), the devnet RPC, and whether a skip fails the run (the gate sets it) |

## Design notes

- **Untrusted input fetching.** Skills download URLs taken from user-submitted HTML. `net.fetch_public` only allows
  http(s), resolves the host and refuses loopback, private, link-local (cloud metadata) and reserved addresses,
  then connects to the address it checked (Host header and TLS SNI keep the original name), so a second DNS answer
  cannot rebind the request. Every redirect hop is re-validated and response size is capped.
- **Fail soft.** Each skill runs under its own timeout; a skill that fails or hangs reports "unavailable" to the
  judge instead of failing the analysis. The whole analysis has a deadline too (504 when exceeded).
- **Bounded input.** Request bodies over `UNFRET_MAX_PAGE_BYTES` are refused before they are parsed.
- **Sign-in grants no spending power.** Wallets only ever sign a readable message (to sign in) or a transfer the
  user sees (to buy). The server holds no user keys; a session token can spend Unfret credits, never SOL.
- **Exactly-once crediting.** Credits and the scanner cursor move in one transaction, by compare-and-swap; debits are
  a single conditional `UPDATE`, so concurrent analyses never overspend.

## Limitations and responsible use

- Scores come from a logistic regression over skill features (`src/unfret/scoring/model.json`, trained with
  `python -m evals.train` on a private labelled corpus). They are a calibrated signal, not a verdict, and can be wrong
  in both directions. Part of the training data is synthetic (marketplace pages rendered from public review data),
  so marketplace accuracy on real pages is likely lower than measured.
- Page content is passed to the LLM, so a hostile page can attempt prompt injection to influence its own score. It is
  delimited as untrusted data, and attempts to address the model are detected and count against the page.
- Registration data (RDAP) is often redacted; the first-certificate date is a second, registry-independent age.

## Data and models

- The image index is seeded with photo fingerprints from [Amazon Berkeley Objects](https://amazon-berkeley-objects.s3.amazonaws.com/index.html) © Amazon.com, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); only derived fingerprints, ASINs and brands are kept.
- Photo embeddings come from [DINOv2-small](https://huggingface.co/facebook/dinov2-small) (Apache-2.0), run locally through ONNX Runtime.

## License

[MIT](LICENSE)
