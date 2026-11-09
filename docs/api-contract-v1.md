# Unfret API contract v1: credits door (backend phase 4 ↔ browser extension phase 5)

Base URL: `https://unfret.kroshtan.com` (configurable in the extension for dev: `http://localhost:8080`).
All JSON. Errors use the existing shape `{"detail": str, "error_code": str, "metadata"?: any}`.

## Identity: Sign-In With Solana (SIWS), basic
Credits belong to a wallet public key. The user proves control by signing a plain-text message; nothing else is stored.

- `GET /v1/auth/nonce` →
  `{"domain": "unfret.kroshtan.com", "uri": "https://unfret.kroshtan.com", "statement": "Sign in to Unfret. This
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
  `page_content` (never raw logged-in HTML; strip account/header/footer/nav).
  Debited `analysis_lamports` up front; refunded when the page isn't analysable (score -1) or on error.
  Insufficient balance → 402 `{"error_code": "INSUFFICIENT_CREDITS", "metadata": {"balance_lamports", "analysis_lamports"}}`.
  401 for missing/invalid/expired token.
- `POST /v1/analyze/stream` (Bearer), same body, `text/event-stream`: one `event: step` per skill finishing
  (`data: {"skill": str, "findings": [...]}`), then `event: result` with the body above, or `event: error`.
- `PageFacts` is the backend's pydantic model `veridex.facts.schema.PageFacts`; its JSON schema is served at
  `GET /v1/schema/page-facts` so the extension can validate against it.

## Pages served by the backend
- `GET /connect` — sign-in page. After a successful verify it does
  `window.postMessage({"type": "veridex-session", "token", "address", "expires_at"}, location.origin)`.
  The extension's content script, injected only on `https://unfret.kroshtan.com/connect*`, relays it to the
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
  `beneficiary` (base58, defaults to `address`). The transaction carries a Memo instruction `veridex:credit:<beneficiary>`,
  and `solana_pay_url` a `memo=` parameter with the same text. The scanner credits the memo's beneficiary when the
  transaction has exactly one well-formed `veridex:credit:` memo; otherwise (none, several, malformed) the transfer's
  source. CPI (inner-instruction) System transfers from vaults count. `GET /v1/credits/solana-pay.svg` also takes
  `beneficiary`. So a Squads vault can pay with `address = vault`, `beneficiary = the member key that signs in`.
- **Stream events:** `/v1/analyze/stream` emits `event: step` with `data: {"skill": <node>, "findings": [...]}` per
  finished node (the classifier's step adds `"page_type"`), then `event: result` (the `/v1/analyze` body) or
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
  "connect_url": "https://unfret.kroshtan.com/connect?pair=<pair_id>", "expires_at": ISO-8601}`.
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
  a result served from the 1-hour cache, which is now free). Show "No check was used" for those.
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
- MCP: spend caps are in dollars (`VERIDEX_MCP_MAX_PRICE_USD`, `VERIDEX_MCP_SESSION_CAP_USD`); a wSOL charge is valued
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
  up before the result is refunded; a finished result is cached first, so repeating the request gets it free.
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
- Extension 0.5.0 is published at `/extension` (CRX, `updates.xml` on unfret.kroshtan.com, and the Chrome Web Store zip
  `veridex-chrome-0.5.0.zip`).

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
  the whole key; only the id is used) instead of `beneficiary`. The memo is `veridex:credit:key:<id>`. A memo naming
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
  `ALREADY_REPORTED_BY_YOU`, 429 `REPORT_LIMIT` (5 per account per month, `metadata: {"used", "limit"}`), 503
  `QUEUE_FULL` (about 200 open reports).
- Reward: when the human label shows the score was at least 30 points outside the label's zone (legit ≥ 70, junk and
  scam ≤ 40), the reporter gets 3 checks. The report is then deleted (its only link to the account); the scrubbed page
  and its label stay as training data. Reports nobody handled are deleted after 30 days.
- Operator: `GET /v1/admin/reports` (the queue, without accounts) and `POST /v1/admin/reports/{id}/label`
  `{"label": "legit"|"junk"|"scam"|"reject"}`, used by `make reports`.
- Extension 0.6.0 replaces 0.5.0 at `/extension` (`veridex-0.6.0.crx`, `veridex-chrome-0.6.0.zip`): an API key in the
  settings as an alternative to a wallet sign-in, and "This is wrong" under a result. Same key and ID, same
  permissions.

## Changes (pricing v2: one price per check, packs, VAT on crypto; breaking for credit purchases)

- **One price per check**, `VERIDEX_PRICE_PER_CHECK_USD` ($0.20): an analysis costs one check in credits and the x402
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
  state's standard rate. The memo is `veridex:quote:<20-char id>`; the stored quote names who to credit and the VAT.
- **`GET /v1/credits/solana-pay.svg`** takes `checks`, `country`, `currency` and `beneficiary`/`key_id` (not
  `amount`); headers `X-Unfret-Total-Usd` and `X-Unfret-Vat-Rate` carry the price.
- **Crediting:** what arrives, minus the VAT at the quote's rate, is credited. A transfer without a known quote (sent
  by hand) counts as a sale to an unknown country at the Dutch rate (credited `received / 1.21`); the older
  `veridex:credit:<account>` memo still names the beneficiary. Each credit leaves a sales record (day, country,
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
  server fetches the page itself (`veridex.page_fetch`) before anything is charged. An account whose balance doesn't
  cover a check gets its 402 without a fetch. A page that can't be checked that way is a free `422` (plain JSON, no
  stream) with `error_code` `PAGE_UNAVAILABLE` (couldn't be fetched), `PAGE_BLOCKED` (the shop answered 401/403/405/
  406/429/503/999, or with a Cloudflare/DataDome/PerimeterX/Amazon bot check), `PAGE_TOO_THIN` (under 400 characters
  of visible text with no price and no product JSON-LD: a shell its scripts fill in) or `INVALID_URL` (not a public
  http(s) address). Fetches are limited per account (`VERIDEX_ANALYZE_FETCH_PER_MINUTE`, 10; over it `429`).
  The result cache and the `report_token` are keyed on the URL-only request; a report of such a check sends back
  `{"url": …}` and the server fetches the page again for the review queue.
- Requests that send the page (the extension) are unchanged.

## Changes (the "Spotted recently" showcase, backwards-compatible)

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
- The `VERIDEX_TRIAL_*` settings are gone.

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
  ignore it), and the Solana Pay link has `&reference=<pubkey>`. The memo `veridex:quote:<id>` stays. The scanner
  finds the quote by the reference among the transaction's accounts first (some wallets, e.g. Phantom, drop memo
  instructions), then by the quote memo, then the older `veridex:credit:` memo, then credits the sender. The
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
  is paid in SOL), the tier's dollar price times `VERIDEX_X402_MAX_PRIORITY_FEE_BPS` converted at the SOL/USD quote.
- A 402 for a refused payment may carry `reason` next to `error`. With `error: "simulation_failed"` it says why the
  payment's simulation failed, derived from the simulation's error and logs: `insufficient_funds`,
  `source_token_account_missing`, `mint_mismatch`, `owner_mismatch`, `account_frozen`, `account_not_found`,
  `blockhash_not_found`, `insufficient_funds_for_rent`, another error class in snake case (`invalid_account_data`),
  or `custom_program_error_<code>` for another program's own error. Treat unknown values as opaque.
