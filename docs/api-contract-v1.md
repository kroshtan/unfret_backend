# Unfret API contract v1: credits door (backend phase 4 ↔ browser extension phase 5)

Base URL: `https://unfret.net` (configurable in the extension for dev: `http://localhost:8080`).
All JSON. Errors use the existing shape `{"detail": str, "error_code": str, "metadata"?: any}`.

## Identity: Sign-In With Solana (SIWS), basic
Credits belong to a wallet public key. The user proves control by signing a plain-text message; nothing else is stored.

- `GET /v1/auth/nonce` →
  `{"domain": "unfret.net", "uri": "https://unfret.net", "statement": "Sign in to Unfret. This
  signature costs nothing and grants no spending rights.", "nonce": str, "issued_at": ISO-8601, "expiration_time":
  ISO-8601, "version": "1"}`
  The nonce is stateless (HMAC-signed by the server, carrying its own expiry, 5 minutes).
- The client signs either via the Wallet Standard `solana:signIn` feature (preferred; pass these fields as the
  sign-in input) or, as a fallback, via `solana:signMessage` over the canonical SIWS text built from the same fields
  (format: the Solana "Sign In With Solana" message format, i.e. EIP-4361 adapted with "wants you to sign in with your
  Solana account:").
- `POST /v1/auth/verify` `{"address": base58, "signed_message": base64 (the exact bytes signed), "signature": base64}`
  → `{"token": str, "address": base58, "expires_at": ISO-8601}`
  The server checks the ed25519 signature, the domain, the nonce's HMAC and expiry, and that the message's address
  equals `address`. The token is stateless (HMAC over address + expiry, 30 days), sent as `Authorization: Bearer <token>`.
  Signing in grants nothing: a wallet's balance row is created by its first purchase.

## Credits
The balance is held in **lamports** (`pubkey → balance_lamports`); the UI shows checks remaining.
- `GET /v1/pricing` → `{"analysis_lamports": int, "x402": {"quick_lamports": int, "deep_lamports": int},
  "bonuses": [{"min_lamports": int, "bonus_pct": int}], "suggested_packs": [{"checks": int, "lamports": int}],
  "hot_wallet": base58, "network": "solana-mainnet"|"solana-devnet"}`
- `GET /v1/credits` (Bearer) → `{"address": base58, "balance_lamports": int, "checks_remaining": int}`
- `POST /v1/credits/purchase-tx` `{"address": base58, "lamports": int}` → `{"transaction": base64 (unsigned
  VersionedTransaction: a System transfer address → hot wallet, payer = address), "lamports": int, "hot_wallet": base58,
  "solana_pay_url": "solana:<hot>?amount=<SOL>&label=Unfret&message=Unfret%20credits"}`
  No auth needed (anyone can fund any wallet). The wallet signs and sends it itself (`solana:signAndSendTransaction`).
- Crediting is done by the server's scanner, from finalized incoming SOL transfers to the hot wallet; credits arrive
  after finality (~15–30 s). Clients poll `GET /v1/credits`.

## Analysis
- `POST /v1/analyze` (Bearer) `{"url": str, "page_facts"?: PageFacts, "page_content"?: str, "checkout_content"?: str,
  "flags"?: [str]}` → `{"score": int 0..100 | -1, "explanation": str, "assessment"?: {"risk_score", "confidence",
  "flags": [{"type", "evidence", "weight"}], "model_version"}, "balance_lamports": int}`
  At least one of `page_facts`/`page_content` is required. The extension sends `page_facts` plus cleaned main text in
  `page_content` (never raw logged-in HTML; strip account/header/footer/nav). In place of the HTML, `page_facts`
  carries what the source checks (`html_source_signals`) read from it: `script_srcs`, `meta`, `markup_names` (class
  names, ids and data-attribute names, never values) and `external_refs` (other sites: a link's host and path, any
  other element's host, never a query), so a quick check of the same page gives the same answer whether the server
  fetched it or the extension read it. What stays different by design: the text of inline scripts and attribute
  values are never sent (an app named only there, or an analytics ID for `shop_links`, is found only on a fetched
  page), and the extension reads the rendered page (scripts have run), the server the HTML as served.
  Debited `analysis_lamports` up front; refunded when the page isn't analysable (score -1) or on error.
  Insufficient balance → 402 `{"error_code": "INSUFFICIENT_CREDITS", "metadata": {"balance_lamports", "analysis_lamports"}}`.
  401 for missing/invalid/expired token.
- `POST /v1/analyze/stream` (Bearer), same body, `text/event-stream`: one `event: step` per skill finishing
  (`data: {"skill": str, "findings": [...]}`), then `event: result` with the body above, or `event: error`.
- `PageFacts` is the backend's pydantic model `unfret.facts.schema.PageFacts`; its JSON schema is served at
  `GET /v1/schema/page-facts` so the extension can validate against it.

## Pages served by the backend
- `GET /connect` — sign-in page. After a successful verify it does
  `window.postMessage({"type": "veridex-session", "token", "address", "expires_at"}, location.origin)`.
  (The message types `veridex-session` and `veridex-session-received` keep their names from before the rename to
  Unfret: released extensions use exactly these.) The extension's content script, injected only on `https://unfret.net/connect*`, relays it to the
  extension (works in Chrome and Firefox; no `externally_connectable`). The page then shows "You can close this tab".
- `GET /checkout?checks=<n>` — connect a wallet, buy credits (purchase-tx → signAndSend), Solana Pay QR for phone
  wallets, polls `/v1/credits`, shows when credits land.
- Both pages: static HTML + vanilla JS, no framework, no third-party requests (Wallet Standard discovery implemented
  directly via the `wallet-standard:app-ready` / `wallet-standard:register-wallet` events).

## CORS
Allow origins `chrome-extension://*` and `moz-extension://*` plus the site itself, headers `Authorization`,
`Content-Type`; methods GET/POST.

## Changes (backend phase 4, all backwards-compatible)
- `GET /v1/pricing`: `hot_wallet` is `null` while buying credits is off (sign-in off, no hot wallet, or the scanner
  hasn't started). Clients should then hide "buy".
- Added `GET /v1/credits/solana-pay.svg?lamports=<n>`: the Solana Pay request as an SVG QR code (503 while purchases
  are off). `/checkout` uses it for phone wallets.
- `/v1/analyze`, `/v1/analyze/stream`, `/v1/credits`, `/v1/auth/*`: 503 `SERVICE_UNAVAILABLE` while sign-in is not
  configured on the server.
- `POST /v1/credits/purchase-tx`: 400 `BAD_REQUEST` outside `[1_000_000, 10_000_000_000]` lamports (metadata gives the
  bounds); 503 while purchases are off.
- `POST /v1/analyze` response omits `assessment` when the scorer isn't used, and `balance_lamports` is always present.
  The stream's `result` event carries the same body (keys with null values omitted).
- `POST /v1/analyze` accepts `page_facts` alone (no `page_content`); the LLM steps then read a text built from the facts.
  Sending cleaned main text in `page_content` as well still gives better results.
- `GET /v1/health/ready` has `credits`: `off` | `sign_in` | `ok` (purchases on).
- x402 `POST /v1/check`: when the analysis fails after payment, the error body adds `refunded_lamports` and a `refund`
  note; the price is credited to the paying wallet.
- Limitation: credits bought from a smart wallet (a program-owned vault, e.g. Squads) are credited to the vault
  address, which can't sign a SIWS message. Buy from a normal wallet to use credits; smart wallets fit x402 instead.
- **Smart wallets / beneficiary (supersedes the limitation above):** `POST /v1/credits/purchase-tx` takes an optional
  `beneficiary` (base58, defaults to `address`). The transaction carries a Memo instruction `unfret:credit:<beneficiary>`,
  and `solana_pay_url` a `memo=` parameter with the same text. The scanner credits the memo's beneficiary when the
  transaction has exactly one well-formed `unfret:credit:` memo; otherwise (none, several, malformed) the transfer's
  source. CPI (inner-instruction) System transfers from vaults count. `GET /v1/credits/solana-pay.svg` also takes
  `beneficiary`. So a Squads vault can pay with `address = vault`, `beneficiary = the member key that signs in`.
- **Stream events:** `/v1/analyze/stream` emits `event: step` with `data: {"skill": <node>, "findings": [...],
  "check": <bool>}` per finished node (the classifier's step adds `"page_type"`; `check`: see "the answer's small
  signs" below), then `event: result` (the `/v1/analyze` body) or
  `event: error`. The earlier `page_type` / `finding` events are gone.
- **Connect handshake:** after posting `veridex-session`, `/connect` listens for
  `{"type": "veridex-session-received"}` (same origin, `event.source === window`) and then shows "Extension connected —
  you can close this tab"; without it after ~3 s it shows a hint to install/enable the extension.
- First start of the scanner waits for the newest confirmed hot-wallet transaction to finalize and starts after it, so
  funding sent right before the first start isn't credited. Any later SOL sent to the hot wallet (a top-up too) counts
  as a purchase by its sender (or memo beneficiary).

## Changes (phone sign-in by pairing, backwards-compatible)
Sign in on the computer's extension with a wallet on a phone, through the phone wallet's in-app browser. No relay,
no on-chain transaction; ordinary SIWS. All pairing state is in the server's memory (5 minutes), never persisted.
- `POST /v1/auth/pair` (no auth) → 201 `{"pair_id": str (128 random bits, URL-safe), "confirm_code": "K7-4Q",
  "connect_url": "https://unfret.net/connect?pair=<pair_id>", "expires_at": ISO-8601}`.
  429 when this visitor started too many (default 10 per 10 minutes) or the server holds too many. 503 while
  sign-in is off.
- The extension shows `connect_url` as a QR code and `confirm_code` large ("check that your phone shows the same
  code"), then polls `GET /v1/auth/pair/<pair_id>` every ~1.5 s:
  - 202 `{"status": "pending"}` — nobody signed in yet;
  - 200 `{"token", "address", "expires_at"}` — exactly once; the pairing is then deleted (a later poll → 404);
  - 410 `error_code: PAIRING_EXPIRED` — expired before a sign-in (start again);
  - 404 `error_code: PAIRING_UNKNOWN` — unknown or already collected.
- The phone opens `/connect?pair=<pair_id>`. The page fetches `GET /v1/auth/pair/<pair_id>/info` →
  `{"confirm_code", "expires_at"}` (410/409/404 as above; 409 `PAIRING_USED` once signed in) and shows "Sign in to the
  device showing K7-4Q". `GET /v1/auth/nonce?pair=<pair_id>` returns the statement
  "Sign in to Unfret. This signature costs nothing and grants no spending rights. For the device showing K7-4Q."
- `POST /v1/auth/verify` takes an optional `pair_id`. With it, the signed message must carry exactly that pairing's
  statement (else 401 `PAIRING_MISMATCH`-style refusal "signed for another device"); the session is attached to the
  pairing once (a second verify → 409 `PAIRING_USED`). The response still contains the session; the phone page
  discards it and shows "Signed in — return to your computer".
- Errors keep the usual shape `{"detail", "error_code"}`.

## Changes (SOL and USDC on both tracks, backwards-compatible)
- **Prices are canonical in SOL** (unchanged: analysis 1_500_000, x402 quick 100_000, deep 1_500_000 lamports). USDC
  amounts are that price converted at payment time with Pyth's SOL/USD price (read on chain; ≤60 s old, confidence
  ≤1%); USDC is refused, not mispriced, while the oracle is stale or uncertain. A `fixed` price mode exists but is off.
- `GET /v1/pricing` adds `purchase: {min_lamports, max_lamports}` (minimum = one analysis), `price_mode`, and
  `usdc: {mint, decimals: 6, analysis_units, x402: {quick_units, deep_units}, purchase: {min_units, max_units},
  sol_usd, quoted_at (unix), source: "pyth"|"fixed"}` — or `usdc: null` plus `usdc_unavailable: "<reason>"`.
- `POST /v1/credits/purchase-tx` takes `currency: "SOL"|"USDC"` (default SOL) and `amount` (base units of the
  currency); `lamports` still works for SOL. The response adds `currency`, `amount`, `mint` (USDC only). A USDC
  purchase is a `TransferChecked` into the hot wallet's USDC account (created in the same transaction if missing, rent
  paid by the buyer) plus the beneficiary memo. Limits apply by converted SOL value.
- Crediting: a USDC purchase is credited at its SOL value at crediting time, then gets the SOL bonus tier that value
  reaches (≥0.05 SOL +10%, ≥0.25 SOL +20%). Balances stay in lamports; `checks_remaining` is unchanged.
- `GET /v1/credits/solana-pay.svg` takes `currency` and `amount` (`lamports` still works for SOL); USDC links carry
  `spl-token=<mint>`.
- x402: the 402 `accepts` lists wSOL first, then USDC (amount = the converted quote) only while USDC can be priced
  AND the hot wallet's USDC account exists. A USDC payment may differ from the quote by ≤2% (prices move between 402
  and payment); wSOL must match exactly. Refunds of failed paid checks are the tier's canonical SOL price as credits.
- `/v1/health/ready` adds `hot_usdc_units` (USDC not yet swept to cold); x402 alerts may include
  `usdc_above_threshold`. `swap_failing` is no longer emitted: USDC is swept to cold as USDC, never swapped.
- **Extension:** nothing has to change (it opens `/checkout`, which now has a SOL/USDC switch, and shows checks). It
  may show the USDC quote from `/v1/pricing.usdc` if it wants to.

## Changes (results for shoppers, backwards-compatible)

- `POST /v1/analyze` and `/v1/analyze/stream` read **`Accept-Language`** (supported: `nl`, `en`; anything else → `en`)
  and return a new **`verdict`** object, in that language:
  `{"band": "reliable"|"concerns"|"careful"|"junk_or_scam", "label": str, "headline": str,
    "reasons": [{"type", "title", "detail", "severity": "high"|"medium"|"low"}] (≤3),
    "positives": [{"type", "title", "detail"}] (≤3), "language": "nl"|"en"}`.
  Bands from the score: ≥80 reliable, 50–79 concerns, 25–49 careful, <25 junk_or_scam. Reasons never contain
  feature names or raw values. For `reliable`, show positives first and at most one reason. `explanation` now equals
  `verdict.headline` (localised); old clients keep working.
- Every `assessment.flags[]` entry gains `label` (human, localised), `kind` (`signal` | `context`: context flags
  describe the page, e.g. "Marketplace listing", and must be shown neutrally) and `severity`. `evidence` is never a
  raw `name = value` any more (may be empty: show `label`).
- New `charged` (bool) on the analyze response and the stream's `result`: false when no check was used (score -1, or
  a result served from the 1-hour cache, which is now free; since "a cached result costs a check" below, only to the
  account that paid for it). Show "No check was used" for those.
- A cache hit on `/v1/analyze/stream` sends only the `result` event, immediately.
- x402 `POST /v1/check` verdict gains `summary` (one sentence), `page_type`, `band`, `assessed` (false for pages that
  couldn't be assessed, with `risk_score: null` instead of 0.0) and `flags[].label` / `flags[].severity`. Flags are
  the shopper reasons (plain evidence) rather than raw scorer features.

## Changes (prices in US dollars, backwards-compatible field names kept)

- **Prices are canonical in US dollars**, in micro-dollars (1 USD = 1_000_000, also USDC's base unit): analysis
  150_000 ($0.15), x402 quick 10_000 ($0.01), deep 150_000 ($0.15). USDC pays exactly; SOL is converted at quote time
  with Pyth SOL/USD (charges round up, credits round down) and is refused while the oracle is stale (USDC still works).
- **Balances are micro-dollars.** `GET /v1/credits` → `{"address", "balance_usd_micros": int, "checks_remaining": int,
  "balance_lamports": int}`; `balance_lamports` is a deprecated alias holding the same micro-dollar value. Migration 5
  divided stored lamport balances by 10, so every wallet keeps its number of checks. Clients should show
  `checks_remaining` (unchanged). `POST /v1/analyze`'s `balance_lamports` and `INSUFFICIENT_CREDITS` metadata
  (`balance_lamports`, `analysis_lamports`) now also carry micro-dollars under the old names (renamed in a later
  release).
- `GET /v1/pricing` → `{"currency": "USD", "analysis_usd_micros", "analysis_usd": "$0.15", "x402": {"quick_usd_micros",
  "deep_usd_micros"}, "bonuses": [{"min_usd_micros", "bonus_pct"}], "suggested_packs": [{"checks", "usd_micros"}],
  "purchase": {"min_usd_micros", "max_usd_micros"}, "hot_wallet", "network", "usdc": {"mint", "decimals",
  "analysis_units", "x402": {"quick_units", "deep_units"}, ...}, "sol": {"usd_per_sol", "quoted_at",
  "source": "pyth", "analysis_lamports", "x402": {"quick_lamports", "deep_lamports"}, "purchase": {"min_lamports",
  "max_lamports"}} | null, "sol_unavailable"?: str, "analysis_lamports": int | null}`. `usdc` is always present now;
  `sol` is null while the oracle is stale. Removed: `price_mode`, the old `usdc.sol_usd`/`usdc.source`, lamport bonus
  and pack fields.
- Bonuses: ≥$5 +10%, ≥$25 +20%. Packs: 10/50/200 checks. Purchase limits $0.15 … $1,000 (SOL limits converted).
- x402 `402`: wSOL (converted, ≤2% drift accepted) first, then USDC (exact) once the hot wallet's USDC account exists.
  While SOL can't be priced only USDC is offered; with neither, `503 PRICE_UNAVAILABLE` with `Retry-After: 30`.
- x402 refund after a failed paid analysis: `refunded_usd_micros` (the tier's dollar price, as credits);
  `refunded_lamports` is kept as a deprecated alias of the same value.
- `/.well-known/x402` adds `"pricing": "canonical in USD"` and per-tier `price_usd` / `price_usd_micros`; `/llms.txt`
  quotes dollar prices.
- MCP: spend caps are in dollars (`UNFRET_MCP_MAX_PRICE_USD`, `UNFRET_MCP_SESSION_CAP_USD`); a wSOL charge is valued
  with the agent's own Pyth reading. `check_store`'s `paid` adds `usd_micros`.

## Changes (wave A: hardening, errors and constraints)

- `GET /v1/auth/nonce` no longer returns `chain_id`. The sign-in message hasn't carried a Chain ID line since the
  wallet-compatibility fix; the field was unused.
- Every error is JSON with `detail` and `error_code`. Request validation errors are `422 VALIDATION_ERROR` with
  `errors: [{"loc", "msg", "type"}]`; unknown routes are `404 NOT_FOUND`. The `/v1/check` codes are listed with
  "charged?" and "what to do" in `/llms.txt` and the README (`PAYMENT_REQUIRED`, `SETTLEMENT_PENDING`,
  `SETTLEMENTS_PAUSED`, `PAGE_BLOCKED`, `RATE_LIMITED`, …).
- Each x402 option's `extra.constraints` states what the payment transaction must look like (memo, compute caps,
  priority-fee cap, no lookup tables, allowed programs).
- `POST /v1/check` retried with the same `PAYMENT-SIGNATURE` returns the same answer for 15 minutes and is charged once.
  A page that can't be assessed (score -1) is refunded as credits (`refunded_usd_micros`).
- `GET /v1/health/ready` gives `x402_alert_count` to everyone. The balances and alert names require the operator token.
- `POST /v1/admin/x402/clear-alerts` (operator token) clears a suspected compromise after investigation.
- The API's OpenAPI document is pinned by `tests/snapshots/openapi.json`, so a contract change shows up in review.

## Changes (wave B: analysis fixes)

- **Stream steps carry progress, not evidence:** `event: step` findings are `{"skill", "type"}` only. The evidence
  arrives with the charged `result` (`verdict.reasons[].detail`, `assessment.flags[].evidence`). A client that hangs
  up before the result is refunded; a finished result is cached first, so repeating the request (with the same
  account) gets it free.
- **Cache:** results are reused only for identical input (same listing, page content, facts, checkout page, tier,
  language and flags). Tracking parameters (`utm_*`, `gclid`, `fbclid`, `ref`, …) don't make a new listing; product
  IDs in the query (`?id=`, `?product_id=`, `?variant=`) do.
- **`assessment.degraded`:** `true` when the content read failed. The score then rests on the other signals, the
  confidence is at most 0.3, and `/v1/check` never answers `allow` on it.
- The headline is at most 25 words. Without an LLM sentence (and always for `/v1/check`, whose callers read the
  flags) it names the verdict and its two strongest reasons ("Some warning signs: fake countdown timer and pre-ticked paid
  add-on.").
- **Fake discounts:** new reasons `implausible_discount`, `reference_anchor` (the "was" price is an RRP/"usually"
  price), `permanent_sale` and, once a product has a month of anonymous price observations, `was_never_charged`.
  New features under `pricing.*`; none are in the scorer yet. `GET /privacy` is the privacy statement.
- **`balance_usd_micros`** in `/v1/analyze` responses (and `balance_usd_micros`/`analysis_usd_micros` in the 402
  metadata) is the balance in micro-dollars. `balance_lamports` and `analysis_lamports` carry the same values and are
  deprecated: extension versions up to 0.5.0 read them.
- Extension 0.5.0 is published at `/extension` (CRX, `updates.xml` on unfret.net, and the Chrome Web Store zip).

## Changes (accounts v2: API keys, fiat interface, reports; backwards-compatible)

**API keys: an account without a wallet.** A key is `ufk_<id>_<secret>`; the `<id>` (10 lowercase base32 characters)
is public and names the account, `key:<id>`, in the same ledger as wallets. Wherever a session token works
(`/v1/analyze`, `/v1/analyze/stream`, `/v1/credits`, `/v1/reports`), `Authorization: Bearer ufk_…` works too. Keys
start empty.

- `GET /v1/keys/challenge` → `{"challenge": str, "bits": int, "expires_at": float}`: a proof of work, accepted once
  within 10 minutes. Find a `solution` (≤ 32 characters, e.g. a decimal counter) with
  `SHA-256(challenge + ":" + solution)` starting with `bits` zero bits (18 by default: a few seconds in a browser).
- `POST /v1/keys` `{"challenge", "solution"}` → 201 `{"key_id", "key", "recovery_code", "account": "key:<id>",
  "balance_usd_micros": 0}`. The key and the recovery code (`ufr_<id>_<secret>`) are shown this once and stored only
  as SHA-256 hashes. 400 `BAD_CHALLENGE`; 429 after 5 new keys per visitor network (IPv6 /64) per day.
- `POST /v1/keys/recover` `{"recovery_code"}` → the same shape: a new key and a new recovery code with the same id
  and balance. The old key and code stop working at once. 401 `INVALID_RECOVERY_CODE`; 429 after 10 tries per network
  per day.
- 401 `INVALID_API_KEY` for a malformed, unknown or rotated key.
- **Top up with crypto:** `POST /v1/credits/purchase-tx` and `GET /v1/credits/solana-pay.svg` take `key_id` (the id or
  the whole key; only the id is used) instead of `beneficiary`. The memo is `unfret:credit:key:<id>`. A memo naming
  an unknown key credits the paying wallet instead. 404 `UNKNOWN_KEY`.
- **Fiat (interface only, shaped for Polar):** `POST /v1/credits/fiat-checkout` `{"key_id", "amount_usd_micros"}`
  (USD, whole cents, at least $10) → `{"checkout_url", "provider"}`. Our account id goes into the checkout's
  metadata and the server also keeps checkout id → account. `POST /v1/webhooks/fiat` takes Standard Webhooks
  (`webhook-id`, `webhook-timestamp` within 5 minutes, `webhook-signature` HMAC with the `whsec_` secret): a paid order
  is credited once (with the same bonus tiers as crypto); a refund debits credits pro rata to the refunded share
  (cumulative, partial refunds add up); a dispute debits all of the order's credits. Debits may make a balance
  negative, which blocks paid use (402) until a top-up. Per order the server keeps provider, order id, account, amount
  in cents, credits granted, refunded cents, disputed and debited credits, for 180 days; never the buyer's email, name
  or address. Off (503) until a Merchant of Record is wired in. `GET /v1/pricing` now says `"api_keys": bool`,
  `"fiat": {"available": bool, "min_usd_micros"}` and `"reports": {"per_month", "reward_checks"}`.
- `GET /v1/credits` returns `address: "key:<id>"` for a key; `checks_remaining` is never below 0.

**Reports: "this is wrong".** Every `/v1/analyze` result with a score (and the stream's `result`) carries a
`report_token`: an HMAC over the account, the score, a hash of exactly what was analysed and of the explanation,
valid 7 days. Nothing is stored when a check runs.

- `POST /v1/reports` (Bearer) `{"report_token", "reason": "real_brand"|"scam"|"junk"|"other", "consent": true,
  "request": <the analysed request, exactly as sent>, "explanation": <as returned>, "verdict"?: <as returned>}` →
  201 `{"status": "queued"|"already_reported"|"already_known", "reports_used", "reports_per_month"}`.
  `consent` is the user agreeing that this one check's page facts, page text and result are shared for review.
  Personal details are scrubbed on the server too (greetings, delivery lines, emails, phone numbers, IBANs, card
  numbers; query strings off every URL; raw HTML reduced to facts and visible text; the checkout page dropped).
- Before the queue: a page (domain + ASIN / bol.com id / GTIN, else the clean URL) already queued is
  `already_reported` and counts as confirmation; a page labelled in the last 90 days is `already_known`.
- Errors: 400 `BAD_REPORT_TOKEN` (someone else's check, changed data, or older than 7 days), 409
  `ALREADY_REPORTED_BY_YOU`, 429 `REPORT_LIMIT` (5 per account per month, `metadata: {"used", "limit"}`; 15 since
  the reports change below), 503 `QUEUE_FULL` (about 200 open reports; see below for the queue by value).
- Reward: when the human label shows the score was at least 30 points outside the label's zone (legit ≥ 70, junk and
  scam ≤ 40), the reporter gets 3 checks (5, and 1 for 10–29 points, since the reports change below). The report is then deleted (its only link to the account); the scrubbed page
  and its label stay as training data. Reports nobody handled are deleted after 30 days.
- Operator: `GET /v1/admin/reports` (the queue, without accounts) and `POST /v1/admin/reports/{id}/label`
  `{"label": "legit"|"junk"|"scam"|"reject"}`, used by `make reports`.
- Extension 0.6.0 replaces 0.5.0 at `/extension`: an API key in the
  settings as an alternative to a wallet sign-in, and "This is wrong" under a result. Same key and ID, same
  permissions.

## Changes (pricing v2: one price per check, packs, VAT on crypto; breaking for credit purchases)

- **One price per check**, `UNFRET_PRICE_PER_CHECK_USD` ($0.20): an analysis costs one check in credits and the x402
  deep tier costs one check (`deep_usd_micros` 200_000). The quick tier stays $0.01. Changing the setting moves every
  price below.
- **Packs only:** 25, 50 and 100 checks. Bonuses and arbitrary purchase amounts are gone (`bonuses` and `purchase` are
  removed from `/v1/pricing`). Per pack, `price_usd` is the price (by card, before the provider's sales tax/VAT:
  $6.00 / $11.50 / $22.00) and `crypto_usd` the lower price with SOL or USDC ($5.00 / $10.00 / $20.00, before VAT).
  Show the card price as the price and crypto as the discount, never as a card surcharge (EU rules). Either way the
  account is credited `credited_usd_micros` (the pack's value).
- `GET /v1/pricing` adds `price_per_check_usd_micros`, `price_per_check_usd`, `packs: [{"checks", "price_usd_micros",
  "price_usd", "crypto_usd_micros", "crypto_usd", "credited_usd_micros", "label": {"en", "nl"}}]` (`label` is the
  ready-made wording, e.g. "25 checks — $6.00 · pay with crypto: $5.00 + VAT where applicable"), and `vat: {"mode",
  "home_country": "NL", "eu_rates_pct": {ISO code: percent}, "applies_to", "country_hint": ISO code | null}`
  (`country_hint` is Cloudflare's guess for this visitor, so the response is `Cache-Control: private, no-store`).
  `suggested_packs` stays as a deprecated alias (`{"checks", "usd_micros"}` = the crypto price).
  `fiat.min_usd_micros` is the smallest pack's card price.
- **`POST /v1/credits/purchase-tx`** now takes `{"address", "currency", "checks", "country", "beneficiary"? |
  "key_id"?}` (`amount`/`lamports` are gone: 422). `country` is ISO 3166 alpha-2 (`EL` is read as `GR`; `XX`/`T1`
  refused). The response adds `checks` and `price: {"country", "vat_rate_pct", "net_usd_micros", "vat_usd_micros",
  "total_usd_micros", "total_usd"}`; `amount` is the total in the currency's base units, VAT included. Under
  `vat_mode=nl_under_threshold` (default) every EU country pays 21% and others 0%; `per_country` uses each member
  state's standard rate. The memo is `unfret:quote:<20-char id>`; the stored quote names who to credit and the VAT.
- **`GET /v1/credits/solana-pay.svg`** takes `checks`, `country`, `currency` and `beneficiary`/`key_id` (not
  `amount`); headers `X-Unfret-Total-Usd` and `X-Unfret-Vat-Rate` carry the price.
- **Crediting:** what arrives, minus the VAT at the quote's rate, is credited. A transfer without a known quote (sent
  by hand) counts as a sale to an unknown country at the Dutch rate (credited `received / 1.21`); the older
  `unfret:credit:<account>` memo still names the beneficiary. Each credit leaves a sales record (day, country,
  country mismatch with Cloudflare's guess, net, rate, VAT, asset, amount, transaction signature), kept ten years;
  no IP address, no wallet column. `GET /v1/admin/sales/{YYYY-Qn}` (admin) exports a quarter as CSV.
- **`POST /v1/credits/fiat-checkout`** takes `{"key_id", "checks"}` (not `amount_usd_micros`) and charges the pack's
  card price. A paid order is credited the value of the pack whose card price it matches; another amount buys the
  whole checks it covers after the provider's cut (webhook outcome `too_small` if none).

## Changes (US market, backwards-compatible)

- `PageFacts.platform` may be `"walmart"` (snapshot updated). Walmart product pages are classified as marketplace
  listings without an LLM call, like Amazon and bol.com.
- New finding types: `us_address`, `mailbox_address`, `business_entity` (skill `us_identity`, US storefronts) and
  `long_delivery`, `returns_abroad`, `return_address_withheld`, `restocking_fee`, `origin_doubt` (skill
  `shop_policies`). New shopper reason keys `no_return_policy`, `returns_abroad`, `return_address_withheld`,
  `restocking_fee`, `origin_doubt`, `long_delivery`, `mailbox_address` and positive `us_business`; clients that map
  reason keys should fall back to the `title`/`detail` text they already get.
- A skill whose rules don't apply to a page reports the feature `<skill>.not_applicable` (e.g.
  `us_identity.not_applicable` on an EU shop); the scorer treats its features as neutral.

## Changes (fingerprints and own image index)

- The Google Cloud Vision photo lookup is gone. `reverse_image` now looks the main photo up in Unfret's own index
  (features `reverse_image.searched`, `reverse_image.other_shops`, `reverse_image.catalogue_matches`; findings
  `photo_on_other_shops` and `catalogue_photo`). It runs in the quick tier too: no outside service is asked.
- New skill `shop_links` (quick and deep): `shop_links.linked_shops`, `.linked_young_shops`, `.shared_payment`,
  `.shared_contact`; findings `shared_identifier` and `linked_young_shops`.
- New reasons `photo_reused` (only for an unknown brand with other resale signs) and `linked_new_shops`.
- Verdict reasons may carry `thumbnails`: up to three URLs of matched photos, served by
  `GET /v1/images/{print_id}/thumbnail.jpg` (a small JPEG fetched by the server, so the shopper's browser never
  contacts the other shop). The field is optional and empty for every other reason.
- Operator routes (token required): `DELETE /v1/admin/shops/{domain}` and `POST /v1/admin/contacts/forget` remove a
  shop or a contact detail from the fingerprint store.
- `POST /v1/check` answers carry `source`: `"analysis"`, or `"fingerprint"` when a full analysis of the same listing
  from a page Unfret fetched itself in the last day is served again (the shop's fingerprint unchanged). It is paid as
  usual, `cached` is `true`, and `action`/`policy_applied` follow the caller's policy. Requests with `page_content`
  are always analysed.

## Changes (link checker, backwards-compatible)

- `POST /v1/analyze` and `/v1/analyze/stream` accept a body with only `url` (no `page_content`/`page_facts`): the
  server fetches the page itself (`unfret.page_fetch`) before anything is charged. An account whose balance doesn't
  cover a check gets its 402 without a fetch. A page that can't be checked that way is a free `422` (plain JSON, no
  stream) with `error_code` `PAGE_UNAVAILABLE` (couldn't be fetched), `PAGE_BLOCKED` (the shop answered 401/403/405/
  406/429/503/999, or another error status with a bot wall's page such as Kasada's or Akamai's; with a
  Cloudflare/DataDome/PerimeterX/Amazon/Akamai/Imperva bot check; or not at all within 6 seconds of connecting:
  the whole fetch gives up after 12), `PAGE_TOO_THIN` (under 400 characters
  of visible text with no price and no product JSON-LD: a shell its scripts fill in) or `INVALID_URL` (not a public
  http(s) address). Fetches are limited per account (`UNFRET_ANALYZE_FETCH_PER_MINUTE`, 10; over it `429`).
  The result cache and the `report_token` are keyed on the URL-only request; a report of such a check sends back
  `{"url": …}` and the server fetches the page again for the review queue. A `url` that redirects (a short link) is
  checked as the page it leads to (each hop a public address, at most five): the result cache, the `share_token`
  (and so the shared page's `product_url`) and `POST /v1/check`'s verdict store use that page's address without
  fragment and tracking parameters; the `report_token` is still for the request as sent.
- Requests that send the page (the extension) are unchanged.

## Changes (the "Spotted recently" showcase, backwards-compatible; retired, see the end)

- `GET /v1/showcase?limit=<1-24, default 6>` (no auth) → `{"items": [{"id", "title", "host", "product_url",
  "checked_at", "checked_on", "published_at", "band", "label", "symbol", "headline", "page_url",
  "screenshot_url"}]}`: checks Unfret ran itself on public product pages, published after review, newest first.
  `Cache-Control: public, max-age=300`. Never a user's check.
- `GET /v1/showcase/{id}` → the same fields plus `result` (`verdict: {band, label, symbol, headline, reasons,
  positives}` and, when there are flags, `assessment: {flags: [{label, evidence, kind}], confidence, model_version}`);
  404 `NOT_FOUND` for drafts and removed cases.
- `band` is `reliable` | `concerns` | `careful` | `warning` (the analysis' worst band), and `label` describes the
  listing ("Unfret would warn you about this listing"). The showcase has no score, and no text containing "scam" or
  "fraud": such a headline is replaced by the band's catalogue headline, such a quote is left out.
- Pages: `/showcase/{id}` (the case in the link checker's presentation) and `/showcase/{id}/screenshot`.

## Changes (no free trial)

- There is no free trial any more: a wallet's first sign-in grants nothing, and every new account (wallet or API key)
  starts at zero checks and buys a pack. Balances already granted stay.
- `GET /v1/pricing` no longer has `trial_checks`. Clients that read it should treat it as absent (0).
- The `UNFRET_TRIAL_*` settings are gone.

## Changes (purchase confirmation and Solana Pay references, backwards-compatible)

- `GET /v1/credits/quotes/{quote_id}` (no auth: the random quote id is the capability) → `{"paid": bool, "checks":
  int}`, `Cache-Control: no-store`. `paid` turns true when the scanner credits a payment for that quote (in the same
  database transaction as the credit). 404 `UNKNOWN_QUOTE` for an unknown, malformed or purged id (quotes are
  deleted a day after they expire). Nothing else about the quote (who pays, who gets the checks, the country) is
  returned. Clients poll it every few seconds after sending the payment.
- `POST /v1/credits/purchase-tx` → also `quote_id`. `GET /v1/credits/solana-pay.svg` → also the
  `X-Unfret-Quote: <quote_id>` header (not CORS-exposed; the checkout fetches it from the same origin).
- Each quote has a Solana Pay **reference** (a random public key). The purchase transaction carries it as an extra
  read-only, non-signer account on the transfer instruction (System transfer or `TransferChecked`; both programs
  ignore it), and the Solana Pay link has `&reference=<pubkey>`. The memo `unfret:quote:<id>` stays. The scanner
  finds the quote by the reference among the transaction's accounts first (some wallets, e.g. Phantom, drop memo
  instructions), then by the quote memo, then the older `unfret:credit:` memo, then credits the sender. The
  reference is never credited.
- The Solana Pay link's `message` is now `Unfret checks`.

## Changes (x402 client edges, backwards-compatible except discovery's `network`)

- `/.well-known/x402` `network` is now the CAIP-2 identifier used in `accepts[].network`
  (`solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1` on devnet, `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` on mainnet); the
  cluster name it used to carry (`solana-devnet`) moved to `network_name`. `/llms.txt` names both. Its `assets` gain
  `decimals` and `quote_tolerance_bps`, and a `feeCap` sentence explains the fee cap's units.
- Each 402 `accepts[].extra` gains `decimals` (9 for wSOL, 6 for USDC), `quoteToleranceBps` (200 for wSOL, 0 for
  USDC: how far a paid amount may be from the quote; re-fetch the 402 right before paying) and `feeCapNote`.
  `extra.constraints.maxPriorityFeeLamports` is unchanged: it is in lamports on every option, USDC included (the fee
  is paid in SOL), the tier's dollar price times `UNFRET_X402_MAX_PRIORITY_FEE_BPS` converted at the SOL/USD quote.
- A 402 for a refused payment may carry `reason` next to `error`. With `error: "simulation_failed"` it says why the
  payment's simulation failed, derived from the simulation's error and logs: `insufficient_funds`,
  `source_token_account_missing`, `mint_mismatch`, `owner_mismatch`, `account_frozen`, `account_not_found`,
  `blockhash_not_found`, `insufficient_funds_for_rent`, another error class in snake case (`invalid_account_data`),
  or `custom_program_error_<code>` for another program's own error. Treat unknown values as opaque.
## Changes (x402 tiers say what they buy; quick is rules-only)

- `quick` ($0.01) now runs only rule-based checks of the page itself: `listing`, `pricing` (without the price
  history), `html_source_signals`, `dark_patterns`, `injection`, `eu_registry` (without VIES), `us_identity`,
  `shop_policies`, `shop_links`. No LLM call (also none to classify the page: a page without a product and a price
  is answered `assessed: false` and refunded), no reviews, no image index, no domain age. It is scored by its own
  model (`model_version` `v1-quick-…`). `deep` ($0.20) is unchanged: every skill, the same analysis as
  `/v1/analyze`.
- Each option in a 402's `accepts` carries `extra.tier` and `extra.includes` (one sentence: what the tier checks);
  `/.well-known/x402` has `tiers.<tier>.includes`, and `/llms.txt` a "Tiers" section. Payments are matched as before
  (scheme, network, asset, payTo, amount), so clients that ignore the new fields are unaffected.
- Under the `balanced` and `lenient` policies, `action` is `allow` whenever `band` is `reliable` or `concerns`;
  `/.well-known/x402` lists this per policy as `never_warns_on_bands`.

## Changes (invites, backwards-compatible; migration 15)

- Every credited pack comes with **invites** to give away: one per 25 checks (25-pack → 1, 50 → 2, 100 → 4), each
  worth 3 checks (3 × the price of a check). Always called an "invite", never "free" or a "trial".
- `GET /v1/pricing`: each pack adds `invites: {"count", "checks_each", "checks"}` (`checks` = all its invites
  together; the 25-pack brings 25 + 3 = 28 checks), and its `label` ends with "· comes with 1 invite of 3 checks to
  give away" (nl: "· met 1 uitnodiging van 3 checks om weg te geven"). Top level adds `invites: {"checks_each",
  "per_checks", "for"}`.
- **Claim token.** `POST /v1/credits/purchase-tx` → also `claim`; `GET /v1/credits/solana-pay.svg` → also
  `X-Unfret-Claim`; `POST /v1/credits/fiat-checkout` → also `checkout_id` and `claim`. Only its hash is stored. It
  never goes on chain or to the card provider. Keep it in the buyer's page (the site keeps pending ones in
  localStorage for up to three days, e.g. across Polar's checkout).
- `GET /v1/credits/quotes/{quote_id}` → also `invites: [str]`. With header `X-Unfret-Claim: <claim>`, the first
  response after payment carries the pack's codes (`ufi_` + 32 base32 characters); they are wiped in the same
  statement, so every later response (and every response without the right claim) has `invites: []`. Shown once:
  a client that loses that response can't get them again.
- `GET /v1/credits/fiat-checkouts/{checkout_id}` (header `X-Unfret-Claim` required; 404 `UNKNOWN_CHECKOUT` without
  it, with a wrong one, or for an unknown checkout; 503 while card payments are off) → `{"paid", "checks",
  "invites"}`, the same once-only rule. `paid` turns true when the webhook credits the order.
- `POST /v1/invites/redeem` `{"code"}` with `Authorization: Bearer <session token | ufk_ API key>` →
  `{"checks_added": 3, "usd_micros_added", "balance_usd_micros", "checks_remaining"}`. Only for an account that never
  had any credit (no purchase, refund, reward or grant: a new wallet, or a key never topped up), once per account;
  each code works once. Errors: 400 `INVALID_INVITE`, 404 `UNKNOWN_INVITE`, 409 `INVITE_USED`, 403
  `INVITE_NOT_FOR_THIS_ACCOUNT`, 401 without a valid sign-in. Codes are case-insensitive; surrounding spaces are
  ignored.
- No issue for a transfer without a quote, a quote made without a claim (older clients), a payment worth less than
  90% of the pack, a quote's second payment, or a card order whose amount isn't exactly a pack's card price.
- CORS allows the `X-Unfret-Claim` request header.
- Storage: `invites (code_hash, issued_on, redeemed_on)` only; `wallets.invite_redeemed`; `vat_quotes` and
  `fiat_checkouts` gain `claim_hash` and `invite_codes` (plaintext until first read, or until the row goes: a quote a
  day after it expires, a card checkout's codes after 7 days), `fiat_checkouts` also `checks` and `paid_at`.

## Changes (card payments through Polar, backwards-compatible)

- `POST /v1/credits/fiat-checkout` is live once Polar is configured (`/v1/pricing` → `fiat.available: true`):
  `{"key_id", "checks"}` → `{"checkout_url", "provider": "polar"}`; send the buyer to `checkout_url` (Polar's hosted
  checkout, in USD). Afterwards Polar returns them to `/checkout?paid=1`; the key is credited when Polar's
  `order.paid` webhook arrives, usually within seconds. Still 503 while card payments are off, and now also when
  Polar can't start the checkout (unreachable, or the product's price doesn't match the pack).
- `GET /v1/health/ready` adds `fiat`: `"off"`, or the provider and its environment (`"polar:production"`,
  `"polar:sandbox"`).

## Changes (shared results, backwards-compatible)

- `POST /v1/analyze` and the stream's `result` event add `share_token` (absent when the page wasn't assessed, the
  request had no web `url`, or sign-in is off). It is an HMAC over the product address (fragment, credentials and
  tracking parameters dropped), a hash of the result as a share would show it, whether `checkout_content` was sent,
  the product title and an expiry (7 days). It names no account.
- `POST /v1/shares/preview` and `POST /v1/shares`, **without `Authorization`** (a request carrying one is a `400
  AUTHORIZATION_NOT_ALLOWED`): `{"share_token", "url": <the url that was analysed>, "result": {"score",
  "explanation", "assessment"?, "verdict"?}}` (other result fields are ignored). The preview answers exactly what
  would be public, stores nothing: `{"title", "host", "product_url", "checked_on": "YYYY-MM-DD", "checked_on_text",
  "band", "label", "symbol", "headline", "result": {"verdict": {band, label, symbol, headline, reasons: [{title,
  detail, severity, thumbnails}], positives: [{title, detail}]}, "assessment"?: {flags: [{label, evidence: "",
  kind}], confidence, model_version}}, "notice"}`; show `notice` ("Anyone with the link sees this verdict and the
  product link. It never says who checked.") before the sharer confirms. Wording as the showcase: no score, never
  "scam" or "fraud"; a detail or headline quoting the page is left out (headline: the band's catalogue sentence),
  flag evidence is always empty. `POST /v1/shares` → `201 {"id", "link": "/r/<id>", "delete_code", "expires_at"}`
  (90 days; the delete code is shown only now). Errors: `400 BAD_SHARE_TOKEN` (forged, or doesn't match the url or
  result), `422 SHARE_TOKEN_EXPIRED`, `422 CHECKOUT_NOT_SHAREABLE` (the check had `checkout_content`), `422
  NOT_SHAREABLE`, `429` (20 shares per visitor network per day, `UNFRET_SHARES_PER_VISITOR_PER_DAY`, and
  `UNFRET_SHARES_DAILY_CAP` in total; previews 30 a minute), `503` without database or session secret.
- `GET /v1/shares/{id}` → the preview's fields (without `notice`) plus `id`, `link`, `expires_at`; `X-Robots-Tag:
  noindex, nofollow`. 404 `NOT_FOUND` once deleted, expired or hidden.
- `DELETE /v1/shares/{id}` with `X-Delete-Code: <code>` → 204; without it `400 DELETE_CODE_REQUIRED`, a wrong one
  `403 WRONG_DELETE_CODE`.
- `POST /v1/shares/{id}/object` `{"reason"?: str ≤ 500}` (a shop owner; no auth) → `{"hidden": true}`: hidden at once
  until the operator reviews it (`GET /v1/admin/shares[?hidden=false]`, `POST /v1/admin/shares/{id}/unhide`,
  `DELETE /v1/admin/shares/{id}`, admin token).
- Page: `/r/{id}`, the result in the link checker's presentation, "Checked by an Unfret user on <day>; the page may
  have changed", a `rel="nofollow noopener noreferrer"` product link, `noindex` (header and robots meta), and forms
  to object or delete.
- CORS: `DELETE` and the `X-Delete-Code` header are allowed.

## Changes (the build commit in health, backwards-compatible)

- `GET /v1/health` and `GET /v1/health/ready` add `commit`: the commit the running image was built from, or `null`
  outside a built image (local runs, tests).

## Changes (English only, backwards-compatible)

- The app writes everything in English. `POST /v1/analyze[/stream]` and `/v1/check` ignore **`Accept-Language`**:
  `verdict` (label, headline, reasons, positives), `assessment.flags[].label` and the early-exit `explanation` are
  always English, and `verdict.language` is always `"en"` (kept for clients that read it). The same input with
  another `Accept-Language` is the same cached result.
- `GET /v1/pricing`: each pack's `label` is `{"en": …}` only (no `nl`).
- The extension keeps working unchanged: it may still send `Accept-Language` and its own interface is its own.

## Changes (copyright lines, backwards-compatible)

- New optional `PageFacts.copyright_lines` (list of strings, at most 10; snapshot updated): the page's copyright lines
  (`© 2026 Acme Outdoor LLC. All rights reserved.`), each the ©, "(c)" or "Copyright" plus the 100 characters after
  it; the server keeps three. The `us_identity` skill reads the company name from them, since the page text it gets has
  no footer; they never count as an imprint. Clients that extract facts themselves (the browser extension) should fill
  it, or a company named only in the footer isn't seen.

## Changes (invite eligibility in credits, backwards-compatible)

- `GET /v1/credits` adds `invite_eligible` (bool): true while the account has never had credit, the same test
  `POST /v1/invites/redeem` makes, so a client can show "Have an invite?" only where a code would be accepted.

## Changes (page classes beyond products, backwards-compatible)

- New optional `PageFacts` fields (snapshot updated), all with empty defaults: `product_ids` (object: `gtin`,
  `gtin8`/`12`/`13`/`14`, `mpn`, `asin`), `payment_methods` (strings such as `paypal`, `ideal`, `bank_transfer`,
  `crypto`, `gift_card`), `phones` (`{"number", "kind": mobile|local|national|toll_free|premium|unknown, "country"}`),
  `emails` (`{"address", "kind": own_domain|webmail|other}`), `addresses`, `founded_claims`, `business_name`,
  `legal_form`, `kvk_numbers`, `licences` (`{"number", "state"}`), `certifications`, `cities_served`,
  `internal_links` (`{"text", "url"}`), `pricing_terms`, `promises`, `testimonials`, `organization_ld`, and the
  nullable objects `seller_profile` (`seller_type` business|private, `member_since`, `rating`, `rating_count`, `badges`,
  `other_listings`, `response_time`, `response_rate`, `jobs_completed`) and `listing` (`negotiable`, `bidding`,
  `condition`, `category`, `listed`, `location`, `delivery`, `payment_protection`, `contact_methods`,
  `payment_requests`). Clients that extract facts themselves (the extension) fill them by the same rules as the
  server: contacts and addresses only from what the business publishes, nothing from forms, account, checkout,
  billing or delivery areas; never a private seller's name.
- `PageFacts.platform` may also be `ebay`, `marktplaats`, `2dehands`, `vinted`, `craigslist`, `facebook`, `werkspot`,
  `thumbtack`, `angi`, `homeadvisor`, `checkatrade`, `taskrabbit`, `bark`, `houzz` or `myhammer`. An older server
  ignores the new fields (unknown fields are dropped) but rejects the new platform values (422), so a server with
  this change must be deployed before an extension that sends them; an older client keeps working unchanged.
- While the server runs with `UNFRET_EXPANSION_CLASSES` on (off by default; on for dev.unfret.net and unfret.net),
  `page_type` (stream `step` of `classify`, x402 `page_type`) may also be `p2p_listing`, `service_listing` or
  `service_site`. Those pages are analysed and charged like product pages; their `verdict` band is `concerns` or
  `careful` (no `assessment`: there is no trained scorer for them yet). Stream `step` events keep their shape; the
  skills of every class report their steps as before.

## Changes (product-page upgrades, backwards-compatible)

- New skills on product pages, with features the shipped scorer doesn't use yet (scores are unchanged until it is
  retrained): `payment_methods` (storefronts: `.irreversible_only`, `.irreversible`, `.protected`; findings
  `irreversible_payment_only`, `protected_payment`), `claims` (storefronts: `.origin_contradicted`,
  `.store_without_address`; findings of the same names), `market_price` (`.other_shops`, `.below_market`,
  `.far_below`; finding `far_below_market`), `review_copies` (`.checked`, `.copied`, `.copied_share`, `.other_shops`;
  finding `reviews_on_other_shops`), `brand_presence` (`.wikidata`, `.own_domain`, `.own_domain_age_years`,
  `.no_presence`, `.shop_is_brand`; findings `brand_on_wikidata`, `brand_domain`, `brand_no_presence`) and
  `safety_gate` (`.barcode_match`, `.model_match`; finding `safety_gate_alert`, whose evidence ends with the alert's
  public URL). `domain_age` adds `.founded_claim_years` and `.founded_contradicted` (finding of the same name);
  `shop_links` adds `.shared_address_shops` (finding `shared_address`).
- New shopper reason keys `safety_gate_alert`, `irreversible_payment_only`, `founded_contradicted`,
  `far_below_market`, `origin_contradicted`, `copied_reviews_elsewhere`, `brand_no_presence`,
  `store_without_address`, `shared_address`, and positive `protected_payment`; `known_brand` may now also come from
  Wikidata. Clients that map reason keys should fall back to the `title`/`detail` text they already get.
- `POST /v1/admin/contacts/forget` accepts `kind: "address"` (a postal address, normalised and hashed as stored).

## Changes (service providers, behind `UNFRET_EXPANSION_CLASSES`)

- `PageFacts.testimonials` is now a list of objects, `{"text", "name", "rating": 0–5|null, "posted": "YYYY-MM-DD"|null}`
  (it was a list of strings, never filled until now): the testimonials a service company publishes on its own site,
  the name only as the site prints it. Empty on a product page (its reviews are in `reviews`) and on a platform
  profile, whose reviews go into `reviews` without names. A client that sends strings there gets a 422; the
  extension sends objects from this release on.
- `PageFacts.seller_profile` adds `years_in_business` (integer or null). On a service platform's profile page
  (Werkspot, Thumbtack, Angi, …) the extractors fill `seller_profile` (member since, jobs completed, years in
  business, rating and count, badges, response rate and time) and `reviews`.
- With the flag on, `service_site` and `service_listing` pages get a full verdict from hand-written rules: any of the
  four bands (`reliable` only with concrete positives), still no `assessment`. New reason types (`doorway_pages`,
  `linked_sites`, `copied_photo`, `premium_number`, `upfront_payment`, `cash_only`, `quote_on_site`, `new_site`,
  `copied_testimonials`, `placeholder_names`, `testimonial_dates`, `all_five_testimonials`, `kvk_malformed`,
  `kvk_missing`, `licence_no_number`, `names_differ`, `bait_callout`, `emergency_pressure`, `vague_pricing`,
  `no_street_address`, `new_on_platform`, `few_platform_reviews`, `low_response_rate`) and positive types
  (`service_established`, `service_identity_nl`, `service_identity_us`, `licence_shown`, `written_quote`,
  `platform_track_record`, `platform_verified`, `service_nothing_found`); clients render `title`/`detail` as for
  any other reason, so nothing changes for them.
- A service provider's result has no `share_token` (results about a business stay private).
- New stream `step` skills on those pages: `service_identity`, `doorway_pages`, `service_terms`, `testimonials`,
  `platform_profile`; `reverse_image` may run there without being asked for (the rules decide).
- `POST /v1/admin/contacts/forget` also takes `"kind": "email_domain"` (a domain such as `example.nl`); phone
  numbers of service sites are stored with their country code, so remove them as `+31 30 234 5678`.

## Changes (peer-to-peer ads, backwards-compatible)

- `PageFacts.listing` gains `description` (string, at most 5,000 characters, default empty): the ad's own text as
  the seller wrote it, not the platform's (snapshot updated). The server reads off-platform wording, contact details
  for the duplicate check and the text fingerprint from it; without it, only the page text's payment wording is read.
  `contact_methods` are `whatsapp`, `telegram`, `phone`, `email`; `payment_requests` are `bank_transfer`,
  `payment_link`, `deposit`, `gift_card`, `crypto`, `money_transfer`, `payment_app`. An older server drops the new
  field; an older client keeps working (the server's own extractor fills it from the HTML).
- With `UNFRET_EXPANSION_CLASSES` on, a `p2p_listing`'s `verdict` may have any of the four bands (`reliable`,
  `concerns`, `careful`, `junk_or_scam`), from hand-written rules, still without `assessment`. Its reasons and
  positives have `p2p_` types (e.g. `p2p_advance_payment`, `p2p_new_account`, `p2p_established`); the shape is the
  same as for products, so clients render them unchanged. `score` is the band's representative value. New stream
  `step` events come from the ad skills `seller_profile`, `off_platform`, `price_outlier` and `duplicate_ads`, and
  from `reverse_image` (now run for every ad with a photo).

## Changes (card payments in euros, iDEAL | Wero; backwards-compatible; migration 21)

- `GET /v1/pricing` adds, per pack, `price_eur_cents` and `price_eur` (e.g. `550`, `"€5.50"`; before the provider's
  sales tax/VAT like `price_usd`; both `null` while euro prices are off), a top-level `card_currency` (`"eur"` for a
  visitor whose `vat.country_hint` is in the euro area, else `"usd"`), and `fiat.currencies` (`["usd", "eur"]`, or
  `["usd"]`) and `fiat.min_eur_cents`. Every existing field is unchanged; `label` stays in dollars. Show a euro-area
  visitor the euro price as the card price (crypto stays in dollars, as the discount).
- `POST /v1/credits/fiat-checkout` takes an optional `currency`: `"usd"` or `"eur"` (case-insensitive; anything else
  422). Without it the visitor gets `card_currency`, so a client that sends nothing from the euro area now gets a
  euro checkout. `"eur"` while euro prices are off is a 400. The response adds `currency`, the checkout's actual
  currency: if Polar refuses the euro checkout (e.g. a product without its euro price), the dollar one is returned
  instead. On a euro checkout Polar offers iDEAL | Wero to buyers in the Netherlands next to cards.
- A paid euro order credits the same pack value as the dollar one, only at exactly the pack's euro price before tax;
  any other euro amount credits nothing (`too_small`), and a currency other than USD or EUR answers
  `unknown_currency`. Refunds and lost disputes take back credits pro rata in the order's own currency, as for
  dollars. Storage: `fiat_payments.currency` (`usd` for every earlier order).
- Setting: `UNFRET_CARD_PRICES_EUR` (default `25=5.50,50=10.00,100=19.00`; one per pack, or empty for dollars only).

## Changes (local payment methods: iDEAL | Wero as its own button; backwards-compatible)

- `GET /v1/pricing` adds a top-level `local_payment_method`: `{"id": "wero", "label": "iDEAL | Wero", "currency":
  "eur"}` for a visitor whose `vat.country_hint` has a local method (today only `NL`, and only while euro prices are
  on), else `null`. Show it as its own button ("Pay €5.50 with iDEAL | Wero") next to the card button, priced in
  its `currency` (`price_eur` for `eur`). Which countries get which method is decided by the server's table
  (`unfret.credits.local_methods.LOCAL_METHODS`), so a client needs no country list.
- `POST /v1/credits/fiat-checkout` takes an optional `payment_method`: `"card"` (default, as before: the buyer picks
  their country on Polar's page, cards until they do) or a local method's `id` (case-insensitive; an unknown id is a
  422). For `"wero"` the checkout is in EUR with the billing country prefilled as `NL`
  (`customer_billing_address: {"country": "NL"}`, [Polar: create checkout session](https://polar.sh/docs/api-reference/checkouts/create-session)),
  which is what makes Polar show iDEAL | Wero; the buyer can still change it. The method's currency is used when
  `currency` is left out; another `currency` is a 400 (`"iDEAL | Wero is paid in EUR."`), and so is `"wero"` while
  euro prices are off. Any visitor may ask for it (the country hint only decides what `/v1/pricing` offers).
- The response adds `payment_method`: the method asked for, or `"card"` when Polar refused the euro checkout and the
  dollar card checkout was made instead (`currency` is then `"usd"`). Metadata, claim, crediting, refunds and
  disputes are exactly those of a euro card checkout.
- Adding a country is one entry in that table (visitor country → method id, label, billing country, currency), after
  checking by hand that Polar really shows that method for that billing country and currency.

## Changes (the visitor's country is a visible, changeable suggestion; backwards-compatible)

- `GET /v1/pricing` adds two tables, the same for every visitor, so a client can price and offer payment methods for
  the country the visitor *chooses* rather than only for Cloudflare's guess:
  - `euro_area`: the sorted ISO codes whose card prices are in euros (the 21 euro countries; `[]` while euro prices
    are off). Card price in `price_eur` for these, `price_usd` for every other country (or none chosen).
  - `local_payment_methods`: `{ISO code: {"id", "label", "currency"}}`, each country's local method (today
    `{"NL": {"id": "wero", "label": "iDEAL | Wero", "currency": "eur"}}`; `{}` while euro prices are off).
- `vat.country_hint` is the **suggested** country only: show it, let the visitor change it, and let the chosen country
  decide the card currency and the local method. `card_currency` and `local_payment_method` stay, unchanged, as the
  values **for the suggested country**, for clients that don't offer a choice. The hint comes from Cloudflare's
  country header (never the IP) and is neither stored nor logged.
- `POST /v1/credits/fiat-checkout` is unchanged; clients that let the visitor choose send the explicit `currency` (and
  `payment_method` for a local method), as `/checkout` does.

## Changes (service providers: new reasons, HomeGuide; backwards-compatible, behind `UNFRET_EXPANSION_CLASSES`)

- `PageFacts.platform` may also be `homeguide` (snapshot updated). An older server rejects the value (422), as for
  the other platform values.
- Service verdicts may carry the reason types `not_local` (high), `trade_town_name`, `national_number_only`,
  `address_elsewhere`, `shared_address_sites`, `details_on_request`, `unproven_profile` and `low_platform_rating`
  (medium). Clients render `title`/`detail` as for every reason.
- New features: `doorway_pages.trade_town_name`, `.not_local`, `.national_number_only`, `.local_page`,
  `.address_elsewhere`; `service_terms.details_on_request`; `platform_profile.unproven`, `.low_rating`.
- A Houzz `/professionals/` page fetched by the server is a bot challenge and now answers `PAGE_BLOCKED`.

## Changes (page type in the analysis response, backwards-compatible)

- `POST /v1/analyze` (and the stream's `event: result`) adds `page_type`: `aggregate_listing`, `storefront`,
  `p2p_listing`, `service_listing`, `service_site`, `article` or `non_product`, the value the stream's `classify` step
  already carried. New values may be added; treat an unknown one like `storefront`.
- A link check of a domain that doesn't resolve answers `PAGE_UNAVAILABLE` ("The site couldn't be reached: …")
  instead of `INVALID_URL`, which stays for addresses that resolve to private or internal networks.

## Changes (limits per visitor network on codes and quotes, backwards-compatible)

- `POST /v1/invites/redeem`: 429 `TOO_MANY_REQUESTS` (`metadata.retry_after_s`) after `UNFRET_REDEEM_PER_IP_PER_10MIN`
  (10) attempts from one visitor network (IPv6 /64) in ten minutes, whatever their outcome.
- `POST /v1/credits/purchase-tx` and `GET /v1/credits/solana-pay.svg`: 429 `TOO_MANY_REQUESTS` after
  `UNFRET_QUOTES_PER_IP_PER_MINUTE` (20) quotes from one visitor network in a minute.

## Changes (onboarding: key hand-off to the extension, `/welcome`; backwards-compatible)

- `GET /welcome` — the page the extension opens once after a new install (Unfret Dev: `https://dev.unfret.net/welcome`):
  a gift code (`/checkout?mode=code`), buying checks (`/checkout`), or signing in with a key you have. Static, no
  script.
- `/checkout` hands every new API key it shows (a gift code, a card payment's return, a recovery, the crypto tab's
  "Make a new key") to the extension: `window.postMessage({"type": "unfret-key", "key": "ufk_…"}, location.origin)`,
  and once the user agreed to switch accounts, the same with `"replace": true`. The extension's content script, injected
  only on `<site>/checkout*`, accepts it only from that window on the configured origin, and answers
  `{"type": "unfret-key-received", "status"}` with `status` one of `signed-in`, `already` (it uses this key),
  `other-account` (signed in with another key or a wallet: nothing changed; ask the user, then post again with
  `replace: true`) or `invalid`. The answer never carries a key, an account or a balance. No answer within 2 s: no
  extension (or one older than 0.11.0); the page shows the store links and "choose API key and paste your key".
- `/checkout?mode=topup&key_id=<10-letter key ID>` opens "Top up my key" with that key filled in ("Topping up key …",
  "use another key"). Only the public key ID goes in the link, never the key. The extension (0.11.0) and `/check` link
  there for a signed-in key; a wallet's link stays `/checkout`.

## Changes (a limit on uncharged analyses, backwards-compatible)

- `POST /v1/analyze` and `/v1/analyze/stream`: 429 `TOO_MANY_REQUESTS` (`metadata.retry_after_s`) once an account has
  had `UNFRET_ANALYZE_FREE_OUTCOMES_PER_HOUR` (30) analyses in the past hour that ran but weren't charged (the client
  hung up before the result, or the page couldn't be assessed: score -1). Refunds are unchanged; analyses that failed
  on our side (timeout, error) don't count.

## Changes (card purchases picked up in another browser, card prices with VAT; backwards-compatible; migration 22)

- The card checkout's success URL is now `/checkout?paid=1&checkout=<checkout id>` (Polar fills in `{CHECKOUT_ID}`).
  `/checkout` removes both parameters from the address bar at once.
- `POST /v1/credits/fiat-checkouts/{checkout_id}/pass` (no auth): for a buyer the bank app or phone brought back to
  another browser, which has neither the pass (API key) made for the purchase nor its claim token. It rotates that key
  and returns `{"key_id", "key", "recovery_code", "account", "checks", "checks_remaining", "invites"}`: the same id and
  balance, a new key and recovery code (the old ones stop working), and the pack's invites if nobody collected them.
  Once per checkout, within 24 hours of the payment, only while the buying browser hasn't picked the purchase up
  itself, and only for a pass made for the purchase (never a top-up of a pass in use). Refusals: 404
  `UNKNOWN_CHECKOUT`, 409 `CHECKOUT_NOT_PAID` (`Retry-After: 3`; the webhook hasn't arrived yet), 409
  `CHECKOUT_TOPPED_UP` (the checks went to a pass that had credit before or got some since; nothing to collect),
  409 `CHECKOUT_COLLECTED`, 410 `CHECKOUT_EXPIRED`, 429, 503 while card payments are off.
- `GET /v1/credits/fiat-checkouts/{checkout_id}` adds `reissued` (bool): the purchase was picked up in another
  browser and the key this browser made for it no longer works. A poll with the claim token after payment marks the
  purchase as picked up by the buying browser, so it can no longer be reissued.
- `GET /v1/pricing` adds `vat.card_rates_pct`: each EU country's standard VAT rate (percent, as text), which is what
  the card provider adds to a card price for a consumer there. Pages show card prices with it ("€6.66 incl. VAT" for
  the 25-pack in the Netherlands), so the price doesn't go up at the provider's checkout. `vat.eu_rates_pct` is
  unchanged: what Unfret itself charges on a crypto purchase (the Dutch rate for everyone while under the EU
  threshold).
- `/checkout` sells by card while crypto purchases are off (no hot wallet or scanner) and hides only the crypto tab;
  it says "not available" only when neither way is.

## Changes (new keys past the daily number, backwards-compatible)

- `GET /v1/keys/challenge`: `bits` may be higher than usual (at most 6 more) once many keys were made that day: past
  `UNFRET_KEY_CREATE_DAILY_CAP` (500) each doubling adds a bit. `POST /v1/keys` no longer answers 429 for the day's
  total, only after 5 new keys from one visitor network; recoveries and card pick-ups have only their per-network
  limit (10 a day).

## Changes (plain words for shoppers: the pass, My checks; backwards-compatible)

- **Words.** The pages call an API key an **Unfret pass** and its recovery code a **backup code**. A pass is shown by
  its ID in capitals in two groups: `ABCDE-FGHIJ` (the key ID `abcdefghij`); `/checkout?mode=topup` and the crypto
  tab accept the ID typed that way. Nothing changes in the API. The extension should use the same words ("Paste your
  pass", "I have a pass") so the pages' instructions match it.
- **Headlines never repeat the label.** `verdict.headline` (and `explanation`) no longer start with the label, which
  every client shows right above them: "Fake countdown and unknown brand." rather than "Many warning signs: fake
  countdown and unknown brand."; a reliable page with a note reads "One thing to note: …". An LLM-written headline
  that starts with the label has it removed. Results cached or shared before this still carry the old form: clients
  may strip a leading `"<label>: "` (the site does, `headlineOf` in `static/check-render.js`).
- `GET /v1/keys/phone-qr` (`Authorization: Bearer <key>`; API keys only, 400 for a wallet's session, 401 without a
  valid key) → `image/svg+xml`, `Cache-Control: no-store`: a QR code for `<site>/pass#pass=<key>` (it was
  `<site>/check#pass=<key>` until the Android app's App Link; see "the pass's phone link" below). `/check` reads
  `#pass=`, checks the key against `/v1/credits`, asks "Use this pass here?" (saying so when it already has another
  pass or a wallet), then keeps it as its sign-in, and clears the address bar.
- `GET /my-checks` ("My checks", in the menu instead of "Sign in"): the pass or wallet on this device (the link
  checker's `localStorage` sign-in), its checks, adding checks (`/checkout?mode=topup&key_id=`), "Use on your phone"
  (the QR code above), sending the pass to the extension, removing it, a new pass from the backup code
  (`POST /v1/keys/recover`, moved here from `/checkout`'s top-up tab), a backup code from `/checkout` not yet
  confirmed as saved, and the wallet sign-in (SIWS, for checks bought with crypto).
- **Extension hand-off from `/my-checks` too:** "Send to the extension" opens `/checkout?send=pass`, which offers
  this browser's pass (the link checker's sign-in) to the extension with the same `{"type": "unfret-key", "key"}`
  message and the same answers as every other hand-off. The extension's key relay and its sender check stay on
  `<site>/checkout*` only: nothing changes for extension 0.11.0. `/checkout` drops `send` from the address bar.
- `/check` no longer signs in with a wallet itself (that moved to `/my-checks`); it still uses a wallet's sign-in
  stored by `/my-checks`. Without checks it shows "Get checks to start" (buy, a code, or paste a pass). Its progress
  list follows the page type from the classifier's `step` event (`page_type`): a shop's, an ad's or a company's five
  points. The scorer's details ("Show details": signals, model, confidence) are no longer shown on `/check`, shared
  results or the showcase; the API still returns `assessment`.
- `/checkout` tabs: `mode=new` ("Buy checks"), `code`, `topup` ("Add to my pass"). `mode=crypto` is no longer a tab
  but still opens the crypto panel directly (links from the extension keep working); without crypto purchases it
  falls back to `new`. In a browser whose link checker uses a pass, "Buy checks" tops that pass up.

## Changes (photo thumbnails, backwards-compatible)

- `GET /v1/images/{id}/thumbnail.jpg`: 429 `TOO_MANY_REQUESTS` after 120 thumbnails from one visitor network in a
  minute; a photo that couldn't be fetched or read answers 404 for an hour without being fetched again; a photo over
  20 megapixels gets no thumbnail (404).

## Changes (a page for developers and agents, backwards-compatible)

- `GET /developers`: the MCP server, x402 on `/v1/check` and a prepaid pass as an API key, with the tiers' prices
  and descriptions from the same settings as `/.well-known/x402`. Linked from every page's menu and footer and from
  the OpenAPI document's `info.description` (so from `/docs`).
- Roadmap: x402 on Base (USDC), next to Solana.

## Changes (the four verdict names; backwards-compatible)

- **Words only.** `verdict.label` reads **"Looks fine"** (`reliable`), **"Worth a look"** (`concerns`), **"Think
  twice"** (`careful`) or **"Serious concerns"** (`junk_or_scam`), replacing "Looks fine", "A few things to check",
  "Some warning signs" and "Many warning signs". The band identifiers in `verdict.band`, `/v1/check`'s `band` and
  `never_warns_on_bands` are unchanged, so a client that maps bands to its own words keeps working; clients should
  use these words when a result comes without a label. `/check`, `/r/{id}` and the extension use them.
- **"Serious concerns" only at high confidence.** For a page the trained scorer judged, `junk_or_scam` needs the
  scorer's `assessment.confidence` to be at least 0.6 (deep tier; the quick tier's scorer 0.7: settings
  `verdict_serious_min_confidence[_quick]`); below that the same score is `careful`. `score` and `/v1/check`'s
  `risk_score` and `action` are unchanged (the action never depended on `careful` versus `junk_or_scam`). Peer-to-peer
  ads and service providers keep their rule-based bands, where `junk_or_scam` already needs one of the listed strong
  combinations.
- **A reason below "Looks fine".** Every verdict whose band isn't `reliable` has at least one entry in `reasons`: for
  products "Several small signals" (`overall`) when nothing else fired, for ads "Little to go on"
  (`p2p_little_known`), and now for service providers "Little to go on" (`service_little_known`, new). A
  `service_listing` page that came without the provider's profile is still not analysed (score -1).
- **Shared results** (`/v1/shares/preview`, `GET /v1/shares/{id}`, `/r/{id}`) name the verdict with the same four
  words in `label` and `result.verdict.label`, also for shares stored before ("Unfret would warn you about this
  listing" before); a stored headline that starts with a former label loses it. The share token and what is stored
  are unchanged. The public `band` stays `warning` for `junk_or_scam`.

## Changes (the showcase is retired; migration 24)

- `GET /v1/showcase`, `GET /v1/showcase/{id}`, the pages `/showcase/{id}` and `/showcase/{id}/screenshot` and the
  operator's `/v1/admin/showcase…` endpoints are gone (404). Migration 24 drops `showcase_cases`. No client used them
  but the site itself, whose home page shows cards about the tricks Unfret catches instead.
- Shared results (`/v1/shares…`, `/r/{id}`) are unchanged: the same public wording, now in `unfret.public_result`.

## Changes (free checks: a free pass, quick checks for everyone; backwards-compatible; migration 25)

Additive: a client that sends none of the new fields sees the same behaviour as before (extension 0.11.0, the MCP
client and `/v1/check` are unchanged).

- `POST /v1/keys` takes an optional `"free": true`: the new pass comes with `free_checks_per_pass` (5) free deep checks,
  within `free_passes_per_ip_per_day` (2) such passes per visitor network (in memory, salted hashes, never stored);
  past that the pass is made empty. The proof of work and the per-network key limits apply as before. The response
  (and `POST /v1/keys/recover`'s) adds `free_checks` (int). The extension asks for one on install, `/check` on a
  browser's first check; each remembers in the browser that it did (extension: `storage.sync`, `/check`:
  `localStorage`), so a reinstall gets a pass without free checks.
- Free checks are spent before the balance. `GET /v1/credits` adds `free_checks_remaining` (included in
  `checks_remaining`) and `free_checks_paused`. They are not credit: a pass with only free checks keeps
  `invite_eligible: true` (a gift code still works on it).
- **One budget a day for free deep checks**: every free deep check counts towards a global counter per UTC day
  (`usage_counters`, `free_checks`), capped at `free_daily_budget_eur` (default 5.00) over `free_check_cost_eur`
  (default 0.005, an estimate of one deep check's LLM and embedding cost): 1,000 a day by default. Once it is reached,
  a deep check that only free checks could pay for is refused with **429 `FREE_CHECKS_PAUSED`** ("Free deep checks
  are back tomorrow (00:00 UTC). Quick checks still work.", `metadata: {"retry_after_s", "quick_checks": true}`),
  nothing is taken, and bought checks are used if the pass has any. A free check whose analysis fails, times out, is
  cancelled or can't assess the page is given back to the pass and to the day's budget.
- `POST /v1/analyze` and `/v1/analyze/stream` take an optional `"tier"`: `"deep"` (default: unchanged) or `"quick"`
  (the x402 quick tier's rule-based checks of the page itself: no LLM, no lookups elsewhere; product pages). A quick
  check is never charged, free with or without `Authorization` (a header that's there must still be valid: 401
  otherwise), at most `quick_checks_per_minute` (10) per account or visitor network, and carries no `report_token` or
  `share_token`. A deep check without `Authorization` is refused as before (401).
- The analysis response adds `tier` (`"deep"` or `"quick"`) and `free_check: true` when a free check paid for it.
- `POST /v1/keys/move-free-checks` (`Authorization: Bearer <pass>`, body `{"to_key_id": "<id or pass>"}`) →
  `{"moved": n}`: moves every free check left on the authenticating pass to another pass; never balance. The extension
  does it when it switches from the free pass it made to a pass its user bought. 400 for a wallet's session.
- `GET /v1/pricing` adds `free: {"checks_per_pass", "quick_checks": true, "paused", "back_at": "00:00 UTC"}`.
- **Key hand-off** (`/checkout` → extension): extension 0.12.0 answers `"other-account"` with `"free_pass": true` when
  what it has is the free pass it made; on `replace: true` it moves that pass's free checks to the new pass before
  switching. `handoff.js` reports this as `"free-pass"` and the page asks "Switch it to this pass? Any free checks left
  on it come along".
- `GET /welcome?free=<n>` (1–100): "You have n free checks" and how to check a page, opened by the extension after it
  made its free pass; without it (or 0), the page for a pass without free checks, as before.
- `/check`: a first visit's first Check makes a free pass (`static/new-pass.js`, shared with `/checkout`); without a
  pass, or when a deep check needs checks the pass doesn't have (402, or 429 `FREE_CHECKS_PAUSED`), the page runs a
  quick check instead and says why. Example links to try (`UNFRET_CHECK_EXAMPLES`, label and address pairs; by default the test
  shop, its sister shop and the clean shop).

## Changes (affiliates and bonus codes; gift codes for any pass; backwards-compatible; migration 26)

Additive: every new request field is optional, and every new response field is new. Extension 0.11.0 and the MCP
client are unchanged.

- **Affiliate links**: `unfret.net/?ref=CODE` on any page. The site keeps the code in `localStorage` (`unfret-ref`,
  the first one, 30 days; no cookie, no request) until a pass exists, and then sends it once:
  - `POST /v1/keys` takes an optional `"ref"` (the code). The new pass counts for that code's affiliate. An unknown or
    ended code is ignored (the pass is made either way).
  - `POST /v1/affiliates/attribute` (`Authorization: Bearer <pass>`, `{"code": "…"}`) →
    `{"status": "attributed" | "already" | "not_eligible" | "invalid"}`. It is for a pass made without the code: the
    extension's install pass, relayed by `/welcome` (below). It returns `not_eligible` for a wallet or for a pass that has bought
    checks before, and `already` once a pass counts for an affiliate: the first one stays. 429 after
    `code_checks_per_ip_per_day` (30) look-ups from one network.
  - `/welcome` (both versions) posts `{type: "unfret-ref", code}` to itself while it has a kept code, and forgets the
    code once it gets `{type: "unfret-ref-received", status}` back. Extension 0.12.0's content script on `/welcome`
    relays it to the background, which calls `POST /v1/affiliates/attribute` with its own pass.
- **Bonus codes**: `POST /v1/credits/codes/check` (`{"code": "…"}`) → `{"code", "label", "bonus_pct",
  "first_pack_only"}` shows what a code gives ("+20% checks", or an affiliate's "+20% checks on your first pack").
  It returns **404 `INVALID_CODE`** ("This code isn't valid, or has ended.") for every code that can't be used
  (unknown, past its last day, used up, switched off), with one answer for all, and 429 after too many tries from one
  network. Codes give checks, never a lower price.
  - `POST /v1/credits/fiat-checkout`, `POST /v1/credits/purchase-tx` and `GET /v1/credits/solana-pay.svg` take an
    optional `code`, one per purchase (404 `INVALID_CODE` as above). The price is unchanged; when the pack is paid, a
    bonus code adds `bonus_pct` more checks (whole checks). A code's use is counted when the purchase is paid,
    atomically: purchases started on a code's last use get it once, and the others are credited without it.
  - Without a code, or with an affiliate's own code, the **first pack** bought on a pass that counts for an
    affiliate comes with `affiliate_bonus_pct` (20%) more checks, once per pass. A paid purchase started with an
    affiliate-linked code also makes a pass that never bought before count for that affiliate. Bonus checks are part of
    a card order's credits, so a refund or chargeback takes their share back.
- `GET /v1/affiliates/stats` (header `X-Unfret-Stats: ufa_…`, the token from an affiliate's private link
  `/affiliates/stats#ufa_…`) → `{"name", "active", "codes", "months": [{"month", "passes", "first_checks",
  "purchases", "money": {"usd"|"eur": {"net", "commission", "reversed", "paid"}}}], "balance": {"usd"|"eur":
  {"payable", "held", "due", "carried_over"}}, "rules": {"hold_days", "min_payout_cents"}}`, in cents. Commission is
  held 30 days (the refund window), a payout needs 2500 cents per currency (all of it once the affiliate is switched
  off), and a refund after a payout makes `payable` negative until later commission makes up for it. These are counts
  and amounts only. 404 `UNKNOWN_STATS_LINK` for an unknown or revoked token; `Cache-Control: no-store`.
- **Gift codes** (invites, `ufi_…`): `POST /v1/invites/redeem` now accepts **any** account, new or in use, except the
  one whose purchase issued the code: **403 `INVITE_OWN_PACK`** ("This gift code came with your own pack: give it to
  a friend."). Each code still works once (409 `INVITE_USED`), and an account may redeem several friends' codes.
  Codes issued before migration 26 don't know their buyer and work for anyone. `GET /v1/credits`'s
  `invite_eligible` is now always `true`.
- Pages: `/affiliates` (the program and its disclosure rules) and `/affiliates/stats` (an affiliate's own numbers).
  The site's footer loads `static/ref.js` on every page.

## Changes (pages about Unfret and requests from shop owners, backwards-compatible; migration 27)

- `POST /v1/objections` (no sign-in) `{"kind": "correction"|"removal"|"other", "site": str 4–500, "message"?: str ≤
  2000, "reply_to"?: str ≤ 254}` → `201 {"status": "received", "answer_within_days": 30}`. `site` is a link or a
  domain; it is stored as host and path (scheme, `www.`, query and fragment dropped). `reply_to` is optional, an
  e-mail address if given. Errors: `422 BAD_SITE`, `422 BAD_REPLY_TO`, `422 VALIDATION_ERROR` (unknown `kind`, too
  long), `429` (5 per visitor network per day, `UNFRET_OBJECTIONS_PER_VISITOR_PER_DAY`, and
  `UNFRET_OBJECTIONS_DAILY_CAP` in total; a refused `site` or `reply_to` doesn't count), `503 QUEUE_FULL` (1000 open
  requests) or `503` without a database. Stored (`objections`): the kind, site, message, reply address and the day;
  no IP address, time of day or sign-in. Deleted by the operator once handled, or after `UNFRET_OBJECTION_DAYS` (90).
- Operator (admin token): `GET /v1/admin/objections[?limit=]` → `{"open", "items": [{"id", "kind", "site",
  "message", "reply_to", "received_on"}]}` (oldest first), `DELETE /v1/admin/objections/{id}` → `{"deleted": true}`.
  Removal requests are carried out with `DELETE /v1/admin/shops/{domain}` and `POST /v1/admin/contacts/forget`.
- Pages: `/about`, `/why` ("Why Unfret?": questions to ask any checker, Unfret next to free scam checkers and AI
  assistants, a Fakespot alternative), `/money` ("How Unfret makes money"), `/press` (fact sheet, logo files, test shop screenshots in
  `static/press/`), `/terms` (a visible draft note while `UNFRET_TERMS_DRAFT` is on) and `/objection`
  (what Unfret keeps about shops, and the form above), all in the footer. The link checker's result and a shared
  result's page link to `/objection`.

## Changes (x402 on Base, behind `UNFRET_X402_BASE_ENABLED`; backwards-compatible)

- `POST /v1/check` can offer a second x402 network: USDC on Base with the `exact` scheme (EIP-3009
  `transferWithAuthorization`), off by default. While it is off nothing changes. While it is on, the 402's `accepts`
  lists Solana's options first, unchanged, then one Base option: `network` `eip155:84532` (Base Sepolia) or
  `eip155:8453` (Base mainnet, refused unless `UNFRET_X402_BASE_ALLOW_MAINNET`), `asset` Circle's USDC contract,
  `amount` the tier's dollar price exactly (6 decimals, the same as Solana's USDC option), `payTo` the receiving
  address, `maxTimeoutSeconds` 300, and `extra` `{name, version}` (the token's EIP-712 domain: `USDC`/`2` on Sepolia,
  `USD Coin`/`2` on mainnet), `decimals`, `tier` and `includes`. No `paymentFlow`: Base uses x402's default
  (`authorization`): the payment is verified first, the analysis runs, and it is settled only when there is an
  answer, so an unreadable page (`422`), a failed analysis (`500`/`504`) or a page that can't be assessed (`200` with
  `"charged": false`) costs nothing and has no `PAYMENT-RESPONSE`. A payment is refused (402) unless it answers that
  option exactly: recipient, value, a validity window that outlives the analysis (`validBefore` at least the analysis
  timeout plus a minute ahead), a 32-byte nonce; the facilitator's own refusals come back as `error`
  (`invalid_exact_evm_insufficient_balance`, `invalid_exact_evm_signature`, …). `PAYMENT-RESPONSE` names the Base
  network and transaction; a settlement that was broadcast but not confirmed still answers 200 with the verdict and
  `PAYMENT-RESPONSE` `{"success": false, "errorReason": "settlement_pending", "transaction"}`; a refused one answers
  `402 SETTLEMENT_FAILED` (with `reason`) and no verdict. Retrying with the same authorization (payer and nonce)
  returns the same answer for 15 minutes and is never settled twice.
- `GET /v1/health/ready` gains `x402_base` (`off`, `ok`, `mainnet_refused`, `misconfigured`, `rpc_unavailable`,
  `compromise_suspected`) and `x402_base_alert_count`; with the operator token also `x402_base_alerts` and
  `base_pay_to_usdc_units`. `GET /v1/admin/x402/alerts` gains `base` (status, network, payTo, its USDC, alerts and
  flagged transactions); `POST /v1/admin/x402/clear-alerts` takes `?network=solana|base` (both by default) and
  answers `base_cleared` and `x402_base` next to the existing fields.

## Changes (x402 on Base: discovery; backwards-compatible)

- `/.well-known/x402` gains `networks`: one entry per network `/v1/check` takes, `{network, network_name, assets,
  feePayer, paymentFlow}`; Solana's first (`paymentFlow: "upfront"`), then, while Base is on, Base's with `assets.USDC`
  `{address, decimals, eip712: {name, version}, assetTransferMethod: "eip3009", pricing}` and `payTo`. The top-level
  `network`, `network_name` and `assets` still describe Solana, unchanged.
- A 402 that offers Base adds `extensions.bazaar` (`info`: the request body and an example verdict; `schema`: JSON
  Schema 2020-12 for `info`) and the resource's `tags` and `iconUrl` (x402's Bazaar discovery). A 402 without a Base
  option is unchanged. Unfret sends its own `resource` and `extensions` to the facilitator with each Base payment.
- `/llms.txt` gains an "On Base" section while Base is on. `/developers` names both networks then.
- `/v1/check` errors: `GATEWAY_TIMEOUT` and `INTERNAL_ERROR` are "refunded" on Solana and "not charged" on Base;
  `SETTLEMENT_FAILED` carries `reason` on Base. The table in `/llms.txt` says so.

## Changes (a cached result costs a check; backwards-compatible)

- A deep result served from the 1-hour result cache (`POST /v1/analyze`, `/v1/analyze/stream`) now **uses a check**
  like a fresh analysis: one bought check, or a free check on a pass with free checks (and from the day's free
  budget). The response is `charged: true` (and `free_check: true` for a free one), with the balance after it;
  the stream still sends only the `result` event, at once.
- Without a check, a cached result isn't served: the account gets the same `402 INSUFFICIENT_CREDITS` or `429
  FREE_CHECKS_PAUSED` as for a page nobody checked, so it can't tell whether a page was checked recently.
- **Exception:** the account (pass or wallet) that already paid for that listing within the hour gets it again
  free, `charged: false` (a reopened result, a page refresh, the extension rendering it again, a stream repeated after
  the connection dropped). The listing is the URL without tracking parameters or fragment, as for the cache. The
  server notes this in memory only, as a keyed hash of the account and the listing, for as long as the result lives.
- Unchanged: quick checks (`"tier": "quick"`) are free, cached or not; a page that couldn't be assessed (score -1) is
  never charged, cached or not (a cached one still needs a check to be available); pages that can't be read are free
  422s; shared results (`/r/…`) are public. x402 `POST /v1/check` was already paid on every answer, the known-listing
  fast path (`cached: true`) included: the payment settles before the answer.
- Clients: show "No check was used" only when `charged` is false, as before; nothing else changes.

## Changes (packs of 25 and 100, gift codes once a month; backwards-compatible; migration 28)

- **Packs:** 25 and 100 checks (`UNFRET_PACKS`); the 50-pack is no longer sold. `GET /v1/pricing` lists the packs on
  sale, as before, with the same fields: per pack `price_usd` $6.00 / $20.00 (by card, before tax; now set by hand,
  `UNFRET_CARD_PRICES_USD`), `price_eur` €5.50 / €17.50, `crypto_usd` $5.00 / $20.00 (the pack's value, before VAT;
  never more than the card price), `credited_usd_micros` the pack's value. Clients must take the sizes from
  `/v1/pricing`, never assume them.
- `POST /v1/credits/purchase-tx`, `GET /v1/credits/solana-pay.svg` and `POST /v1/credits/fiat-checkout` with
  `checks: 50` answer `400 BAD_REQUEST` "Not a pack size." (`metadata.packs: [25, 100]`), as for any size not on sale.
  Passes that bought a 50-pack keep their checks, and its gift codes stay redeemable; a crypto quote for 50 made
  before the change still credits when paid.
- **Gift codes** (invites): one per 25 checks, as before (25-pack → 1, 100-pack → 4), now worth **5 checks** each
  (`UNFRET_INVITE_CHECKS`). Each pack's `invites` says so (`{"count": 4, "checks_each": 5, "checks": 20}`) and its
  `label` ends "· with 1 gift code for a friend (5 checks)" / "· with 4 gift codes for friends (5 checks each)". The
  top-level `invites` adds `redeem_per_month: 1`, and its `for` now reads "any account but the buyer's: a wallet or an
  API key, new or in use" (true since gift codes work on any account).
- `POST /v1/invites/redeem`: **one gift code a calendar month (UTC)** per pass or wallet. A second one in the same
  month answers `429 INVITE_MONTHLY_LIMIT` ("You've already used a gift code this month; this one still works next
  month, or give it to someone else.", `metadata.next_month`: the date it works again, e.g. `"2026-12-01"`) and the
  code stays unused, for this account next month or for someone else now. Show the `detail` as it is. The server keeps
  only the month of the account's latest gift code (`wallets.gift_month`, migration 28), never which code.
- `/checkout?checks=50` (an old link) selects the nearest pack on sale (25) and says the 50-pack isn't sold any more.

## Changes (the scorers in health, backwards-compatible)

- `GET /v1/health` and `GET /v1/health/ready` add `model_version` (the scorer analyses and deep checks run) and
  `quick_model_version` (the x402 quick tier's), the same strings as `assessment.model_version` and the x402
  verdict's `model_version`, or `null` without a scorer. A server runs the open model in the repository unless its
  operator configured another (`UNFRET_MODEL_PATH`, `UNFRET_MODEL_QUICK_PATH`).

## Changes (an Unfret pass pays `/v1/check`; developer passes; backwards-compatible)

Additive: x402 clients see the same requests, responses, headers and status codes as before.

- `POST /v1/check` takes `Authorization: Bearer ufk_…` (an Unfret pass) instead of a payment. The body and the verdict
  are the same; the pass's response adds `charged` (bool), `free_check` (bool), `balance_usd_micros` and
  `free_checks_left`, and carries no `PAYMENT-RESPONSE`. Charged like `POST /v1/analyze`: a deep check uses one check
  (the pass's free checks first, then its balance); a quick check is free (at most `quick_checks_per_minute` a minute
  per pass, shared with `/v1/analyze`); a cached answer (the same input within the hour, or the listing's fast-path
  answer of the last day) costs a deep check like a fresh one, except for the pass that paid for that listing within
  the hour. A page that can't be fetched (422), assessed (`assessed: false`, `charged: false`) or analysed
  (500/504 `INTERNAL_ERROR`/`GATEWAY_TIMEOUT`) is given back.
- New `/v1/check` errors, only with `Authorization`: **401 `INVALID_API_KEY`** (not a valid pass; a wallet's session
  token doesn't pay `/v1/check`), **402 `INSUFFICIENT_CREDITS`** (no checks left; `metadata` as for `/v1/analyze`, no
  `PAYMENT-REQUIRED` header), **429 `FREE_CHECKS_PAUSED`** (only free checks left and today's budget is spent), 429
  `TOO_MANY_REQUESTS` (per-pass limits of `/v1/analyze`). A request with `PAYMENT-SIGNATURE` is always an x402
  payment (`Authorization` is then ignored); without either header the answer is the x402 `402` as before.
- `POST /v1/keys` takes an optional `"developer": true`: the new pass comes with `developer_free_checks` (20) free deep
  checks instead of `free_checks_per_pass`, within `developer_passes_per_ip_per_day` (1) per visitor network (in
  memory, salted hashes, never stored); past that it is made empty. Same proof of work and per-network key limits,
  the same daily budget for free deep checks. `free_checks` in the response says how many it got.

## Changes (the remote MCP server at `/mcp`; `/v1/check`'s limit per pass; backwards-compatible)

- **`/mcp`**: an MCP server over streamable HTTP (stateless, JSON responses; protocol 2026-07-28 and the earlier
  initialize handshake), for hosted MCP clients. `Authorization: Bearer ufk_…` (an Unfret pass) is required: otherwise
  **401** with `WWW-Authenticate: Bearer realm="Unfret"` (plus `error="invalid_token"` for a pass that isn't valid)
  and `{"detail", "error_code": "UNAUTHORIZED" | "INVALID_API_KEY"}`; **429** `RATE_LIMITED` (with `Retry-After`)
  after too many bad passes from one network in a minute; 503 while the service isn't up. Tools:
  `check_store_quick` and `check_store_deep` (`url`, `policy`, `page_content`; run `POST /v1/check` with the pass,
  tier quick (free, read-only) or deep (one check; not read-only, not idempotent)), `get_checks_left`
  (`GET /v1/credits`), `get_pricing` (`GET /v1/pricing`); no other route is reachable from it. It never pays from a
  wallet.
- `POST /v1/check` with a pass: its per-minute limit (`x402_check_per_minute`, 30) now counts per pass rather than
  per visitor network, so callers behind one host share nothing; requests with a pass that doesn't authenticate count
  per network, and past the limit get 429 `RATE_LIMITED` without a lookup. x402 requests are limited per network as
  before.
- CORS allows the `Mcp-Protocol-Version`, `Mcp-Session-Id`, `Mcp-Method`, `Mcp-Name` and `Last-Event-ID` request
  headers and exposes `Mcp-Session-Id` and `WWW-Authenticate`.

## Changes (the result card; the same product sold elsewhere; backwards-compatible)

`POST /v1/analyze`, the stream's `result` event (and so `/check`), and shared results add the **result card** to
`verdict`. All fields are new and optional: a cached result, a share made earlier or an older server has none of them,
and clients show what is there.

- `reasons[].source` and `positives[].source` (string): where the evidence comes from, in words, e.g. "Domain registry
  (RDAP)", "EU VAT register (VIES)", "Unfret's price history", "Unfret's image index", "The page", "The page's code",
  "The shop's terms and legal pages", "All signals together (Unfret's model)". Show it under the finding ("Source: …").
- `checklist`: `[{"key", "label", "status": "checked"|"concern"|"not_checked", "note"}]`, what was checked, area by
  area: for shops (storefronts and marketplace listings) `who` "Who's behind the shop", `product` "The product and the
  brand", `reviews` "Reviews", `prices` "Prices, delivery and returns", `tricks` "Hidden tricks"; for ads `seller`,
  `price`, `photos`, `payment`, `text`; for service providers `company`, `location`, `reviews`, `terms`, `photos`.
  `concern` when a finding is about the area (also one beyond the three shown; `note` names the strongest),
  `not_checked` with `note` saying why ("The page shows no reviews.", "Not part of a quick check.", "Couldn't be
  checked this time.", "Doesn't apply to this page.", "Nothing on the page to check."). An area nothing looks at on
  such a page is left out. New keys may be added; show `label`.
- `checks_run` (int): how many separate checks ran on the page and had something to check. A clean result (`reliable`
  without reasons) says so: "Nothing worrying in 14 checks."
- `next_step`: `{"type", "text"}`, one suggested next step, from the strongest finding (`type` names it, e.g.
  `shop_reviews`, `compare_price`, `check_basket`) or the band (`type` = the band, `ad_<band>` or `service_<band>`).
- `same_product` (shops and marketplace listings only; `null` otherwise): the product photo is the same photo as on
  another shop or listing in Unfret's image index. `{"title": "Looks like the same product, sold elsewhere[ for
  $X]", "detail", "where": the other shop's domain, "Amazon", or "an Amazon catalogue listing (brand: …)", "domain":
  the other shop's registrable domain or null, "others": how many more shops show it, "price", "currency",
  "price_text", "price_seen_on": the other listing's price and the day Unfret saw it (only when Unfret's price
  history has that listing: a shop's listing with a public product ID such as a GTIN, seen in the last 90 days;
  otherwise absent), "image": the other photo's thumbnail (`/v1/images/{id}/thumbnail.jpg`), "own_image": this page's
  photo's thumbnail (null when the index doesn't keep it, e.g. the test shop)}`. Show it **first**, with both photos.
  Nothing else of the other listing, and nothing of the public catalogue seed but the brand it names, is in the
  response. Load the photos only from the API's own `/v1/images/` (as reason thumbnails).
- Shared results (`/v1/shares/preview`, `GET /v1/shares/{id}`, `/r/{id}`) carry the same fields, in public wording; a
  `same_product` with a word a public result never uses is left out whole. A share token issued for a result without
  them still matches it.
- Clients: the card's links are "Wrong result?" (the report flow, `POST /v1/reports`, for a result with
  `report_token`) and the shop owner's objection (`/objection`). `/r/{id}` links "Wrong result? Check the page again"
  to `/check?url=…` and "Object to this result" to its own form.
- x402 `POST /v1/check` and the MCP server are unchanged.

## Changes (x402 directories can find `/v1/check`; backwards-compatible)

- The OpenAPI document (snapshot updated) marks `POST /v1/check` as paid with `x-payment-info`:
  `{"price": {"mode": "dynamic", "currency": "USD", "min": "0.01", "max": "0.2"}, "protocols": [{"x402": {}}]}`, the
  tiers' price range from the same settings as `/.well-known/x402`. x402scan and other directories that follow the
  agentcash discovery spec register paid calls from it; the 402 still has the exact amounts.
- The 402's `resource.description` now says what the check is for (a store, marketplace seller or product page,
  before an agent buys). Clients don't parse it.

## Changes (ownership proof for x402 directories; backwards-compatible)

- While x402 is on, the OpenAPI document has top-level `x-agentcash-provenance: {"ownershipProofs": [...]}` and
  `x-discovery: {"ownershipProofs": [...]}`, and `/.well-known/x402` a top-level `ownershipProofs` (its other fields
  are unchanged). Each proof is the public origin (`https://unfret.net`, no trailing slash) signed by a `payTo` key:
  for Solana the hot wallet's ed25519 signature over the origin's UTF-8 bytes, base58. x402scan verifies it against
  the `payTo` of the 402's options. With x402 off the fields are absent (the OpenAPI snapshot, taken with x402 off,
  is unchanged).

## Changes (x402 on Base: a hot wallet swept to cold; behind `UNFRET_X402_BASE_ENABLED`, still off)

- Base's treasury now matches Solana's: `payTo` in the Base option of the 402 and in `/.well-known/x402` `networks` is
  the Base hot wallet (the address of `UNFRET_X402_BASE_HOT_KEY`), which the server sweeps, as USDC, to the cold
  address (`UNFRET_BASE_COLD_ADDRESS`). Clients pay exactly as before: whatever `payTo` the 402 names. With Base off
  nothing changes, the 402 included.
- `GET /v1/health/ready` with the operator token: `base_pay_to_usdc_units` becomes `base_hot_usdc_units`, and
  `base_hot_eth_wei` (the hot wallet's ETH for the sweeps' gas) is new (snapshot updated). `x402_base_alerts` drops
  `base_sweep_due` and gains `fee_reserve_low`, `usdc_above_threshold` and `sweep_failing` (as Solana's). The public
  fields are unchanged.
- `GET /v1/admin/x402/alerts`: `base` gains `cold` and `eth_wei`; a flag's `signature` is a transaction hash, or the
  nonces (`nonce 3-4`) or blocks a flag covers.
- With Base on, `ownershipProofs` (above) gains a second proof: the origin signed by the Base hot wallet, EIP-191
  (`personal_sign`), `0x`-prefixed hex, after Solana's. x402scan recovers the signer and compares it with Base's
  `payTo`.

## Changes (a quick result can be reported; backwards-compatible)

- A `"tier": "quick"` result of `POST /v1/analyze` and `/v1/analyze/stream` now carries a `report_token` too, for an
  account (a pass or a wallet's session); a quick check without `Authorization` still gets none, since reports are
  per account (the monthly limit, the reward). It never carries a `share_token`.
- `POST /v1/reports` is unchanged: send back the analysed request exactly as sent, `"tier": "quick"` included. The
  token binds the tier, so a quick result can't be reported as a deep one (400 `BAD_REPORT_TOKEN`); deep tokens are
  as before. The same limits apply (5 per account per month, one per page, consent, scrubbing); the queued item
  records the tier.

## Changes (a quick result says whether a deep check is worth it; backwards-compatible)

Quick results (`"tier": "quick"` on `POST /v1/analyze`, its stream's `result` event, and `POST /v1/check`, with x402
or a pass, so also the MCP tools' `verdict`) add an optional **`deep_check`**: on `/v1/analyze` inside `verdict`, on
`/v1/check` at the top level of the verdict. It is worked out from the quick result alone and never changes it: the
rest of a quick answer is byte for byte what it was. Deep results, pages that couldn't be assessed, cached results from
before this change and older servers have no `deep_check` (or `null`); clients show nothing then.

- `{"suggested": bool, "reason", "quiet": bool, "adds": [{"key", "area", "label"}], "text", "cost", "action",
  "action_label"}`.
- **When**: `concerns` ("Worth a look") or `careful` ("Think twice"): suggested, `reason: "on_the_fence"`. `reliable`
  ("Looks fine"): suggested with `quiet: true`, `reason: "unknown_seller"` (a quick check can't see a weeks-old domain
  or a supplier's photo), except on a big marketplace's own listing: a page the quick check classifies as
  `aggregate_listing` (Amazon, bol.com, Walmart) that the listing skill doesn't call a reseller's (the seller is the
  brand, the marketplace's retail arm, or not named): not suggested, `reason: "known_seller"`. Whether a web shop is a
  known brand's own takes outside evidence (Wikidata, the AI read) that only a deep check looks up, so a web shop that
  looks fine gets the quiet suggestion. `junk_or_scam` ("Serious concerns"): never (`reason: "serious_concerns"`; the
  advice is already to buy elsewhere). A page where a deep check would add nothing: `reason: "nothing_to_add"`.
- **`adds`** (at most 2): what a deep check would look at on this page that the quick one left out, an area with a
  concern first: `shop_age` "how old this shop is" (web shops), `photo_elsewhere` "whether its product photo appears
  on other shops" (a photo on the page), `reviews` "whether its reviews look genuine" (reviews on the page), `brand`
  "whether the brand is known beyond this page" (a brand named), `price_history` "whether its 'was' price was ever
  really charged" (a struck-through price), `vat_register` "whether its VAT number is in the EU register" (a VAT
  number shown). An ad or a provider's profile on a known platform (which a quick check reads as a shop's page, and a
  deep check, with the page classes beyond products on, as what it is): `seller_profile`, `ad_photos`, `ad_price`;
  `provider_profile`, `service_terms`. `area` is the `checklist` key it belongs to. New keys may be added; show `label`.
- **`text`**: "A deep check also looks at how old this shop is and whether its product photo appears on other
  shops." (empty when not suggested).
- **`cost`**, `action` (`run_deep` | `get_checks`) and `action_label` ("Run a deep check" | "Get checks"), only when
  suggested, for this caller (a cached quick result is priced per request): a pass with free checks (today's not all
  used) "Uses 1 of your N free deep checks." / "Uses your last free deep check."; bought checks "Uses 1 check (you
  have N)."; neither "Deep checks come in packs, from $6 for 25." (the smallest pack's card price, from the settings),
  or, with only free checks while today's are all used, "Free deep checks are back at 00:00 UTC. Deep checks also
  come in packs, from $6 for 25." (`get_checks`). `/v1/check` paid with x402: "A deep check of this page costs $0.20."
  (the deep tier's price). Running it is a normal deep check of the same page, charged as always.
- Clients show it once, under the quick result, with one button (`action_label`): `run_deep` runs a deep check of the
  same page, `get_checks` opens the checkout. Quietly when `quiet`. No timers, no colour of its own.
- OpenAPI snapshot updated (`DeepCheck`, `DeepCheckAdd`).

## Changes (reports: rewards in two steps, 15 a month, a second opinion, a queue by value; backwards-compatible; migration 30)

- Limit: `reports_per_month` is **15** per account (pass, wallet or API key), up from 5; still one report per page per
  account. `POST /v1/reports` answers as before (`reports_per_month: 15`; 429 `REPORT_LIMIT` past it).
- Rewards, to the first reporter only and only after a person labelled the page: **5 checks** when the score was at
  least 30 points outside the label's zone (legit ≥ 70, junk and scam ≤ 40), **1 check** when it was 10–29 points
  outside it, nothing for a correct result. `GET /v1/pricing` → `"reports"` adds `"reward_gap": 30`,
  `"small_reward_checks": 1` and `"small_reward_gap": 10` next to `"per_month"` and `"reward_checks"` (now 5).
  Settings: `UNFRET_REPORT_REWARD_CHECKS`, `UNFRET_REPORT_REWARD_GAP`, `UNFRET_REPORT_SMALL_REWARD_CHECKS`,
  `UNFRET_REPORT_TRAINING_GAP` (the small reward's gap, also what counts as a training sample).
- A full queue: `report_queue_cap` is 1000 (was 200). Each open report has a value (how far the reporter says the
  score is off, +15 for a page and +15 for a shop the labelled data doesn't have yet, +10 per confirmation by another
  account, up to three). With the queue full, a new report takes the place of the open report worth least if it is
  worth more (that one is deleted, as if expired); otherwise it is refused with 503 `QUEUE_FULL` as before.
- A second opinion: each queued report is read once by a language model (`UNFRET_REPORT_SECOND_OPINION_MODEL`,
  default `qwen3.5-397b-a17b` on the same EU provider; empty: off), at most `UNFRET_REPORT_SECOND_OPINIONS_PER_DAY`
  (200) calls per UTC day, in the background. It proposes `label` (`legit`|`junk`|`scam`|`unsure`), `confidence`
  (0–1) and a one-line `reason` (the corpus labeller's field names). It sees only the stored, scrubbed report (facts,
  text, score, explanation and verdict, the reporter's reason), with everything from the page as delimited data. A
  proposal is never a label: it trains nothing and pays nothing until the operator confirms it. A failed call leaves
  the report for a person; one over the daily cap waits for a later day.
- Operator: `GET /v1/admin/reports` takes `order` (`value`, the default, or `confidence`), `agreement` (`reporter`,
  `score`, `neither`, `unsure`, `none`), `min_confidence` and `limit` (up to 1000), and returns `cap`,
  `second_opinions` (count per state) and per item `value` and `second_opinion: {"state", "label", "confidence",
  "reason", "model", "agreement", "gap", "reward_checks"}` (what confirming the proposal would mean). New
  `POST /v1/admin/reports/confirm` `{"min_confidence", "items": [{"id", "label"}]}` labels the previewed items that
  still carry exactly that proposal (per item `labelled`, `changed` or `gone`, with `gap`, `reward_checks`,
  `reward_usd_micros`, `training`); the label endpoint's answer adds `reward_checks`. `make reports` uses both: a
  preview of the confident proposals to confirm at once, then the rest one by one.
- Clients: the report consent says an AI model may suggest a label and a person decides; the website's dialog shows
  both rewards from `/v1/pricing`. The extension's own copy says "3 free checks": it should read the numbers from
  `/v1/pricing` (`reports.reward_checks`, `reports.small_reward_checks`).

## Changes (`/v1/check`: the 402 before body validation; backwards-compatible)

- A `POST /v1/check` without `PAYMENT-SIGNATURE` and without `Authorization: Bearer …` now always answers the x402
  `402` with `PAYMENT-REQUIRED`, whatever the body: none, empty, not JSON, or not a valid check request (it was a
  `422 VALIDATION_ERROR`). x402 directories (x402scan, x402-list.com) probe without a body first and read the price
  from that 402. The tier priced is the body's `tier` when it is `quick` or `deep`, else `quick` (the default), so
  such a 402 is byte for byte the one a valid request for that tier gets, `resource` and Base's `extensions.bazaar`
  included. The 402 of a valid request is unchanged.
- With a payment or a pass the body is validated first, as before (`422 VALIDATION_ERROR`); nothing is verified,
  settled or charged for an invalid body.
- The OpenAPI document (snapshot updated) has `info.contact.email` (`hello@unfret.net`) and a top-level
  `x-agentcash-guidance: {"llmsTxtUrl": "<base URL>/llms.txt"}`, which x402scan reads; `POST /v1/check`'s description
  says that the 402 comes first. Request and response schemas, and the ownership proofs (`x-agentcash-provenance`,
  `x-discovery`), are unchanged.

## Changes (a quick check reads the same app traces from the extension's facts; backwards-compatible)

- `PageFacts` gains `markup_names` (up to 500: class names, ids and data-attribute names of the page's elements;
  names only, identifier-like, without a run of four digits; inside a form, account menu or checkout area only that
  area's own element) and `external_refs` (up to 200: the other sites the elements refer to, a link's host and path,
  any other element's host; never a query or fragment; none on a private seller's ad). Both default to empty, so
  older clients are unaffected (snapshot updated; the extension mirrors the extraction in `src/extract/markup.ts`).
- `html_source_signals` matches them with the script URLs and meta tags, so the dropshipping, review-import,
  supplier and pop-up traces in the markup reach a quick (and deep) check of a page the extension sends, as they do
  for a page the server fetched. The test shop's quick check gives the same features and verdict both ways.
- A scorer flag's `evidence` is the finding of the feature's own type first (`html_source_signals.review_import_app`
  quoted the dropshipping app's finding before, as both types end in "app").

## Changes (the same photo under other brand names, backwards-compatible; migration 31)

- `verdict.same_product.brands` (new, at most three strings, empty when unknown): other brand names the same photo
  is sold under on the shops named there, names only (`detail` says it in words too: "…, under another brand name
  (VELUNOX)."). Snapshot updated.
- Two new reason types on product pages, source "Unfret's image index", checklist area `product`:
  `rebranded_photo` (the same photo is sold under other brand names: white-label goods, relabelled; the band is at
  best "Worth a look") and `brand_photo_reused` (a known brand's catalogue photo on a page naming another brand; the
  band is at best "Think twice", next step `real_brand`). Neither makes a page "Serious concerns" by itself. When
  `same_product` is shown their `thumbnails` are empty (the block has the photos).
- New findings `photo_other_brands` and `brand_photo_reused` (skill `reverse_image`), and features
  `reverse_image.other_brands` and `reverse_image.brand_photo_reused`.
- `photo_reused` no longer fires for a brand that is on Wikidata (as `copied_reviews_elsewhere`).

## Changes (Unfret from any language: `/developers/http`; only the Python client is published)

- New page `GET /developers/http`, "Use Unfret from any language": the two ways to pay (a pass as
  `Authorization: Bearer ufk_…`, or x402), a developer pass in code (`GET /v1/keys/challenge`, the proof of work:
  SHA-256 of the UTF-8 bytes of `challenge + ":" + solution` with `bits` leading zero bits, then `POST /v1/keys` with
  `"developer": true`), `POST /v1/check`'s request and the response fields a client typically reads, the clients'
  band → action policy, errors, retries and timeouts, and examples in curl, JavaScript, Go and PHP. Numbers come from
  the settings and `developers-measured.json`; the JavaScript examples are run in the tests against the server's own
  proof-of-work check and models. Linked from `/developers` and the footer.
- `/llms.txt` gains a "Clients" section: Python (`pip install unfret-client`; from the source until its first release
  on PyPI) and `/developers/http` for every other language.
- The TypeScript client (`clients/typescript`) is a reference to copy, not an npm package (`"private": true`); the
  "Publish clients" workflow publishes only the Python client. No API change; the OpenAPI document is unchanged.

## Changes (the Android app's download page; backwards-compatible)

- Page: `GET /android` ("Unfret for Android"): what the app does, the signed release APK's download, its version,
  size, SHA-256 and the signing certificate's SHA-256 fingerprint, install steps (and turning "Install unknown apps"
  off again), updates, the app's permissions and privacy. Not linked from the home page, menu or footer yet, and
  `noindex` (`X-Robots-Tag` and a meta tag); the usual page policy otherwise.
- The download links to the release asset on git.kroshtan.com
  (`/kroshtan/unfret_android/releases/download/v<version>/unfret-<version>.apk`), an address built from the version.
  The facts come from `static/android-release.json`, which `make android-release TAG=v<version>` writes from the
  release's `release.json` after checking the APK's size and SHA-256; a release is offered only when they are complete
  and its certificate is Unfret's release key's (`unfret.android_release.SIGNING_CERT_SHA256`). Without that file the
  page says the download is coming soon and offers no file.
- A beta: a version with a pre-release suffix (`0.1.0-beta.1`, tag `v0.1.0-beta.1`; `make android-release
  TAG=v0.1.0-beta.1`) whose `release.json` says `"prerelease": true`. The page labels it "Beta" next to the version
  and says, in one sentence, that it is an early version that may still change and to write to hello@unfret.net
  about what doesn't work; the install steps and the certificate check are the same. The flag must match the version
  (a beta that doesn't say so, or a plain version marked as one, is not offered); a `release.json` without it is a
  stable release.

## Changes (developer passes' free checks are for the API; old named codes gone; migrations 33 and 34)

- **Developer passes**: `POST /v1/keys` with `"developer": true` stores a flag on the pass (`api_keys.developer`,
  migration 34; nothing about who asked). Its free deep checks count on **`POST /v1/check` only** (the API, the MCP
  servers, the clients). On `POST /v1/analyze` and `/v1/analyze/stream` (the website, the browser extension, the
  Android app) a developer pass is a pass without free checks: a deep check is paid from its bought balance, and
  without one it gets exactly the `402 INSUFFICIENT_CREDITS` an empty pass gets. Bought checks on it work everywhere.
  Its free checks don't move to another pass with `POST /v1/keys/move-free-checks`, and none move onto it
  (`{"moved": 0}`). Passes made before migration 34 have the flag off and work as before.
- `POST /v1/keys` and `POST /v1/keys/recover` add `developer` (bool).
- `GET /v1/credits` adds `api_free_checks_remaining` (int): a developer pass's free checks, which are **not** in
  `checks_remaining` or `free_checks_remaining` (those count what works everywhere, so the website and the extension
  never offer checks they can't spend); 0 for any other pass and for a wallet. `free_checks_paused` covers them too.
  The remote MCP server's `get_checks_left` counts them in, as `/v1/check` does.
- `/.well-known/x402` adds `auth.developer_pass.free_checks_count_on`; `/llms.txt`, `/developers` and
  `/developers/http` say where a developer pass's free checks work.
- **Named codes** (`FRIENDS` and the like, retired in 0.13.0) are gone: `POST /v1/invites/redeem` answers any code that
  isn't a gift code (`ufi_…`) with `400 INVALID_INVITE` ("That isn't a gift code."), whatever it once was;
  `INVITE_ENDED`, `INTRO_DAILY_CAP` and `INVITE_NOT_FOR_THIS_ACCOUNT` are no longer sent. Migration 33 drops their
  table and day counter. `/key` (with or without `?code=`) redirects to `/checkout?for=key`; `/checkout?code=` with
  anything but a gift code drops it from the address bar and opens as if the link had none. The operator endpoints
  `/v1/admin/codes` and `make codes` are removed, and `UNFRET_INTRO_CODES_ENABLED` and
  `UNFRET_INTRO_DAILY_CHECKS_CAP` are no longer read. `GET /v1/admin/invites` adds `via_code`, `via_code_bought` and
  `organic_bought` (the buyer counts the codes list had). Gift codes, their monthly limit, the buyer's own pack refusal
  and the claim token are unchanged, as are affiliates and bonus codes.

## Changes (the brand from a marketplace byline; a seller named like the brand; backwards-compatible)

- `PageFacts.brand` is the brand alone: a marketplace byline's phrase is taken off ("Visit the X Store", "De X Store
  openen", "Besuche den X-Store", "Visiter la boutique X", "Marca: X", in en, nl, de, fr, it, es, pt, pl, se, tr and
  ja), as is a store word hyphened on ("X-Store", "X-winkel"). On Amazon the product's structured data comes first
  (JSON-LD, the overview table's and the detail table's brand row, the byline link's brand filter), the byline's text
  after it. A name that reads like a sentence or a link to a store (more than five words, a "visit" or "open" word,
  a store word before its last word) is no brand: `brand` is `""`. The server applies the same rule to a client's
  `page_facts.brand` before the analysis, so an older client that sends the byline phrase gets the same result.
- A seller is the brand when the brand's name appears in the seller's as whole words (case, spaces, hyphens and
  accents ignored, "AnkerDirect" and "Airmax Official" included); `listing.seller_not_brand` follows that rule.
- New shopper reason key `seller_lookalike` (medium; source `page`) on a marketplace listing: the seller's whole name
  (without "Official Store", a company form or a suffix such as "Direct") is a slip away from a brand known elsewhere
  (Wikidata, or its own domain for five years or more) without being it: a letter added, dropped or swapped with its
  neighbour, a digit or a lookalike character for a letter, or for a name of more than six letters two edits of any
  kind, with the same first letter ("Nikee", "N1ke", "Nike-e" for Nike; not "Boss" for Bose). It replaces `seller_not_brand` for that listing. The
  listing skill's finding of the same name carries the evidence; there is no new feature.
- `known_brand` and `brand_no_presence` are never on one result: when the content read knows the brand but the
  lookups find it nowhere, neither is said.

## Changes (the card: a step that didn't finish, checks per area, what a check cost; backwards-compatible)

- `verdict.incomplete` (new, optional; `null` when every key step finished): `{"steps": ["content"], "text"}` when
  the AI read of the page failed or ran out of time, the same condition as `assessment.degraded`. `text` is one line
  to show under the headline: "Part of this check didn't finish (the AI read of the page), so this answer may be
  milder than it should be. It still counted as a check. Checking again after an hour may help." Shared results carry
  it with the first sentence only. New step keys may be added; show `text`.
- `verdict.checklist[].checks` (new, optional int): how many checks looked at the area. A check serving two areas
  counts in the first, so the areas' counts add up to `checks_run`; an area the page doesn't have (a marketplace
  listing's "Who's behind the shop") no longer adds its checks to `checks_run`. Clients show "What we checked: 16
  checks in 5 areas" (areas with a check) and "· N checks" on each area; without the field, as before.
- `charge_note` (new, optional string) in `POST /v1/analyze` responses and the stream's `result` event: what the
  check cost, as one sentence ("This check used one of your free checks; 4 are left.", "This check used one of your
  checks; 23 are left.", "Quick checks are free.", "You checked this page recently, so no new check was used.", "This
  page couldn't be assessed, so no check was used."). The count is left out when it isn't known. Charging is
  unchanged.
- Each LLM call's attempt is abandoned after `UNFRET_LLM_ATTEMPT_TIMEOUT_S` (15 s; the content read
  `UNFRET_CONTENT_ATTEMPT_TIMEOUT_S`, 20 s) and tried once more. No API change.
- x402 `POST /v1/check` and the MCP server are unchanged.

## Changes (the pass's phone link opens the Android app; backwards-compatible)

- `GET /v1/keys/phone-qr`'s QR code now holds `<public_base_url>/pass#pass=<key>` (production
  `https://unfret.net/pass#pass=ufk_…`, dev `https://dev.unfret.net/pass#pass=ufk_…`) instead of
  `<site>/check#pass=<key>`. The fragment is unchanged: exactly one parameter, `pass`, whose value is the whole pass
  `ufk_<id>_<secret>` (`^ufk_[a-z2-7]{10}_[a-z2-7]{52}$`, `PASS_RE` in `static/pass.js`; only these characters, so
  nothing in it is percent-encoded). Read it as URL-encoded parameters (`/check` uses
  `new URLSearchParams(location.hash.slice(1)).get("pass")`) and ignore any other parameter; a value that doesn't
  match is no pass.
- **The Android app** (`net.unfret.android`) declares `https://unfret.net/pass` as a verified App Link
  (`android:autoVerify="true"`, scheme `https`, host `unfret.net`, path `/pass`). With it installed and verified,
  Android opens the scanned link in the app, which never sends the fragment anywhere. **The app must ask "Use this
  pass?" before storing it**, showing the pass ID (`ABCDE-FGHIJ`) and saying so when it already has another pass,
  exactly as `/check` does: any site can link to `/pass`, so a pass that arrives by link is never kept unasked. It
  may check the pass with `GET /v1/credits` (`Authorization: Bearer <key>`) first, and must not log it.
- `GET /.well-known/assetlinks.json` → 200, `Content-Type: application/json`, no redirect, `Cache-Control:
  no-cache`:

  ```json
  [{"relation": ["delegate_permission/common.handle_all_urls"],
    "target": {"namespace": "android_app", "package_name": "net.unfret.android",
               "sha256_cert_fingerprints": ["3E:71:EF:6B:76:FA:BA:49:41:97:22:AE:99:8A:42:4B:38:A7:E9:05:C6:A4:48:4A:37:F8:A9:6F:E4:5F:66:34"]}}]
  ```

  The package and certificate are constants in `src/unfret/android_app_links.py` (the certificate is
  `unfret.android_release.SIGNING_CERT_SHA256`, the app's `docs/release-cert.sha256`). Dev serves the same file:
  the app has no dev package, and a debug build (same package, a throwaway key per checkout) is not listed, so its
  link is approved by hand (`adb shell pm set-app-links --package net.unfret.android 2 all`) when testing.
- `GET /pass` (without the app, on an iPhone or a computer): a tiny page, `X-Robots-Tag: noindex, nofollow` and a
  robots meta tag, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, the same Content Security Policy as
  `/check`. Its script (`static/pass-link.js`) does `location.replace("/check" + location.hash)`, so
  `/check#pass=<key>` behaves exactly as before and `/pass` stays out of the history. Without a script the page says
  what the link is for and links to `/check`. Not in the sitemap.
- `/check#pass=<key>` keeps working, for QR codes shown or printed before this change.

## Changes (developer passes: their own daily budget and a harder proof of work; backwards-incompatible for making one)

- **A developer challenge**: `GET /v1/keys/challenge?developer=true` answers a challenge `UNFRET_DEVELOPER_POW_EXTRA_BITS`
  (4) bits harder than an ordinary one (`bits` says how hard, as before; the difficulty is under the challenge's HMAC),
  and the response adds `developer` (bool). `POST /v1/keys` with `"developer": true` accepts only such a challenge:
  an ordinary one answers `400 BAD_CHALLENGE` ("… A developer pass needs a developer challenge: GET
  /v1/keys/challenge?developer=true.") and stays usable for an ordinary pass. Ordinary passes keep their difficulty.
  **Breaking** for clients that make developer passes with the ordinary challenge: `unfret-client` 0.1.0's
  `new_developer_pass()` is refused; 0.1.1 asks for the developer challenge. `/developers` and the TypeScript
  reference client are updated.
- **Their own daily budget**: a developer pass's free deep checks count towards `usage_counters` `developer_free_checks`
  (a number per UTC day), capped at `UNFRET_DEVELOPER_FREE_DAILY_BUDGET_EUR` (1.00) over `UNFRET_FREE_CHECK_COST_EUR`
  (200 checks a day by default); shoppers' free checks count towards `free_checks` as before, and neither touches the
  other. Once the developers' budget is used, a deep check on `/v1/check` that only a developer pass's free checks
  could pay for answers **`402 INSUFFICIENT_CREDITS`**, as an empty pass does, with `detail` "Today's free developer
  checks are used up; they're back at 00:00 UTC. Bought checks still work." and `metadata.retry_after_s` beside the
  usual shortfall. A free check that is given back (failed, unassessable) goes back to the budget it came from.
- `GET /v1/credits`: `free_checks_paused` is about the budget this pass's free checks come from (the developers' for
  a developer pass); `api_free_checks_remaining` is unchanged.
- `/.well-known/x402`: `auth.developer_pass.challenge` is the developer challenge's address, and it adds
  `per_network_per_day` and `pow_bits`. `/llms.txt`, `/developers` and `/developers/http` say a developer pass is
  for trying Unfret out (one per network a day), with its harder challenge and its own budget.
- The site's proof of work (`static/new-pass.js`, for `/check`, `/checkout` and `/developers`) computes SHA-256 in
  plain JavaScript instead of one Web Crypto call per hash: the same first counter, several times faster, so a
  developer pass's harder challenge takes seconds in a browser.

## Changes (the answer's small signs on the screen; progress counts checks; backwards-compatible)

- **When no single reason fired**, the verdict's one reason (type `overall`) is the scorer's small signs, counted
  and named: `title` "One small sign" / "Two small signs" at "Worth a look" ("One sign" / "Two signs" below it),
  `detail` "No single big problem, but together these lowered the score: a sizeable discount and some pushy sales
  talk.". The signs are the assessment's `flags` that raised the risk, without the ones that only describe the page
  (`kind: "context"`), each in the catalogue's words (`small_signs.signs`). Without any, `overall` reads "Nothing
  clear-cut" (it no longer says "several"). The template headline names the (up to two) strongest signs, and the
  written headline is told how many there are.
- **The checklist marks each small sign's area** (`status: "concern"`, `note`: the sign's words) on any verdict below
  "Looks fine", also next to reasons that fired: an area whose signal moved the answer off "Looks fine" is never
  `checked`. Clients need no change.
- **Stream `step` events add `check`** (bool): whether the step is one of the checks the result's `checks_run`
  counts (a skill that measured something, had something to look at and belongs to the page's checklist). Reading
  the page, classifying it and triage are steps, not checks. A client counting progress counts the `check` steps,
  so "7 checks done" while checking is the "7 checks" the result says it ran; a cache hit streams only the result.

## Changes (the claim audit, deep checks; backwards-compatible)

- A new skill on product pages (deep tier only; the quick tier is unchanged): `claim_audit`, with features
  `claim_audit.claims` (checkable claims found), `.contradicted` (0/1), `.contradictions` (how many are told, at most
  three) and `.impossible` (how many of those physics, the price or an authority rules out, as against the page merely
  contradicting itself), or `claim_audit.not_applicable` while it is off (`UNFRET_CLAIM_AUDIT=false`) and
  `claim_audit.unavailable` when it failed. Its findings have type `claim_contradicted` and evidence
  `Says "<claim, verbatim>"; <why, one plain sentence>`. Not in the scorer.
- A new shopper reason key `claim_contradicted` ("A claim that doesn't add up", severity medium, area `product`,
  source `ai_read`): `The page says “<claim>”. <why>`, with a second finding about the same claim appended (a padded
  piece count that doesn't add up either). It stands in for `implausible_claims` when both fire. On a page that no
  track record or known brand vouches for, a claim physics, the price or an authority rules out
  (`claim_audit.impossible`) makes "Looks fine" "Worth a look" at most; it never makes a warning by itself.

## Changes (an address's age from the Internet Archive where the registry gives none; backwards-compatible)

- On a web shop whose domain registry publishes no registration date (common for .nl, .de and other EU country
  domains), a deep check may ask the Internet Archive for the first copy of the shop's home page. Its date is a
  minimum for the address's age, never its registration date.
- `domain_age` adds `.archive_age_years`, `.archive_first_year` and `.age_from_archive` (the address's oldest age is
  the archive's, not the registry's or the first certificate's); `.age_years` and `.known` keep their meaning
  (registry or first certificate only). New finding `archive_first_capture`: "Online since at least <date>, per the
  Internet Archive (its first copy of <address>; a minimum, not the registration date)."
- An archive age of at least 3 years plans the `domain_history` look on a doubtful page, as a registered one does.
- A new positive `type` **`established_archive`**: title "Established shop", detail "This web shop's address has been
  online since at least <year>, per the Internet Archive, and its earlier copies show the same shop.", `source`
  "Earlier copies of the site (Internet Archive, web.archive.org)". Given only when the address has no registration
  age of 3 years, its archive age is 3 years or more, and `domain_history` compared copies with none showing another
  site, a parked page or a placeholder; never together with `established_domain`.
- `domain_repurposed` on an archive age reads "This web address is at least <n> years old, but …".
- x402 `POST /v1/check` and the MCP server: unchanged in shape.

## Changes (the product photos against the listing's own claims; backwards-compatible)

- Two new reason types on product pages (deep checks only), severity medium, source "The listing's photos (Unfret's
  AI read of them)" (new source key `photos`), checklist area `product`, next step `check_listing` (new):
  `photo_count_mismatch` ("Fewer pieces in the photo than the listing says": the listing's piece count against a
  photo of the whole set with far fewer of the main items, caps, lids and boxes not counted) and `photo_size_mismatch`
  ("Looks bigger in the photos than its stated size": the stated size against how big the product looks next to a
  person or a common object). `thumbnails` stay empty (the photos are the shop's own, not served by Unfret). Either
  makes a "Looks fine" page "Worth a look"; neither makes a page worse than that by itself. Clients that map reason
  keys should fall back to `title`/`detail`, as before.
- New skill `photo_claims` (findings of the same names as the reasons; features `photo_claims.checked`, `.photos`,
  `.count_claimed`, `.count_seen`, `.count_mismatch`, `.size_mismatch`, not in the scorer). It counts in "What we
  checked" only when the listing stated a count or size and a photo could be read.
- x402 `POST /v1/check` (deep tier) carries the reasons as flags like any other; the quick tier and the MCP tools are
  unchanged.

## Changes (a piece count that is off is "Worth a look"; backwards-compatible)

- A new feature `claim_audit.count_off`: how many of the claim audit's findings are about the piece count (padded
  with accessories, or the parts it lists not making the total). With one, "Looks fine" becomes "Worth a look",
  whatever the brand or the shop's track record, and the `claim_contradicted` reason keeps a "Worth a look" the scorer
  gave. Other self-contradictions are unchanged: a note, the band as it is.

## Changes (an older address with a new shop: the shop's earlier copies; backwards-compatible)

- A deep check of a web shop (`POST /v1/analyze`, the stream, `/v1/check`'s deep tier) may look at the shop's home
  page in the Internet Archive from about 1, 3 and 12 months ago: only when its address is at least 3 years old and
  the page already looks doubtful by the other checks. The stream may then have one more `event: step` with
  `skill` `domain_history`. A quick check never does.
- A new reason `type` **`domain_repurposed`** (`severity` `medium`, area `who`, next step `shop_reviews`): title
  "Older address, new shop", detail "This web address is 12 years old, but as recently as March 2026 it showed a
  parked page, with the address for sale. So its age says little about the shop on it now." (or "an empty placeholder
  page", or "a different site (not a shop, in Dutch, “…”)"), `source` "Earlier copies of the site (Internet Archive,
  web.archive.org)". With it, the positive `established_domain` isn't given, and the verdict is `concerns` at best.
- `reasons[].links` (new, optional, at most three strings, empty for every other reason): the archived copies the
  reason rests on, `https://web.archive.org/web/<timestamp>/<address>`. Clients may show them as links ("The earlier
  copies: March 2026, …"); clients that don't know the field show the reason as any other. Shared results carry
  them too (only `https://web.archive.org/web/` links).
- New findings in the stream's steps and the assessment's evidence: `archive_copy` (one per copy compared),
  `no_archive_copies` (the archive has none from the past year: neutral, never a reason) and `domain_repurposed`.
- Checklist: "Who's behind the shop" counts the look as a check when copies were compared; its new note key
  `no_archive` reads "The Internet Archive has no earlier copies of the shop's address." when nothing else in the
  area ran.
- x402 `POST /v1/check` and the MCP server: unchanged in shape.

## Changes (the claim audit reads the shop's other pages; backwards-compatible)

- On a web shop's own site (`storefront`, not on a marketplace platform), the deep tier's `claim_audit` also reads the
  shop's returns, shipping, about and contact pages (then imprint and terms; at most four, linked from the page on
  the shop's own domain) and holds the product page's claims against them, and the pages against each other.
  Marketplace listings and the quick tier are unchanged.
- Findings `claim_contradicted` that quote another of the shop's pages have evidence
  `Says "<claim>"; Its <returns page> says '<other side>'. <why>`, or, when the claim itself is on another page,
  `Says "<claim>"; That's on the shop's <about page>; its <returns page> says '<other side>'. <why>`. The shopper
  reason reads `The page says “<claim>”. Its returns page says '…'. <why>`. Such a contradiction is the shop
  contradicting itself: told, the band unchanged (it never counts as `claim_audit.impossible`).
- Two new features: `claim_audit.shop_pages` (how many of the shop's other pages were read) and
  `claim_audit.across_pages` (how many of the findings quote one of them). Not in the scorer.
- `page_facts.policy_links` may carry a new kind, `about` ("About us", "Our story", `/pages/about`). A client that
  doesn't send it gets the server's reading of the page's HTML for it; other kinds are taken from the client as before.
- `UNFRET_CLAIM_AUDIT_SHOP_PAGES=false` reads the product page only.

## Changes (variant options, and the variant check; backwards-compatible)

- `PageFacts` (request `page_facts`, `GET /v1/schema/page-facts`) gains `variants` (optional, `null` when the page has
  fewer than two options): `{source, dimensions, options, headline, headline_basis, from_price}`. `source` is
  `amazon`, `walmart`, `shopify`, `woocommerce`, `schema_org` or `picker`. `dimensions` (at most 5): `{name, kind,
  values}`, `name` as the page labels it (≤100), `kind` one of `size`, `colour`, `count`, `pattern`, `material`,
  `flavour`, `style`, `other`, `values` (≤100, each ≤200). `options` (≤100): `{label (≤300), values (≤5, one per
  dimension), price (≥0, the page's currency, or null), available (bool or null), selected (bool)}`. `headline`: the
  index in `options` of the option the page's price belongs to, or null; `headline_basis`: `selected`, `from` (the
  page shows a "from" price or a range: the cheapest option), `price` (the option whose price it is) or `""`;
  `from_price` (bool). A client that extracts facts itself (the extension) sends the same as the server reads from
  the HTML. `variations` is unchanged.
- A new skill on product pages (deep tier only; the quick tier is unchanged): `variant_check`, with features
  `variant_check.options`, `.asked` (0/1: the model was asked), `.grouped` (0/1), `.bait_price` (0/1) and
  `.price_spread` (how far the prices spread beyond what the options' amounts explain, ≤100), or
  `variant_check.not_applicable` while it is off (`UNFRET_VARIANT_CHECK=false`) and `variant_check.unavailable` when
  it failed. Findings: `variant_bait_price` and `variants_grouped`, each with the sentence the shopper reads as its
  evidence. Not in the scorer.
- New shopper reason keys: `variant_bait_price` ("The price shown is for a different item", severity medium, area
  `prices`) and `variants_grouped` ("Different products under one listing", severity low, area `product`), source
  `ai_read`, next step `check_variant`. Only the first is shown when both fire (its sentence says the grouping too). A
  bait price makes "Looks fine" "Worth a look" at most, whoever sells it; a grouping alone is a note and never a
  warning.

## Changes (a marketplace's own reference price is a note; backwards-compatible)

- A marketplace's own reference-price label (Amazon's in every locale: "List Price", "Typical price", "RRP",
  "Recommended retail price", "UVP", "Unverbindliche Preisempfehlung", "Adviesprijs", "Prix conseillé"; bol.com's
  "Adviesprijs", "Adviesverkoopprijs" and "Meestal"; Walmart's "List price" and "Typical price") no longer gives
  `pricing.reference_anchor` and the `reference_anchor` reason. It gives a `pricing` finding `marketplace_reference`
  (the label, quoted, as its evidence) and the new shopper reason key `marketplace_reference` ("Discount on the
  marketplace's reference price", severity low, area `prices`, source `page`, next step `compare_price`): a note that
  never moves the band. It is `reference_anchor` again once the product's price history shows the reference price
  was not charged (`pricing.was_above_30d_low`). A web shop's labels, and a seller's own anchor in a marketplace
  listing's text, are unchanged.
- Amazon's labels on its other locales count too ("Prezzo consigliato", "Prezzo di listino", "Precio recomendado",
  "PVPR", "Precio de venta recomendado", "Cena katalogowa", "Sugerowana cena", "Rek. pris", "Rekommenderat pris",
  "Listpris", "Unverb. Preisempf." and the Portuguese, Danish, Norwegian, Finnish and Czech equivalents), and so does
  eBay's struck "List price". On a web shop the new labels give `reference_anchor`, as "RRP" does. A reference price
  written with its currency after the number ("UVP: 14,39 €") now matches as well; it never did before.

## Changes (Common Crawl first for a shop's earlier copies, and Unfret's own first sighting; backwards-compatible)

- `domain_history` asks Common Crawl first and the Internet Archive only when Common Crawl has no usable copy. Its
  findings name the archive: `archive_copy` reads "Common Crawl copy of <address> from <month>: …" or "Internet
  Archive copy of …"; `domain_repurposed` "… but Common Crawl's copy from <month> shows …" or "… but the Internet
  Archive's copy …"; `no_archive_copies` "Common Crawl and the Internet Archive have no copies of <address> …" (or the
  one archive asked). The comparison rules and the reason's wording are unchanged.
- `reasons[].links` may now also hold a Common Crawl copy's record in its index
  (`https://index.commoncrawl.org/CC-MAIN-<crawl>-index?url=<address>&from=<timestamp>&limit=1`; Common Crawl has no
  page viewer), at most three as before. Shared results keep links of either archive only. The `source` of
  `domain_repurposed` and `established_archive` names the archive: "Earlier copies of the site (Common Crawl,
  commoncrawl.org)" or "Earlier copies of the site (Internet Archive, web.archive.org)"; an `established_archive` by
  Unfret's own first check has source "Shops Unfret has checked".
- The address's first sighting (`domain_age.archive_age_years`, `.archive_first_year`, `.age_from_archive`, finding
  `archive_first_capture`) is the oldest of Common Crawl's oldest copy among a few crawls (else the Internet
  Archive's first copy) and Unfret's own first check of the shop (the first day a page Unfret fetched itself showed
  it). New features `domain_age.first_seen_common_crawl` and `.first_seen_unfret` say which (neither: the Internet
  Archive). `archive_first_capture` reads "Online since at least <date>, per Common Crawl (the oldest copy of
  <address> found there; …)", "per the Internet Archive (its first copy of <address>; …)" or "per Unfret's own
  checks (the first time Unfret checked <address>; …)".
- `established_archive`'s detail names its source: "This web shop's address has been online since at least <year>,
  per <Common Crawl | the Internet Archive | Unfret's own checks>, and its earlier copies show the same shop."
- The card note `no_archive` reads "The web archives (Common Crawl, the Internet Archive) have no earlier copies of
  the shop's address."
- x402 `POST /v1/check` and the MCP server: unchanged in shape.

## Changes (a recommended price on the brand's own shop, or next to the 30-day low, is a note; backwards-compatible)

- On a web shop, a recommended-retail label ("UVP", "RRP", "MSRP", "Unverbindliche Preisempfehlung", "Adviesprijs",
  "Prix conseillé" and the other languages' recommended-price labels) no longer gives `pricing.reference_anchor` and
  the `reference_anchor` reason when the shop is the brand's own (its domain is the brand's name, or the seller is
  the brand). It gives a `pricing` finding `brand_reference` (the label, quoted) and the shopper reason key
  `brand_reference` ("Discount on the brand's recommended price"). On another web shop that discloses the lowest
  price of the last 30 days, with today's price at or below it and it at or below the "was" price, it gives a
  `pricing` finding `disclosed_reference` (evidence: the label, quoted, then `; lowest price in the last 30 days:
  <amount>`) and the reason key `disclosed_reference` ("Discount on a recommended price, below the 30-day low"; detail
  placeholder `{low}`). Both: severity low, area `prices`, source `page`, next step `compare_price`, a note that never
  moves the band; `reference_anchor` again once the price history shows the price was not charged
  (`pricing.was_above_30d_low`).

## Changes (euro card prices include VAT; backwards-compatible)

- The euro card prices are now **€6.00 / €20.00 including VAT** (`price_eur_cents` 600 / 2000), the final price for
  every buyer in the euro area whatever their country's rate; they were €5.50 / €17.50 with VAT added at checkout.
  `GET /v1/pricing` adds a top-level `card_tax_inclusive: {"usd": false, "eur": true}`: whether the card prices in
  each currency include sales tax/VAT. Where it is `true`, show the price as it is (no VAT added for the buyer's
  country; "€6.00, VAT included"); where `false`, as before (the dollar prices: add `vat.card_rates_pct` for an EU
  country, sales tax at checkout elsewhere). The dollar prices, the crypto prices, the packs and their credits
  (25 / 100 checks) are unchanged.
- A paid euro order credits its pack when it paid the pack's euro price including VAT (Polar's `net_amount +
  tax_amount`); refunds still count pro rata from the order's amount before tax. A euro checkout is refused before
  anyone pays when the Polar product's EUR price isn't set to tax-inclusive (or isn't the pack's price).
- Settings: `UNFRET_CARD_PRICES_EUR` (default `25=6.00,100=20.00`), `UNFRET_CARD_PRICES_EUR_TAX_INCLUSIVE` (default
  `true`; `false` goes back to VAT on top).

## Changes (crypto prices include VAT; backwards-compatible)

- A pack's crypto price, `crypto_usd` ($5.00 / $20.00), now **includes VAT**: every buyer pays it, whatever their
  country, and is credited all of it. Before, an EU buyer paid it plus VAT ($6.05 / $24.20 at the Dutch 21%) and was
  credited the pack's value. Crypto stays at or below the card price everywhere (the euro card prices include VAT too).
- `POST /v1/credits/purchase-tx`: `price.total_usd_micros` is the pack's crypto price for every country;
  `price.net_usd_micros` is that price less its VAT (`total / (1 + rate)`, rounded down) and `price.vat_usd_micros`
  the VAT in it. `amount` is unchanged in meaning (the total in the currency's base units). The Solana Pay QR's
  `X-Unfret-Total-Usd` likewise. Show the total as the price ("$5.00, including 21% VAT ($0.87)").
- `label` reads "pay with crypto: $5.00, VAT included". `vat.eu_rates_pct` keeps its meaning: how much of the price
  is VAT in each EU country.
- The scanner credits everything that arrives (manual transfers too) and records the VAT worked out of it. A quote
  made before this change still credits what was paid.

## Changes (the claim audit judges each contradiction's materiality; backwards-compatible)

- Each contradiction the claim audit tells is judged in one follow-up call (only on a page that has any): who
  benefits, where the buyer can see it. New features `claim_audit.judged` (contradictions judged), `.benefits_seller`,
  `.benefits_buyer`, `.benefits_nobody`, `.benefits_unclear`, `.buyer_facing` and `.likely_slips` (how many of the
  judged ones) and `.inconsistencies` (told as a note). Not in the scorer.
- Only a contradiction that favours the seller, where the buyer sees it (title, price, headline bullets, main photo),
  and isn't a likely data slip stays a `claim_contradicted` finding; `.contradicted`, `.contradictions`,
  `.impossible` and `.across_pages` now count those only. Every other one, a likely slip buried in a data field
  too, is a new finding `data_inconsistency`, with the same evidence form (`Says "<claim>"; <why>`). A piece count
  that is off (`.count_off`, unchanged) stays `claim_contradicted` whatever the judgement; a contradiction that
  couldn't be judged is told as before.
- A new shopper reason key `data_inconsistency` ("Details on the page that don't match", severity low, area
  `product`, source `ai_read`): `The page says “<claim>”. <why> Check which is right before you buy.` A note: it
  never moves the band and keeps no "Worth a look" by itself.

## Changes (developer docs: three ways in, backwards-compatible)

- OpenAPI: `POST /v1/check` is tagged `Check (x402 or a pass)` (was `x402`); its description names both ways to pay
  (a pass, x402 with the networks from `/.well-known/x402`), and it documents its 422, 429, 503 and 504 answers as well
  as 401 and 402. The API's description lists the three ways in (MCP, `/v1/check` with a pass, x402).
- `/.well-known/x402` adds `mcp` (`remote`, `stdio`) and `docs` (`llms_txt`, `openapi`, `guide`): where the other ways
  in are. Nothing else changes.
- `/llms.txt` opens with the three ways in (the MCP server, the API with a pass with the developer pass's three
  calls, x402), then the details as before; it adds `/v1/credits`, the pass-only errors (`TOO_MANY_REQUESTS`,
  `BAD_CHALLENGE`) and which answer a paused developer pass gets (402 `INSUFFICIENT_CREDITS`; other passes 429
  `FREE_CHECKS_PAUSED`).
- `/developers` and `/developers/http` no longer announce USDC on Base as "coming next": Base appears only while
  `/v1/check` takes it.

## Changes (OAuth sign-in for the remote MCP server, backwards-compatible)

- New, outside the OpenAPI document and 404 while `UNFRET_MCP_OAUTH_ENABLED` is off:
  `/.well-known/oauth-protected-resource[/mcp]`, `/.well-known/oauth-authorization-server`, `POST /oauth/register`,
  `GET`/`POST /oauth/authorize`, `POST /oauth/token` and `POST /oauth/revoke` (README, "Signing in with OAuth"). Their access tokens (`ufat_…`) work at `/mcp` only.
- `/mcp`: a 401 adds `resource_metadata` and `scope` to `WWW-Authenticate` while OAuth is on; `Bearer ufk_…` works as
  before.
- `POST /v1/keys`: unchanged (its description now names its 400 and 429 in prose).

## Changes (`vat.applies_to` says card prices include tax; backwards-compatible)

- `GET /v1/pricing`: `vat.applies_to` follows `card_tax_inclusive`. With card prices including tax in both currencies
  (the default since 0.15.0) it reads "all purchases: card prices include sales tax/VAT (collected by the payment
  provider); crypto prices include VAT"; the old "crypto purchases (card purchases: the payment provider adds its own
  sales tax/VAT)" is kept only while card prices are before tax. It is prose for people: read `card_tax_inclusive`
  for logic. No field added or removed.

## Changes (anonymous usage statistics, backwards-compatible)

- `POST /v1/keys`: new optional `source` (`extension`, `website`, `android`, `api`), for Unfret's anonymous usage
  counts only, never stored with the pass; any other value is a 422 (snapshot updated). Left out, it is guessed from
  the request's `Origin`.
