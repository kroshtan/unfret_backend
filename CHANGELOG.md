# Changelog

## Unreleased

### ⚠ BREAKING CHANGES

* **No free trial**: a wallet's first sign-in no longer grants free checks; every new account (wallet or API key) starts at zero and buys a pack. `/v1/pricing` loses `trial_checks`, and the `VERIDEX_TRIAL_*` settings (checks, wallet age, signatures, per-visitor limit, daily cap) are gone, with the on-chain history lookup and the in-memory limiter that served only the trial. Balances already granted stay. The `wallets.trial_granted` column (migration 5) is left in place, unused. The home page no longer mentions a trial. The reward of 3 checks for a good report is unchanged
* **Credit purchases are packs, with VAT** (pricing v2): `POST /v1/credits/purchase-tx` and `GET /v1/credits/solana-pay.svg` take `checks` (25, 50 or 100) and `country` instead of an amount; `POST /v1/credits/fiat-checkout` takes `checks` instead of `amount_usd_micros`. Purchase bonuses are gone. `/v1/pricing` loses `bonuses` and `purchase`. Analyses and x402 deep checks now cost $0.20

### Features

* **Checkout confirms the payment** (migration 14): after paying from a browser wallet or showing the phone QR code, `/checkout` replaces the payment choices with a "Waiting for your payment…" panel (the QR code stays above it) and then "✓ Payment received: 25 checks added" (to the wallet, or "to key <id>", with the key and a recovery-code reminder when the key was made on the page, and the new balance when a session or whole key is at hand); after about three minutes a calm "still settling" message with "Check again"; wallet errors show in the same place. No sign-in needed any more before paying from a browser wallet. Driven by the new `GET /v1/credits/quotes/{quote_id}` (`{"paid", "checks"}`, no auth, `no-store`), backed by `vat_quotes.paid_at`, which the scanner sets in the crediting transaction. `purchase-tx` returns `quote_id`; the QR code's response carries `X-Unfret-Quote`, and the page fetches it to read the header
* The Solana Pay message is "Unfret checks" (was "Unfret credits")

* **"Spotted recently" showcase** (migration 13, `veridex.showcase`): real checks of real product pages, run by the operator and published only after the owner's review; no demo data. `make showcase ARGS="add URL | list | show ID | publish ID | remove ID --reason … | screenshot ID FILE | title ID TEXT"` drives `/v1/admin/showcase…`; the server fetches the page and runs the full analysis itself, without an account or charge, and stores a draft (title, host, product URL, checked at, the analysis response, an optional screenshot of at most 800 kB in the row). `make reports` offers to promote a page whose label agrees with the model: only its address is sent and the page is checked afresh, so no case carries anything from a report or an account. Public: `GET /v1/showcase[/{id}]` (published only, `Cache-Control: public, max-age=300`), a "Spotted recently" section on `/` and `/check`, and `/showcase/{id}` in the link checker's presentation (its result rendering moved to `static/check-render.js`, shared by both). Factual wording: each band is phrased as what Unfret would tell a shopper about the listing, no score, never "scam" or "fraud" (such a headline or quote is left out), "Checked on …; the page may have changed since", a nofollow link to the product and a removal route for shop owners. The privacy page (en/nl) describes it
* **Link checker** at `/check`, phones first: paste or share a product link and check it, signed in with a wallet or an API key (kept in the browser's localStorage until sign-out; no history). Installable as a web app (`/manifest.webmanifest`, `/check-sw.js`) with an Android share target; the service worker turns a shared `/check?url=…` into `/check#url=…` so the link never reaches the server's logs. Backed by URL-only `POST /v1/analyze[/stream]`: the server fetches the page before charging, and a page it can't check (`PAGE_UNAVAILABLE`, `PAGE_BLOCKED`, `PAGE_TOO_THIN`, `INVALID_URL`) is a free 422, shown as "this shop blocks automatic checks" with a pointer to the extension, never as a verdict. httpx request logging (which carries full URLs) is kept below the log level
* Extension 0.7.0 at `/extension` (same key and ID, permissions unchanged), now published as `unfret-0.7.0.crx`: the Unfret name and icon, the softer labels, "Check this page", and up to three photo thumbnails under a photo-reuse reason (loaded only from this server's image proxy)
* `POST /v1/admin/keys/{key_id}/grant` (operator token): add up to $10 of credit to an existing API key per call, for support and for test checks for a store reviewer
* **Renamed to Unfret** (user-facing only): page titles, the home page, the sign-in statement ("Sign in to Unfret"), the Solana Pay label, `llms.txt`, the x402 discovery document and payment requirements (`serviceName`/`service`: "Unfret"), the OpenAPI title ("Unfret API"), the README and the API contract. The MCP server is now the `unfret-mcp` package and server `unfret`, reads `UNFRET_*` settings (the `VERIDEX_*` names and the `veridex-mcp` command still work) and defaults to `https://unfret.kroshtan.com` (it still pointed at the retired veridex host). The Python package, `VERIDEX_*` settings, database names, payment memos (`veridex:credit:`) and key prefixes keep their names
* **Softer verdict labels** for shoppers (en/nl): "Looks fine" / "Ziet er goed uit", "A few things to check" / "Een paar dingen om na te gaan", "Some warning signs" / "Een aantal waarschuwingssignalen", "Many warning signs" / "Veel waarschuwingssignalen". The machine fields (`action`, `risk_score`, the band keys) are unchanged
* **Home page** at `/` (was a redirect to `/docs`): what Unfret does, for shoppers and for agents, with links to the extension, checkout, privacy statement and API reference
* `/extension` serves releases named `unfret-*` as well as `veridex-*`; the Linux policy command writes `unfret.json` and removes the old `veridex.json`
* **One price per check** (`VERIDEX_PRICE_PER_CHECK_USD`, $0.20, `veridex.credits.packs`): the analysis price, the x402 deep tier and every pack derive from it, so changing it moves every sales route (`/v1/pricing`, x402 402s, `/.well-known/x402`, `/llms.txt`, the MCP server's `get_pricing`, the checkout). Packs of 25/50/100 checks cost $6/$11.50/$22 by card (the pack's value plus the provider's worst-case 6.5% + $0.50, rounded up to $0.50; the provider adds sales tax/VAT) and $5/$10/$20 with SOL or USDC plus VAT. The card price is presented as the price and crypto as the discount (`packs[].label`, en/nl), never as a card surcharge
* **VAT on crypto purchases** (migration 10, `veridex.credits.vat`): the buyer states their country, prefilled from Cloudflare's `CF-IPCountry` only when Caddy saw the request come from Cloudflare (`X-Edge-Peer`, checked against the vendored ranges); the IP is never stored. `nl_under_threshold` (default): Dutch 21% for every EU buyer, none elsewhere; `per_country`: each member state's standard rate, vendored from the EU's TEDB (`scripts/update_vat_rates.py`, `make vat-rates-check`, a `vat_rates_stale` warning after 45 days). The purchase's quote is named in the memo (`veridex:quote:<id>`), so the scanner credits the net and records the VAT; a transfer without a quote counts as an unknown country at the Dutch rate. One sales record per crypto credit (day, country, mismatch flag, net, rate, VAT, asset, amount, signature; no IP, no wallet column), kept ten years; `GET /v1/admin/sales/{YYYY-Qn}` exports a quarter as CSV. The privacy page (en/nl) says so
* **Own image index** (migration 11): `reverse_image` looks the main product photo up in Unfret's own index. A match needs both perceptual hashes (pHash, dHash), a DINOv2-small embedding (Apache-2.0, ONNX on the CPU) and a colour grid to agree, with thresholds tuned on 48,777 ABO catalogue photos and 4,881 held-out queries (0 false matches: under 0.061% per query at 95%; 54% recall on edited copies) ([docs/image-index.md](docs/image-index.md)). Small images, banners and blank images are skipped; a photo on more than 50 shops is a stock image; the own shop and the same listing (a changed listing is an update) don't count. Filled by analyses; an operator can import a seed of Amazon Berkeley Objects photos (CC BY 4.0) with `python -m veridex.image_seed import`. Runs in the quick tier too. Findings carry up to three thumbnails, served by `GET /v1/images/{id}/thumbnail.jpg`
* Reasons `photo_reused` (an unknown brand with other resale signs only) and `linked_new_shops`
* **Known-listing fast path** for `/v1/check` (migration 12): the answer of a full analysis of a page Unfret fetched itself is served again for a day (`source: "fingerprint"`, still paid) while the shop's fingerprint is unchanged. Never kept from or served instead of caller-supplied `page_content`, nor from degraded analyses
* **Shop fingerprint store** (migration 7, `veridex.shop_store`): every analysed web shop (not marketplaces) is recorded after the answer, in the background: first/last day seen (days, never times), platform and apps, a template simhash, the RDAP/CT dates the analysis already looked up, and the account identifiers on its pages (Google Analytics/Tag Manager/Ads, AdSense, Meta/TikTok/Pinterest pixels, Stripe and PayPal merchant IDs, the `*.myshopify.com` name). Contact e-mail addresses and phone numbers only as HMAC-SHA256 hashes (enforced by a check constraint), never shown, deleted after 180 days; the rest after 400 days. No URL, caller, score or verdict is stored. `DELETE /v1/admin/shops/{domain}` and `POST /v1/admin/contacts/forget` handle removal requests. Off in evaluation runs. The privacy page (en/nl) describes it
* **API keys** (migration 8, `veridex.credits.keys`): an account without a wallet. `GET /v1/keys/challenge` + `POST /v1/keys` (a proof of work, 5 new keys per visitor network per day) create an empty key (`ufk_<id>_<secret>`) and a recovery code; both are shown once and stored only as SHA-256 hashes. `POST /v1/keys/recover` rotates both and keeps the id and balance. Keys work as `Authorization: Bearer` wherever a session does. Top up with SOL/USDC (`purchase-tx` with `key_id`, memo `veridex:credit:key:<id>`; a memo naming an unknown key credits the payer) or, once a Merchant of Record is wired in, by card. `/checkout?for=key` creates, recovers and tops up keys
* **Fiat interface, shaped for Polar** (`veridex.credits.fiat`, no real provider yet): hosted checkout (USD, at least $10) with our account id in its metadata and a server-side checkout → account mapping; Standard Webhooks verification with a replay window; paid orders credited once; refunds debit pro rata, disputes debit the whole order, and may take a balance below zero (which blocks paid use). Per order: provider, order id, account, amount, credits, refunded/disputed, for 180 days; never the buyer's details. Tested with a fake provider
* **Reports of wrong results** (migration 9, `veridex.reports`): results carry a `report_token`; `POST /v1/reports` (with consent) sends that one check's page back. Personal details are scrubbed on the server (greetings, delivery lines, emails, phones, IBANs, cards, URL query strings; raw HTML reduced to facts and text; no checkout page). Duplicates are closed before the queue (`already_reported` counts as confirmation, `already_known` within 90 days of a label); 5 reports per account per month, one per page, about 200 open. A label at least 30 points from the model's score pays the reporter 3 checks; the report is then deleted and the page kept as training data without the account. Unhandled reports go after 30 days. `make reports` labels the queue (most confirmed and least certain first) with one keypress per page, straight into the corpus. The privacy page (en/nl) describes keys, card payments and reports
* Extension 0.6.0 at `/extension` (same key and ID, permissions unchanged): "use an API key" in the settings as an alternative to a wallet sign-in, and "This is wrong" under a result (reason, consent for that one check, the outcome in plain words), in English and Dutch
* `shop_links` skill (quick and deep tiers): shops sharing an identifier, one hop only, ignoring identifiers held by more than 25 shops; evidence names the shared public ID and the shops, never a contact. Features are logged but have no weight in the scorer until the corpus has them

* **US market**: Walmart.com product pages (`platform: "walmart"`, read from the page's `__NEXT_DATA__`: price and "was" price, seller, Walmart fulfilment, rating histogram, verified reviews; product id `walmart:<id>` in the price store), and the current amazon.com buy box ("Ships from"/"Sold by" as sibling elements, `avp-badge-linkless` verified badges, the media "Format:" suffix off the byline). A shop counts as US (`veridex.market.shop_market`) only on positive evidence: US dollar prices (structured, or "$" prices outnumbering €/£ in the text), no EU VAT ID, not on an EU/UK country domain. On a US shop a named company or a US business address counts as the imprint, the missing-returns reason doesn't cite EU law (`no_return_policy`), and US sales tax is left out of the checkout drip-pricing comparison. New skills `us_identity` (company, US address and phone, mailbox-only addresses; no state registry lookup: there is no free, terms-permitted API) and `shop_policies` (15+ day delivery, returns abroad, return address withheld, restocking fees above 20%, doubtful "Made in USA"), in both tiers, with shopper reasons in en/nl; their features have no weight in the scorer until the corpus has them (`python -m evals.us_market` reports the effect per market). A skill can now report `<skill>.not_applicable`, which the scorer reads as neutral

### Bug Fixes

* **Purchases whose wallet drops the memo reach the right account** (migration 14): Phantom sent a purchase without the `veridex:quote:` memo, so the scanner credited the paying wallet instead of the API key the quote named. Each quote now has a Solana Pay reference (a random public key, `vat_quotes.reference`), carried as an extra read-only account on the transfer instruction of `purchase-tx` and as `reference=` in the Solana Pay link; the scanner finds the quote by it first, then by the memo. The reference is never credited

* **x402 rough edges an agent hit on devnet**: `/.well-known/x402` and `/llms.txt` now name the network by the same CAIP-2 identifier as `accepts[].network` (`solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1`; the cluster name moved to `network_name`). A payment whose simulation fails still answers `error: "simulation_failed"` and now adds `reason` (`insufficient_funds`, `source_token_account_missing`, `mint_mismatch`, `blockhash_not_found`, the error class, or `custom_program_error_<code>`), derived from the simulation's error and logs only. Each `accepts` option's `extra` gains `decimals` (9 wSOL, 6 USDC), `quoteToleranceBps` (200 wSOL, 0 USDC) and `feeCapNote`, which says that `constraints.maxPriorityFeeLamports` is in lamports on the USDC option too (5% of the price, converted at the SOL rate); `llms.txt` says so and tells clients to re-fetch the 402 right before paying
* Checkout: the API key option now says to choose "API key" in the Unfret extension's popup (extension 0.8.3) instead of its settings
* **A well-known US brand's own product page was called "careful"** (a paid check of allbirds.com came back `warn`, risk 0.61, with "No company details" as its only reason). Three causes, all fixed:
  * Product JSON-LD of type `ProductGroup` (schema.org's variant markup, which Shopify themes emit) was ignored, and so was a product's `AggregateRating` in a separate block: the page had no brand, rating or JSON-LD, and the content read was told `Brand: unknown`, so it scored a famous brand as white-label. Both are read now (a ProductGroup's price comes from the page's Open Graph price or, failing that, its first variant's offer; a shop's or organisation's rating never counts as the product's)
  * On a US shop a missing EU imprint and VAT ID counted against it (in the scorer, +0.12 log-odds, and as the shopper reason "No company details"), although `veridex.market` says a US shop must not be marked down for lacking them. `eu_registry` now reports them as not applicable on a US shop (a named company or US address still counts as its imprint), which the scorer reads as neutral feature by feature (`<skill>.<feature>.not_applicable`, also in `evals.train`) and which gives no reason
  * A page whose only reasons were absences (no imprint, no return or withdrawal policy, missing trust information, or none) could be "careful" or worse while the scorer was unsure. Below `VERIDEX_VERDICT_ABSENCE_ONLY_MIN_CONFIDENCE` (0.2) it is now "concerns" ("A few things to check"); a concrete red flag or a sure scorer still warns. The x402 `action` (from the risk score and the caller's policy) is unchanged
  * Measured on the corpus TEST split (333 pages) with the shipped scorer `v1-20261108-C`, unchanged: AUC 0.901 → 0.895 overall, 0.967 → 0.964 US, 0.832 → 0.832 EU; pages shown "careful" or worse: legit 35/143 → 25/143 (US 1/39 → 1/39, EU 34/104 → 24/104), unreliable 166/190 → 155/190; precision of that band 0.826 → 0.861. A retrained candidate was worse on EU (AUC 0.812) and is not shipped. Regression test on a hand-made page with the same structure: `tests/fixtures/pages/shopify-productgroup-us.html`
* **Fewer false alarms for agents and shops outside the EU**:
  * x402: under the `balanced` and `lenient` policies an agent is no longer told to `warn` (or `block`) about a page a shopper would see as "reliable" or "concerns" (`balanced` warned from risk 0.4, below the shopper's "careful" at 0.5, so 37 of 143 legit TEST pages drew a warning against 25 shown "careful"). `strict` is unchanged; a degraded analysis still warns. Also for answers served again from the fingerprint store. `/.well-known/x402` lists the exempt bands per policy (`never_warns_on_bands`), `/llms.txt` and the README say so
  * A third market, `other` (`veridex.market`): a shop on a country domain outside Europe and the US (.ca, .au, .br, …) or pricing in a currency of neither (CAD, AUD, …) is no longer judged by EU law. A shop with no evidence at all keeps the EU rules (in the corpus such pages are 6 scams to 1 legit shop)
  * Outside the EU (US and `other`), a missing return/withdrawal policy is not applicable, like the imprint and VAT ID: neutral for the scorer and no "No return policy found" reason. A policy the page does link to still counts. Sales tax added at checkout is no drip pricing outside the EU either
  * The content read can be told which policy pages a shop's page links to (`VERIDEX_CONTENT_POLICY_LINKS`, off): its text has the footer stripped, so it scored "missing trust information" without seeing the returns or contact links. Off until measured, since it changes the prompt (recorded eval responses for those pages miss; a re-recording of the storefront pages costs roughly 4M input tokens) and the meaning of a feature the shipped scorer was trained on
  * Measured on TEST (333 pages), shipped scorer unchanged: x402 `balanced` warn-or-block on legit pages 37/143 → 25/143 (US 2/39 → 1/39, EU 35/104 → 24/103), recall 176/190 → 155/190 (now the same as the shopper's "careful or worse"); shopper bands unchanged (legit 25/143, recall 155/190); AUC overall 0.8946 → 0.8943, US 0.9643 → 0.9643, EU 0.8315 → 0.8292 (with the same market membership; 6 pages moved to `other`: 1 legit, 5 scams, 4 of them still "careful or worse")
* **No "discount on a reference price" for a marketplace's own label**: amazon.com's "List Price" / "Typical price", Walmart's and bol.com's "Adviesprijs" appear on every discounted listing whatever the seller does, so on those platforms they no longer count as a reference anchor on their own (a seller's own anchor such as "Retail value" still does, and on a web shop "List price" still does). Made-up reference prices on marketplaces are left to the price history check

### Removed

* **The USDC→SOL swap** (`veridex.x402.swap`, the Jupiter client, `VERIDEX_SWAP_*` settings, the `swap_failing` alert): the product never swaps. USDC is swept to the cold address's USDC account as USDC (`VERIDEX_USDC_SWEEP_THRESHOLD_UNITS`, $5, or ≥$0.50 older than a day; on mainnet and devnet alike), creating that account idempotently with the hot wallet paying the rent. The hot wallet signs only verified payments and the two sweep shapes, and the outflow audit accepts a token transfer by the hot wallet only as a USDC `transferChecked` into the cold address's USDC account, so a swap now counts as an unexpected outflow. `hot_usdc_units` in readiness stays (USDC not yet swept); `usdc_above_threshold` now means USDC sweeps are failing
* The Google Cloud Vision photo lookup (`reverse_image` skill, `VERIDEX_VISION_API_KEY` / `VERIDEX_VISION_MONTHLY_BUDGET`, the `vision` status field and the "same photo on wholesale sites" reason): no external image services any more. Product photos will be matched against Unfret's own image index instead. The privacy page no longer mentions Google

## [0.6.0](https://github.com/kroshtan/veridex_backend/compare/0.5.0...0.6.0) (2026-11-08)

Open-source release.

### Features

* Agent experience: every error is JSON with `detail` and `error_code` (request validation errors too, as `VALIDATION_ERROR` with `errors`, and unknown routes as `NOT_FOUND`); an errors table (what each code means, whether you were charged, what to do) in `/llms.txt` and the README; each 402 option states what the payment transaction must look like in `extra.constraints` (memo, compute budget and priority-fee caps, allowed programs, no lookup tables). MCP: a free `get_pricing` tool, described parameters, and `status: unavailable` with a retry hint when Veridex is paused or rate-limiting
* Performance: payments settle in parallel (the wallet lock covers verification and signing only, not the wait for confirmation); the hot wallet's balances are read in one `getMultipleAccounts` call
* **Prices in US dollars**: x402 quick $0.01, deep $0.15, an analysis $0.15; purchase bonuses ≥$5 +10%, ≥$25 +20%; packs of 10/50/200 checks. USDC pays exactly; SOL is converted at quote time with Pyth's SOL/USD price read on chain (fully verified, ≤60 s old, confidence ≤1%; charges round up, credits round down) and refused while the oracle fails those checks, when only USDC is offered. x402 offers wSOL (≤2% drift from the quote) then USDC once the hot wallet's USDC account exists. Balances are micro-dollars (`balance_usd_micros`; migration 5 converts lamport balances so every wallet keeps its number of checks). `/v1/pricing`, `/checkout`, `/llms.txt` and `/.well-known/x402` show dollars with live SOL amounts. All prices come from one `PriceBook`
* USDC alongside SOL on both payment tracks
* USDC→SOL swaps before sweeping (mainnet): Jupiter routes, verified by simulation before the hot wallet signs (exact USDC out, at least the slippage bound and a Pyth floor in, fee-only SOL debit, no delegate/close authority/owner change); the cold wallet only ever receives SOL. The sweep job also creates the hot wallet's USDC account; the outflow audit recognises swaps by their net effect
* The MCP server pays in USDC when offered and held, otherwise SOL, with per-currency spend caps; `/checkout` has a SOL/USDC switch
* Minimum credit purchase raised to one check ($0.15)
* Credits door replaces accounts: Sign-In With Solana (stateless nonces and session tokens; the signature grants no spending rights) and a balance per wallet (micro-dollars since migration 5), the only user state stored. `/v1/analyze` (and its stream) debits atomically and refunds pages that can't be analysed and failed analyses
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
* Relative "see reviews" links were resolved against the site root instead of the page
* Daily quota now resets at midnight UTC regardless of database timezone
* Reverse image search reported "no dropship domains" when every search had failed
* `httpx` was only a dev dependency but is used at runtime

### Maintenance

* Logs are one JSON object per line, and a processor drops page and visitor fields (`url`, `ip`, …) and masks URLs and IP addresses in every other value; the start-up log states the configuration without secrets (`VERIDEX_LOG_JSON=false` for readable local logs)
* Settings that contradict each other (purchase limits below one check, unsorted bonus tiers, a wide quote tolerance, a sweep reserve above its threshold, …) are refused at start-up
* Ruff now also runs Bandit (`S`), blind-except (`BLE`) and unused-`noqa` (`RUF100`) checks; the real findings are fixed
* `chain_id` removed from `GET /v1/auth/nonce` (unused since sign-in stopped sending a chain ID); leftover Paddle and signup test settings removed
* `docs/api-contract-v1.md` is the API contract, and an OpenAPI snapshot test (`tests/snapshots/openapi.json`) flags any contract change
* `make bump VERSION=x.y.z` sets the version and dates the CHANGELOG (no commit, tag or deploy)
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
