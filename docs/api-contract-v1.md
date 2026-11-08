# Veridex API contract v1: credits door (backend phase 4 ↔ browser extension phase 5)

Base URL: `https://unfret.kroshtan.com` (configurable in the extension for dev: `http://localhost:8080`).
All JSON. Errors use the existing shape `{"detail": str, "error_code": str, "metadata"?: any}`.

## Identity: Sign-In With Solana (SIWS), basic
Credits belong to a wallet public key. The user proves control by signing a plain-text message; nothing else is stored.

- `GET /v1/auth/nonce` →
  `{"domain": "unfret.kroshtan.com", "uri": "https://unfret.kroshtan.com", "statement": "Sign in to Veridex. This
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
  The first successful sign-in of a wallet creates its balance row (trial rules below).

## Credits
The balance is held in **lamports** (`pubkey → balance_lamports`); the UI shows checks remaining.
- `GET /v1/pricing` → `{"analysis_lamports": int, "x402": {"quick_lamports": int, "deep_lamports": int},
  "bonuses": [{"min_lamports": int, "bonus_pct": int}], "suggested_packs": [{"checks": int, "lamports": int}],
  "hot_wallet": base58, "network": "solana-mainnet"|"solana-devnet", "trial_checks": int}`
- `GET /v1/credits` (Bearer) → `{"address": base58, "balance_lamports": int, "checks_remaining": int}`
- `POST /v1/credits/purchase-tx` `{"address": base58, "lamports": int}` → `{"transaction": base64 (unsigned
  VersionedTransaction: a System transfer address → hot wallet, payer = address), "lamports": int, "hot_wallet": base58,
  "solana_pay_url": "solana:<hot>?amount=<SOL>&label=Veridex&message=Veridex%20credits"}`
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
  "Sign in to Veridex. This signature costs nothing and grants no spending rights. For the device showing K7-4Q."
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
- `/v1/health/ready` adds `hot_usdc_units`; x402 alerts may include `swap_failing` and `usdc_above_threshold`.
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
  "purchase": {"min_usd_micros", "max_usd_micros"}, "hot_wallet", "network", "trial_checks", "usdc": {"mint",
  "decimals", "analysis_units", "x402": {"quick_units", "deep_units"}, ...}, "sol": {"usd_per_sol", "quoted_at",
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
- A credit purchase (or a memo naming a wallet) no longer uses up that wallet's free trial.
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
  flags) it names the verdict and its two strongest reasons ("Be careful: fake countdown timer and pre-ticked paid
  add-on.").
- **Fake discounts:** new reasons `implausible_discount`, `reference_anchor` (the "was" price is an RRP/"usually"
  price), `permanent_sale` and, once a product has a month of anonymous price observations, `was_never_charged`.
  New features under `pricing.*`; none are in the scorer yet. `GET /privacy` is the privacy statement.
