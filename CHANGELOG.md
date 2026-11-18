# Changelog

## Unreleased

## [0.13.0](https://github.com/kroshtan/unfret_backend/compare/0.12.0...0.13.0) (2026-11-18)

### Free checks and the Unfret pass

* **Free checks: install and check, no code, no purchase.** A new pass can come with 5 free deep checks (the extension
  asks for one on install, `/check` on a browser's first check), spent before any bought checks. Quick checks (rules
  only, no AI) are free for everyone, with or without a pass. All free deep checks share one budget a day; when it
  runs out, "free deep checks are back tomorrow" and quick and bought checks go on. A check that fails or can't be
  assessed is given back. The extension's first page after installing says how many free checks you have and leads
  straight to checking a page (#168).
* **`/check` works without a pass**: a first-time visitor pastes a link and gets the browser's free checks, then
  quick checks, and is told why a check was quick. It offers three examples to try: the test shop, its sister shop
  and a clean made-up shop with none of the tricks, to compare (#168, #236).
* **No second round of free checks after reinstalling the extension**, even when the browser doesn't sync: the
  welcome page tells the extension whether this browser already had its free checks before the pass is made, and
  says why a pass starts without them (#195).
* **Plain words: the Unfret pass and its backup code** (were "API key" and "recovery code"). "My checks" shows your
  pass or wallet, its balance and how to use it on your phone; buying again tops up the pass you already use. The
  phone app starts with "Get checks to start" instead of a sign-in wall (#155).
* **A pass reaches this browser or the extension only when you say so**: a pass from a QR code or link asks "Use this
  pass here?", and "Send to the extension" works on a click (#165, #155). The checkout and connect pages show a button
  when the extension needs a click, and wait for the extension's own confirmation window before switching passes
  (#188).
* **New passes and recoveries can't be used up for everyone**: they are limited per visitor network only; mass
  creation gets more expensive instead of locking anyone out (#171).
* **Old passes can be retired by the operator** (`make retire-passes`, a dry run that only counts unless confirmed;
  it refuses passes that hold a balance or bought checks unless told otherwise). A retired pass answers like any
  unknown pass, and `/check`, My checks and the welcome page say the old pass has ended and start over with the free
  checks (#238).

### Verdicts and the result card

* **The four verdicts are called Looks fine, Worth a look, Think twice and Serious concerns** (were Looks fine, A few
  things to check, Some warning signs, Many warning signs), on the site, shared results (also older ones), the API
  docs and `llms.txt`. "Serious concerns" only shows when the scorer is sure; otherwise it reads "Think twice". A
  verdict below "Looks fine" always names a reason. The band identifiers in the API are unchanged (#164).
* **The result card**: every result shows its evidence, the same on `/check`, shared results, the extension and the
  phone app: the same product sold elsewhere first (with both photos), each finding with its source ("Domain
  registry (RDAP)", "Unfret's price history", "The page"), one suggested next step, and what was checked, area by
  area. A clean result says how many checks it passed. "Wrong result?" and "Object to this result" links on every
  result. New fields in the API are additive (#205).
* **Delivery and returns are with the prices**: a long delivery time, returns sent abroad, a withheld return address,
  a costly restocking fee or no return policy found now show under "Prices, delivery and returns" instead of "Who's
  behind the shop" (#245, #247).
* **The same product sold elsewhere** also when a shop names its main photo by a path instead of a full address
  (#222).
* **The same photo under other brand names**: a product photo also sold under another brand name ("Same photo under
  other brand names", at worst "Worth a look": relabelled generic goods are mostly overpriced, not fraud), or a known
  brand's catalogue photo on a page naming another brand ("Photo of another, known brand", at worst "Think twice").
  Neither fires for a brand that is known beyond the page or on the brand's own site, and neither ever gives "Serious
  concerns". On marketplaces the photo is also compared with the marketplace's other listings (#234).
* **Quick results say whether a deep check is worth it**, what it would add for this page and what it would cost
  you: suggested for "Worth a look" and "Think twice", quietly for "Looks fine" (not on a big marketplace's own
  listing), never for "Serious concerns" or when a deep check would add nothing. The quick verdict itself is
  unchanged; the API adds a `deep_check` field (#231).
* **Results read more easily**: the headline no longer repeats the verdict above it, and each reason shows what it
  means right under its title (#155).

### Reporting a wrong result

* **Quick results can be reported too** ("Wrong result?") when you check with a pass or wallet; the report says which
  tier it was (#229).
* **Report rewards in two steps**, to the first reporter and only after a person has labelled the page: 5 checks when
  the score was far off, 1 check when it was somewhat off; nothing for a correct result. Up to 15 reports a month.
  The review queue keeps the most useful reports first, and an EU-hosted AI model suggests a label for each queued
  report; a suggestion never labels, trains or pays until a person confirms it. Nothing about the reporter is sent to
  it. The report dialog and the privacy statement say so (#225).

### Accuracy and the scorer

* **How accurate is Unfret? (`/accuracy`)**: per verdict, how many pages got it and how many of those were really
  unreliable, false alarms on legitimate shops (EU and US), the method and the limits, from a replay of the labelled
  test set (aggregates only) (#180). The page shows the open baseline next to the newest retrained scorer Unfret runs,
  each named, with the one this server runs marked, and earlier versions folded below with their release dates; the
  data is in `accuracy-history.json` (#236, #244).
* **Fewer false alarms on well-known sellers**: a retailer selling a brand on its own site is no longer "seller isn't
  the brand"; a brand written in capitals is no longer "unknown" by itself; settings in a page's source no longer
  read as dropshipping apps by accident; "costs revealed later" only for fees on top of the price, not "shipping
  calculated at checkout". Replay on the labelled corpus: ROC AUC 0.897 → 0.903 (#224).
* **The scorer in this repository is the open baseline**; a server can run a scorer of its own
  (`UNFRET_MODEL_PATH`, `UNFRET_MODEL_QUICK_PATH`), checked at start-up. `/v1/health` names the scorer versions
  (#198).
* **Quick checks feed Unfret's photo, price and review records** in the background (the answer is unchanged), and
  every record is guarded against forged content: something seen only in content a client sent counts in a verdict
  once enough independent sources saw it, or when Unfret fetched the page itself. No link between a pass and a page
  is kept (#227). The same guard now covers the shop links Unfret records (shared IDs, addresses, other shops'
  identifiers): links that only a client sent need corroboration before they link another shop (#237).
* **Quick checks give the same answer whether Unfret fetched the page or the extension sent it**: the extension can now
  send the names of a page's classes, ids and data attributes (never their values) and the other sites it links to,
  so shop apps hidden in the markup count either way (#235).

### Checking pages

* **Shops that turn the server away say so in seconds**, and `/check` lists the big shops that only the extension
  and the Android app can read (a technical note, not a judgement). A product link that can't be read says to use
  the extension or the app, instead of "try the link to the product"; Zara's product links count as product links
  (#196, #216).
* **The product photo, not the header's logo**: when a product page's structured data names no photo, its Open Graph
  photo is taken as the main photo, so quick checks record the product's photo (#237).
* **On a phone, "Check a link" is the main button** on the home page and checkout (#196).
* **Second-hand ads and tradespeople's websites are checked in production too**, as a secondary feature (#157).

### Website and pages

* **A new home page: "Check before you pay."** A paste-a-link field in the hero, Unfret's real answer for the test
  shop beside it (with its sources), what Unfret checks, pricing with the free tier first (in euros for euro-area
  visitors), questions, then ads and tradespeople lower down. AI agents get their own way in. The main button fits
  the visitor's browser. The footer and the welcome page carry the same line (#156, #155, #207, #211, #236).
* **The tricks Unfret catches, as cards**, each with a made-up example; the "Spotted recently" showcase is retired
  and its routes answer 404 (#159).
* **A test shop at `/test-shop`** to demo and test against, with every trick Unfret catches, a sister shop selling
  the same product under another made-up brand, so a check shows "the same product, sold elsewhere", and a clean
  shop with none of the tricks (#161, #217, #236).
* **One Dutch page at `/nl`**, with iDEAL | Wero, euro prices and a Dutch footer (#207, #245).
* **New pages**: `/about`, `/money` (how Unfret makes money), `/press` (with Unfret's result for the test shop),
  `/terms`, `/objection` (for shop owners, with a request form) and `/why` (Unfret next to free scam checkers and AI
  assistants; a Fakespot alternative) (#176, #190, #218, #220). The terms describe the affiliate program in full
  (#202), name the company's details and the court for disputes (#230), and say that their fair-use rules cover the
  service, not the MIT-licensed code (#232). The terms are approved, so no page shows the "draft, not in force" note
  any more; the switch stays for future drafts (#239).
* **`/android`, the Android app's download page**: what the app does, the signed release with its checksums, install
  steps and permissions in plain words. Unlisted and noindex for now, and it says the download is coming soon until a
  signed release is published (#243).
* **`/sitemap.xml` and `/robots.txt`**: the public pages for search engines; the production site welcomes search
  engines and AI agents and points agents at `/llms.txt`, `/.well-known/x402` and `/openapi.json`; the dev site asks
  every crawler to stay out (#245, #247).
* **One look for every page**: one header and footer, a typeface served by the site itself, light and dark, phone
  width; `/extension` ("Get Unfret", now with the phone app) no longer scrolls sideways on a phone, and the
  backup-code field on `/my-checks` is wide enough (#155, #167, #162).
* **US dates everywhere** ("November 18, 2026"), on results, `/accuracy`, the privacy statement, the terms and
  `/android` (#236, #245).
* **The footer and README link the source of every app and its GitHub mirror**; the README opens with accuracy, the
  model note, how to run your own server and a demo (#175, #179, #185).

### Payments, packs and codes

* **Two packs: 25 checks for $6 and 100 checks for $20** by card (€5.50 / €17.50 in the euro area); the 50-pack is no
  longer sold. Checks already bought keep working (#197).
* **Card prices include VAT** where it applies, and the crypto price shows before a country is chosen (#155, #167).
* **Card and iDEAL | Wero first, crypto one step away**; checkout says who handles a card payment and that they ask for
  an email for the receipt; "Connect wallet" instead of "sign in" (#155, #167, #196). Card payments no longer stop
  when crypto payments are unavailable (#155).
* **Gift codes of 5 checks**, one per 25 checks bought, for any pass except the buyer's own; a pass or wallet redeems
  one a month (#183, #197).
* **Affiliates and bonus codes**: a `?ref=` link or a code at checkout gives extra checks on a first pack; affiliates
  earn a share and have a private stats page. Codes give checks, never a lower price (#183, #186).
* **Intro codes are retired** now that every new pass gets free checks: redeeming one says so, and an old intro-code
  link points to the free checks instead. They stay off unless the operator turns them back on (#183).
* **A cached result costs a check**, like a fresh one, except for the pass that already paid for it within the hour
  (#191).
* **A card purchase can be picked up in another browser** (common when a bank app brings you back elsewhere), only
  while its pass holds exactly that purchase (#155, #166).

### For AI agents and developers

* **An Unfret pass pays `/v1/check`** next to x402, and developer passes come with 20 free deep checks (#201).
* **A remote MCP server at `/mcp`** (streamable HTTP), paid from an Unfret pass; it never pays from a wallet (#203).
* **A Python client** (`unfret-client`): `check(url)` → allow, warn or block. It is not on PyPI yet; until then it
  installs from the repository. A TypeScript reference client is in the repository too, not published to npm (#204,
  #242). An example agent checks a shop with Unfret before checkout, against the test shop (#210).
* **`/developers`, complete**: calls in three lines, a real response, measured speed, limits, and a button that makes
  a developer pass (#155, #212). **`/developers/http`, "Use Unfret from any language"**: both ways to pay, the proof of
  work for a developer pass, the request and response, errors and retries, with examples in curl, JavaScript, Go and
  PHP (#242).
* **x402 directories can find `/v1/check`**: `openapi.json` marks it as paid, with a clearer description (#214), and
  a request without a payment gets the 402 with the price before its body is checked, so directory probes see the
  price (#219). The server can sign its own origin for x402scan's ownership check (#223).
* **x402 in USDC on Base, behind a switch that is off**: through a facilitator, with Bazaar discovery, a hot wallet
  swept to a cold address as on Solana, and every network listed in `/.well-known/x402` (#193, #194, #221, #228).
  Nothing changes while it is off.

### Privacy and security

* **The privacy page is complete**: what the site and extension store, server logs, the legal basis, your rights and
  how to complain, where processors work, how long records are kept. Contact: hello@unfret.net (#167). It names the
  controller's address (#230).
* **No request log at the proxy**: no visitor addresses or query strings are written (#215).
* **Page addresses lose sessions, tokens and e-mail addresses** like tracking parameters, the same in the server and
  the extension (#195).
* **Limits against abuse**: code redemptions and price quotes per visitor network (#169), uncharged analyses per
  account (#172), photo thumbnails (#170). Fetched pages are decoded within the size limit (#163).
* **x402's outflow audit**: changing the cold address no longer raises a false alarm, the audit survives restarts and
  a pass that fails half-way, and the operator sees why x402 is paused (#160, #174).

### Development

* CI checks a pull request once it is ready (no `WIP:` in the title); `main`'s runs queue and are never cancelled
  (#208).
* `make golden` measures false alarms on well-known sellers; `make train` writes retrained scorers outside the
  repository; `make accuracy` regenerates `/accuracy` and publishes a scorer's aggregates to its history (#180, #198,
  #224, #236, #244). `make android-release` writes the `/android` page's release facts from a signed app release
  (#243).
* Migrations 22 to 32: card pick-up records, x402 audit state, the showcase table dropped, free checks, affiliates
  and bonus codes, shop-owner requests, monthly gift-code limit, provenance on photo, price and review records,
  report values and suggestions, brands on seen photos, provenance on shop links.

## [0.12.0](https://github.com/kroshtan/unfret_backend/compare/0.11.1...0.12.0) (2026-11-16)

### Bug fixes

* **Verdict polish from real pages on dev** (corpus replay with URLs, `--split all`: the 1,582 product pages with the new classes off and on and the 879 p2p and service pages score exactly as before, ROC AUC 0.897 and 0.932, precision/recall at <50 0.838 / 0.804 and 0.938 / 0.756, p2p 1.0 / 0.476 with no legit ad under 50; one legit product page loses a "Hard-to-believe claims" note)
  * **Marktplaats and 2dehands ads can look fine** (with the new classes on): 10 of 15 real ads said "little to go on" next to an account of 2–20 years and pickup or the platform's payment. "Looks fine" now needs, with nothing against the ad and its text read, an account of two years or more with pickup or the platform's payment protection (or, well rated, a checked normal price); those platforms show no rating count, and a low or missing rating is its own reason. It is held back when the content read leans towards paying off the platform, even without a quote. 13 of the 15 real ads now look fine locally (the other two: no account shown, a phone number in the ad), none of the corpus's 147 unreliable ads does (all synthetic ads keep their band)
  * **"Same text in other ads"** no longer fires on its own for an account of two years or more: three bike ads from one 17-year account found each other. With any other reason, or on a younger account, it counts as before (the ad store keeps no seller to tell a seller's own ads from someone else's)
  * **An account's real age**: "on the platform for 17 years" instead of a capped "10 years"; the `seller_profile.account_days` feature keeps its ten-year cap
  * **"Text aimed at AI" on ads and service pages** only from the injection skill's quoted phrase, as on product pages; the content read's yes/no no longer counts
  * **"Little shop information" on a decade-old shop** (fonq.nl, 20 years) that shows its return policy is no longer said when its legal pages couldn't be read that time; and a **"Hard-to-believe claims"** note is not shown when it would be the only note on a page that looks fine from an established shop (address three years or more, or return policy and company details shown): mrmarvis.nl's "reduces microplastics by up to 90 %". The band comes from the scorer as before; in the corpus the claim showed on a "Looks fine" page only for legit pages (2, 0 unreliable)
  * **A service platform's page without the provider's profile isn't judged**: Houzz answers our server with real pages for some profiles but nothing of the provider for its `/professionals/` pages, which gave "a few things to check" with no reason at all. A link to such a page is now `PAGE_BLOCKED` before anything is charged ("check it in your browser with the Unfret extension"), and a profile page sent with nothing of the provider in it is not analysed (score -1, not charged) instead of "a few things to check" about nothing
* A town called Evergreen ("Evergreen, CO" in a Denver plumber's list of areas served) was read as an "evergreen" countdown script that restarts on reload, which gave Mr. Rooter's Denver page a high-severity "fake countdown"; only a timer named so counts now. A US address with a directional ("2500 SW 7th Ave", "4040 SE International Way … USA") no longer reads as a Dutch postcode, which made US firms "Dutch" and missing a KvK number
* **With the new page classes on, product pages are classified exactly as with them off.** The LLM classification of a page the rules leave open now always starts with the unchanged product prompt; only a page that step would not analyse (an article or "not a product"), or a "shop" with nothing to buy (no price, product data, basket or checkout) that names a trade service, gets a second, separate question about the service classes, and the answer counts only with evidence on the page (a trade such as plumber or loodgieter, a licence, call-out terms, towns served, service structured data). A peer-to-peer ad now comes only from the platform table (no more "a private seller on eBay" for a shop that isn't on eBay), and a provider profile only on a service platform. On the 1,256 labelled product pages, no page changes class with the flag on (in one of three runs, a scam chargeback-recovery "service" went to `service_site`), against 53 before (13 early exits that let 11 scams escape, 2 shops as eBay ads); no product page becomes an early exit or an ad. Peer-to-peer and service-listing pages classify as before; service sites 275 of 311 (297 before), the rest being analysed as shops
* **Marktplaats and 2dehands ads are read from the ad's own JSON** (`window.__CONFIG__`), which the server-fetched page carries while its seller box and deal area are built in the browser: bidding, pickup or shipping, the platform's payment protection, the date placed, the town, the category, the price and the whole gallery, and the account (private or business, how long on the platform: "4 maanden", "11 jaar"). Most real ads used to come back "little to go on" with no account and no delivery at all. The JSON names the seller and links their profile (and has the seller's coordinates): only the listed fields are copied out of it, so no name, id, profile link or coordinates are ever kept
* **Fewer false warnings on real ads**: a rental deposit "due at lease signing after a viewing" (or "bij ondertekening", "na de bezichtiging") is no longer payment up front, while a deposit before any viewing, wired, or for keys sent by post still is; a bank transfer offered next to cash, card, PIN or the platform's own payment is no longer a warning unless something else about the payment is risky (shipping only, the seller abroad, a deposit, untraceable methods, the platform's payment refused); and the LLM's "moves the deal off the platform" counts only when its quote names a way off (WhatsApp, a phone number, e-mail, a payment method, shipping only, abroad), so "neem contact op voor specifieke prijzen" (ask through the platform) no longer does
* **A domain that no longer resolves gets "The site couldn't be reached"** (`PAGE_UNAVAILABLE`) instead of "That isn't a public web address" (`INVALID_URL`), which stays for addresses that resolve to private or internal networks
* The synthetic Dutch service sites of the label collection (`evals.sources.service_pages`) now show VAT numbers with a valid check digit; the 17 corpus pages that had one failing it were corrected
* **Fewer false warnings on established shops' product pages**, from real pages checked on dev (ikea.nl, bever.nl, fonq.nl, mrmarvis.nl, hema.nl). Corpus replay (1,256 pages): ROC AUC 0.897 → 0.897, precision/recall at <50 unchanged (0.843 / 0.804, 101 legit pages under 50), only 5 scores move (4 legit up, one scam 9 → 29); on legit pages "No company details" as a reason drops from 178 to 11 and "Unknown brand" from 29 to 4, on unreliable ones "Unknown brand" stays at 184
  * **"No company details"** (`no_imprint`): established shops keep their company details on a contact or legal page, not on each product page. When an EU shop's page shows no imprint but links to its contact page or terms on its own site, `eu_registry` reads those (at most two, cached per shop domain, not in evaluations or the quick tier; new `legal_pages_checked` feature): a register number (KvK, Handelsregister, …) or a VAT number there counts as the imprint, so the score gets the same credit as an imprint on the page. The reason is said only when those pages were read and showed nothing, or the page links to none ("found on the page or on the shop's contact and terms pages"), never for a VAT number the EU register knows or a shop whose domain is ten years old or that is the brand's own site with the brand on Wikidata (ikea.nl renders its legal pages with scripts). A "Bedrijfsgegevens" or "Company details" link is an imprint page (mrmarvis.nl)
  * **"Reviews about something else"** (`off_topic_reviews`): the similarity threshold (0.35 against the product title) was calibrated on marketplace reviews, where pooling happens; a shop's own short Dutch reviews about fabric and washing sit at 0.3–0.5 (ikea.nl: 40% "off topic", all on topic). On a shop's own page a review now counts only when it is also 0.1 below the page's median review (marketplace listings unchanged), the reason needs half the reviews there (0.3 on marketplaces), and it is medium instead of high
  * **"Unknown brand"** (`generic_brand`): a name that merely looks generated ("HKLIVING") is no longer called unknown when the content read knows the brand, Wikidata has it, or its own `.com`/`.nl` domain is at least five years old (hkliving.com, 2008); outside evidence like that also lets "Known brand" show
  * **"Text aimed at AI"** (`ai_text`): only from the quoted phrase the injection skill finds; the content read's own yes/no fired on as many legit pages as unreliable ones in the corpus (9 vs 8; bever.nl's cookie banner)
  * **"Registered for a year"** (`short_registration`) next to "existed for 29 years": a registry that publishes no expiry date (.nl) gave a term of 0, which read as "one year". The reason now needs a known term of at most a year on a domain younger than three years, and says so: "New, short registration: registered less than a year ago, and only for one year"
  * **"Little shop information" next to "Shop details present"** (mrmarvis.nl): the deterministic check wins. When the page's legal information shows a return policy and company details, the content read's "little shop information" isn't said; the two are now mutually exclusive
* **bol.com seller names without their rating label**: "Verkoop door Light Gallery NL beoordeeld met een" is now "Light Gallery NL" (label and rating dropped; the page text only as a fallback when the offer's JSON-LD names no seller), in parity with the extension
* **`make codes list` counts only buyers who kept their purchase**: an account whose card orders were all refunded in full or disputed is no longer an "organic buyer" or "via code, bought" (crypto purchases always count); the list says what a buyer is

### Features

* **A new key signs the browser extension in by itself.** Of three people who redeemed an intro code on the website, none used a check: after getting a key they had to open the extension, choose "API key" and paste it. Now every new key `/checkout` shows ("I have a code" → "Get my free checks", the card payment's return, "Lost your key?" recovery, the crypto tab's "Make a new key") is offered to the Unfret extension in the same browser (`static/handoff.js`, `window.postMessage` to the page itself, relayed by the extension's content script on `/checkout*` only, from extension 0.11.0). Signed in: "✓ Your Unfret extension is signed in with this key — you can start checking pages now." No answer within 2 s: "Don't have the extension yet? Add Unfret to Chrome or Firefox" with the store links, and "choose API key and paste your key". Signed in with another key or a wallet: "Switch it to this new key?", and nothing changes unless the user agrees. The key goes only from the page to the extension; the answer is a status, never a key or account. The key and recovery code stay on screen with "I've saved my key", as before; `/check` still signs itself in with the new key
* **`/welcome`**, the page the extension opens once after a new install: "Shop without the worry", then three ways in: "I have a code" (`/checkout?mode=code`), "Buy checks" (`/checkout`) and "I already have a key" (how to sign the extension in). Static, no script, the site's design in light and dark
* **"Top up my key" with the key filled in.** `/checkout?mode=topup&key_id=<key ID>` opens the top-up tab showing "Topping up key abcdefghij · use another key", ready to pay. Only the 10-letter public key ID goes in the link, never the key. `/check`'s "Buy checks" and, out of checks, "Pay by card" link there for someone signed in with a key (they used to open the tab that makes a new key; "Pay with crypto" opens the crypto tab); the extension (0.11.0) does the same
* **Service providers: verdicts that tell rogue traders from real firms (behind `UNFRET_EXPANSION_CLASSES`).** Measured on the labelled corpus (replay, with URLs): on **real** archived sites (95 pages of 20 ACM-named Dutch locksmith and plumber lead sites, 166 real firms) "some" or "many warning signs" now covers 83 of the 95 rogue pages (was 9) and 19 of the 20 sites (was 2), still with no warning on any of the 166 firms; on platform profiles (279 synthetic) every scam profile got "a few things to check" like every honest one, now 136 of 150 get a warning and all 129 established profiles "looks fine" (ROC AUC 0.5 → 1.0). New derived signals, no register lookups: a site named after nothing but a trade and a town the page says it works in (`zwolle-slotenmakers.nl`, `loodgieters.nl`) that doesn't say which company it is; the page saying its tradesperson "isn't necessarily based in" the town; only national phone numbers (085/088, 0800, 0900, US toll-free) on a page about a town or with a Dutch number rented anywhere; an address in another town than the one the page is about; company details "on request"; another unrelated site giving the same postal address (the fingerprint store, twice or more); and on a platform a profile without a track record (under six months, under five reviews) and a low rating over ten or more reviews. "Some warning signs" for the anonymous local site (a trade-and-town name or only national numbers, with no KvK number and no street address, or in the US no street address, licence number or legal name) and for a profile without a track record that makes emergency, call-out, payment or pricing promises; "many warning signs" when the not-local statement comes with another mark of a call centre. Copy in `copy/en.json`, factual and about the page
* **Platform profiles read as the platforms write them**: "4.5 ★ uit 160 beoordelingen" was read as a rating of 1 from 60 reviews (the rating is no longer the first digit of the count); "★ (350 reviews)", "4.9 ★★★★★ (143)", "312 verified reviews", "Years in Business: 15", "Responds within about 2 hours", "Reageert meestal binnen 2 uur", "356 klussen via Werkspot" and "KvK-nummer gecontroleerd" are read; "No reviews yet" / "Nog geen beoordelingen" counts as zero reviews. **HomeGuide** joins the platform table (`PageFacts.platform` `homeguide`; a pro's profile `/<state>/<city>/<trade>/<pro>` is a `service_listing`), with Houzz's "Best of Houzz" and "Verified License" badges. Houzz `/professionals/` pages answer a server with Fastly's "Client Challenge", now recognised as a blocked page (`PAGE_BLOCKED`, check it with the extension) instead of being judged as an empty profile
* **`page_type` in the `/v1/analyze` response** (and the stream's `result`), the same value the stream's classify step carries, so a client of the plain endpoint knows which kind of page was checked

### Changed

* **The intro-code daily cap is an emergency brake, off by default**: codes already have their own max uses, so `UNFRET_INTRO_DAILY_CHECKS_CAP` (was 250) now applies only when set (0 still pauses every code). `make codes list` says "no daily cap" when it's off, and `/v1/admin/codes` returns `today.cap: null`
* **The product corpus is less lopsided; the scorer stays as shipped.** The labelled storefronts were 92.5% scams when US and 19.6% elsewhere, and their scams came mostly from one dataset, so "a US shop" and "this source" predicted the label by themselves. Two new collectors (`make balance`): `evals.sources.us_shops` takes archived product pages of long-established US shops (Wikidata and OpenStreetMap seeds, Internet Archive captures 2021–mid 2025 of shops first archived at least five years earlier, US-market product pages only, Shopify capped at 40%), `evals.sources.coi_shops` the shops on the Czech Trade Inspection Authority's open-data list of risky e-shops, captured within the period of the warning. The archive's redirects find the captures (`wayback.fetch_closest`, `earliest_capture`), since its index took 40 s a query. Both datasets are strict in `evals.second_opinion` (`STRICT_DATASETS`, `--dataset`): a page keeps its label only when the labeller confidently agrees. 168 of 234 US pages and 158 of 200 ČOI pages were kept (1,108 new recorded LLM responses). Each origin dataset is now its own split stratum, which moved no existing page between train and test. The leakage test adds the label AUC of the source or dataset alone: "a US shop" now predicts the label with AUC 0.654 (was 0.866), the source alone 0.771 (was 0.844). A retrain with the constrained procedure is better on US shops (TEST AUC 0.897 vs 0.862, F1 at <50 0.845 vs 0.811, 9 vs 10 legit warned of 62) and overall by AUC (0.906 vs 0.896) and Brier (0.124 vs 0.135), but warns about more legit pages (37 vs 28 of 220, all of the increase on non-US shops) at lower precision (0.840 vs 0.863), so `model.json` and `model-quick.json` are not replaced. `looks_like_shop` reads Czech prices and carts

## [0.11.1](https://github.com/kroshtan/unfret_backend/compare/0.11.0...0.11.1) (2026-11-16)

### Bug fixes

* Fewer false warnings on service companies' sites, found by checking real sites on dev: a street address written out in the footer or contact block (several branches, "12111 Menchaca Rd Austin, Texas 78748", "Tingietersweg 129 2031 ER Haarlem") now counts, so an established firm with many branch pages is no longer "pages for many towns, no address"; a licence number with a dot ("License #MP.00188386") counts as shown; and the payment and call-out checks no longer read the customers' testimonials ("no call-out fee, he was there in 20 minutes" is a customer's story, not the company's terms). On a private seller's ad, "bought abroad" is no longer read as "the seller is abroad"

### Features

* **The country checkout uses is shown and can be changed.** The card currency and the iDEAL | Wero button no longer change silently with the visitor's location. "Buy checks" and "Top up my key" now say "Paying from: Netherlands (change)" above the pay buttons (Cloudflare's country hint, or "Choose your country" without one); "change" opens a country list, and the chosen country decides the card prices (euros in the euro area, dollars elsewhere) and whether a local method's button shows (Netherlands: "Pay €5.50 with iDEAL | Wero"). The choice lives only in the page (not stored) and also starts the crypto tab's VAT country. The home page's price tiles say why they are in euros ("Card prices in euros for the Netherlands · change at checkout"), and the privacy page explains where the country comes from and that it is never stored. API: `/v1/pricing` adds `euro_area` (the countries priced in euros) and `local_payment_methods` (`{country: {id, label, currency}}`); `card_currency` and `local_payment_method` stay, as the values for the suggested country
* **"Pay with iDEAL | Wero" as its own button for Dutch visitors.** Polar shows iDEAL | Wero only when the buyer's billing country is the Netherlands, and with no country it shows cards only, so Dutch buyers rarely saw it. `/checkout` now gives Dutch visitors two buttons in "Buy checks" and "Top up my key": "Pay €5.50 with iDEAL | Wero" (first) and "Pay €5.50 by card", priced for the selected pack; it replaces the single "Pay … by card or iDEAL | Wero" label. Everyone else keeps the one card button. The Wero checkout is a EUR checkout with `customer_billing_address: {"country": "NL"}` prefilled; metadata, claim, crediting and refunds are unchanged. Table-driven so more countries can follow with one entry each (`unfret.credits.local_methods.LOCAL_METHODS`, after checking at Polar that the method really shows for that country). API: `/v1/pricing` adds `local_payment_method` (`{id, label, currency}` or `null`); `POST /v1/credits/fiat-checkout` takes an optional `payment_method` (`card` | `wero`; `wero` is a 400 while euro prices are off or with `currency: "usd"`) and answers with the checkout's `payment_method` (`card` when Polar refused the euro checkout and the dollar one was made instead).

### Changed

* **dev.unfret.net runs with the new page classes on** (`UNFRET_EXPANSION_CLASSES: "true"` in `config/deploy.dev.yml`), so peer-to-peer ads and service providers get their own checks and verdicts there; production keeps them off
* **Scorer training (`evals.train`) guards against learning the corpus instead of shops; the shipped scorers are unchanged.** A retrain on today's labels and features had looked much better (TEST AUC 0.951–0.953 against the shipped 0.908) only through a leak: two columns set on one single training page (`listing.seller_not_brand`, `us_identity.mailbox_only`) got coefficients of +68 and −68, and because a column's never-measured pages train at its mean, the pair rebuilt an indicator of "a US shop" (`us_identity` applies), which alone separates the corpus's storefronts with AUC 0.866 (92.5% of its US storefronts are scams, 19.6% of the others). The fit now keeps each coefficient on the side of zero its feature can mean (`FEATURE_SIGNS`: a timer, hidden characters or fee wording never make a page safer, a named company or an imprint never makes it riskier), gives no weight to a feature seen on fewer than five training pages, and picks the L2 strength by the held-out balanced log loss (a sign-bounded L-BFGS fit instead of `LogisticRegressionCV` by AUC). The largest coefficient is now 1.80 (`listing.discount_pct`). A **source-prediction leakage test** runs with every training and goes into the report: how well the features tell the corpus sources apart within one label (legit live vs archived: AUC 0.70; scam dataset vs archived: 0.83, mostly `listing.is_shopify` and `eu_registry.withdrawal_right`), the label AUC of each skill merely applying (`us_identity` 0.866), and the scorer's TEST AUC within the one source that holds both labels (archived shops: 0.900; the shipped model 0.913). Without the leak the retrained variant C is not better than the shipped model on TEST (AUC 0.898 vs 0.908, precision at <50 0.867 vs 0.877, recall 0.753 vs 0.747, F1 0.806 vs 0.807, Brier 0.129 vs 0.127, legit pages warned about 22 vs 20 of 170), and the quick model retrained the same way is worse (AUC 0.664 vs 0.739), so neither `model.json` nor `model-quick.json` is replaced

## [0.11.0](https://github.com/kroshtan/unfret_backend/compare/0.10.5...0.11.0) (2026-11-15)

### Features

* **Card payments in euros, with iDEAL | Wero for Dutch buyers.** Visitors from the euro area (Cloudflare's country hint; the 21 euro countries) see and pay the card prices in euros: €5.50 / €10.00 / €19.00 for 25 / 50 / 100 checks (`UNFRET_CARD_PRICES_EUR`, one per pack, before VAT like the dollar prices; empty: dollars for everyone). Their Polar checkout is in EUR, which is what makes Polar offer iDEAL | Wero in the Netherlands, and for Dutch visitors the button reads "Pay €5.50 by card or iDEAL | Wero". Everyone else stays on dollars; `POST /v1/credits/fiat-checkout` takes an optional `currency` (`usd` | `eur`, which the checkout page sends) and answers with the checkout's `currency`. A euro order credits the same checks and invites as the dollar pack, only at exactly the pack's euro price; refunds and lost disputes take credits back pro rata as for dollars; other currencies stay refused. `/v1/pricing` adds `price_eur`/`price_eur_cents` per pack, `card_currency`, `fiat.currencies` and `fiat.min_eur_cents`; the home page's price tiles show euros to euro-area visitors too. Migration 21 adds `fiat_payments.currency` (`usd` for earlier orders). Each Polar product needs a EUR price equal to its pack's; until it has one, a euro checkout falls back to the dollar one rather than failing
* **Foundation for new kinds of pages, behind `UNFRET_EXPANSION_CLASSES` (default off).** Three page types join `aggregate_listing` and `storefront`: `p2p_listing` (a private seller's ad on Marktplaats, 2dehands, Vinted, Craigslist, Facebook Marketplace, or a private seller on eBay), `service_listing` (a service provider's profile on Werkspot, Thumbtack, Angi, HomeAdvisor, Checkatrade, …) and `service_site` (a service company's own website). Classification stays rules first: a host and path table of known platforms settles their listing and profile pages without the LLM (an eBay listing whose seller the page labels a business stays a marketplace listing), their other pages go to the LLM instead of counting as a shop, structured data naming a trade (`Plumber`, `Locksmith`, …) makes a service site, and the LLM's prompt names the new types. With the flag off, classification is exactly as before (same rules, same prompt) and such pages are treated as they always were
* **One LangGraph subgraph per page class** behind the shared preprocess and classify steps: product (today's pipeline: cheap skills, triage, reverse image, judge), p2p and service. The new classes' judge is an interim one: never "looks fine", "a few things to check" (or "some warning signs" when a skill found something serious), with a headline that says the checks are new; the class-specific judges replace it. Existing skills now declare the new classes where they clearly apply (content and injection everywhere, reverse image when asked for; domain age, EU registry, US identity, shop links and dark patterns on service sites). `POST /v1/analyze/stream` streams every class's skill steps
* **Retries on the nodes that call the LLM or fetch outside data**, with LangGraph's own RetryPolicy: a dropped connection, a provider 5xx or a rate limit gets `UNFRET_GRAPH_RETRY_ATTEMPTS` attempts (default 2) before a skill counts as unavailable. langgraph, langchain-core and langchain-openai are pinned to their minor release, and LangSmith tracing is forced off (no hosted LangChain service)
* **Business facts in `PageFacts`** (all optional): public product IDs (GTIN/EAN, MPN, ASIN), payment methods mentioned, published phone numbers (typed: mobile, local, national, toll-free, premium for NL and US) and e-mail addresses (own domain, webmail), addresses, "since 1998" claims, business name and legal form, KvK numbers, US licence numbers with their state, certification, pricing-term and response-time snippets, cities served, same-site links, Organization/LocalBusiness JSON-LD; and, for the platform extractors to fill, `seller_profile` (member since, rating, badges, jobs, response rate: never a name) and `listing` (a peer-to-peer ad's condition, delivery, payment protection, contact methods and payment requests). Contacts and addresses come only from what the business publishes (structured data, `tel:`/`mailto:` links, the footer, contact, legal and imprint sections), never from forms, account menus, checkout, billing or delivery areas. `PageFacts.platform` recognises the peer-to-peer and service platforms; on a peer-to-peer platform the seller's name is dropped (also from the offer's structured data), and only product IDs and payment methods are read as business facts there

* **Product-page upgrades: seven new checks, reasons and positives for shoppers; scores unchanged.** Each reports new features that the shipped scorer ignores until it is retrained (the corpus replay scores every page exactly as before), and reaches the shopper as a reason or positive only on concrete evidence:
  * **Payment methods** (`payment_methods`, storefronts): only payments that can't be reversed (bank transfer, crypto, Western Union, MoneyGram) and no card, PayPal, iDEAL, Klarna or other protected method in the text, logo alt texts or a payment provider's script → "Only payments you can't get back" (high); Shopify shops and gift cards alone never. Protected methods → positive "Payment with buyer protection". On the corpus it fires on 11 pages, all scams
  * **Claims vs facts**: "since 1998" / "founded 20 years ago" about the shop itself on a domain younger than a year by both its registration and its first certificate (`domain_age`) → "Says it's long-established, but the site is new"; "a Dutch company", "family business in Amsterdam" while every address given is in another country, and "visit our showroom" with no address, postcode or phone anywhere (`claims`) → "Not where it says it is", "Mentions a store, gives no address". The evidence quotes the claim and states the fact
  * **Price vs market** (`market_price`): a product with a barcode at 40% or less of the median price at least three other shops charged for it in the last 90 days, from a new anonymous store (migration 18, `market_prices`: SHA-256 of the barcode and of the shop's domain, the day and the price; 90 days) → "Far below the usual price"
  * **Cross-shop reviews** (`review_copies`): the same review texts on other shops, from a new review fingerprint store (migration 18, `review_fingerprints`: SHA-256 of the normalised text and of the shop's domain, first and last day; no text, reviewer or URL; 90 days) → "Reviews copied from other shops", for an unknown brand with other resale signs only
  * **Shared addresses** (`shop_links.shared_address_shops`): the shop's postal address, normalised (a Dutch one as postcode and house number) and stored as a keyed hash like contacts (180 days), counted on other unrelated shops; never used to link shops. Five or more → "Address shared by many shops" (low). `POST /v1/admin/contacts/forget` takes `kind: "address"`
  * **Brand web presence** (`brand_presence`): the brand on Wikidata (a business or brand item) and its own `.com`/`.nl` in RDAP, cached as store-level facts. Wikidata also gives the "Known brand" positive; no presence anywhere → "A brand with no presence of its own", on marketplace listings with other white-label signs only
  * **EU Safety Gate** (`safety_gate`): the product in the EU's alerts on dangerous products, by barcode or by brand and whole model number, from a vendored index of the Commission's open data (CC0; `make safety-gate`, checked monthly by the "Vendored data" workflow) → "On the EU list of dangerous products" with the alert's reference, date, risk and link
* The privacy page describes the new stores (prices across shops, review fingerprints, hashed postal addresses) and the Wikidata lookup
* **Service providers, behind `UNFRET_EXPANSION_CLASSES` (default off): a full verdict for a service company's own site (`service_site`) and a provider's platform profile (`service_listing`), for the Netherlands and the US.** No register is looked up (no KvK API, Companies House or state licence boards): the signals are derived from the page, the shop fingerprint store and the photo index, plus VIES for a VAT number. New skills: `service_identity` (NL: KvK number shown and eight digits, VAT number, company name, legal form, street address; US: company name, street address, and "says licensed but shows no licence number"; two different company names on one site), `doorway_pages` (landing pages for five or more towns, "Loodgieter Utrecht" or "Plumber Austin", with no street address and often only a central number), `service_terms` (payment in full or a deposit of half or more before the work, cash only, a call-out "from €0/$0" or next to nothing, a price set only on the doorstep, prices only on request, "24/7, there in 20 minutes", a premium-rate 0900/1-900 number; a written quote or fixed price as the positive), `testimonials` (copies and templates with a town or name swapped, all 5 stars, repeated or placeholder names, dates within a week; on a platform, its reviews) and `platform_profile` (member since, jobs, rating and count, badges, response rate). `shop_links` now records service sites like shops and links them on their published phone numbers (national formats too) and their e-mail domain (not webmail), hashed like other contacts (migration 19), so one call centre behind many "local" brands shows up; its findings speak of sites. The service triage runs the photo search (on the first photo that isn't a logo) when the cheap skills found something it would confirm. The judge's hand-written rules (`graph/services.py`): "Many warning signs" for doorway pages plus links to other sites, a premium-rate number plus payment up front or cash only, copied photos plus doorway pages, links or a brand-new site, or three serious signals; "Some warning signs" for one serious or two medium ones; "Looks fine" only with an established domain, a complete identity, a street address, no doorway pattern and no upfront or cash-only terms (on a platform: two years and twenty reviews averaging four stars); "A few things to check" otherwise, and whenever all there is is something missing. Copy in `copy/en.json` (factual, about a business, never "scam"); the headline is the catalogue's template, no LLM. Service results carry no share token
* **Testimonials and platform profiles in the page facts.** `PageFacts.testimonials` holds `{text, name, rating, posted}` from the company's structured data (its `review`s) or, failing that, elements marked as one testimonial or review (and Review microdata), never from forms or account areas, and not on a product page (its reviews are in `reviews`); the name only as the site prints it. On Werkspot, Thumbtack, Angi and the other service platforms, `seller_profile` (with the new `years_in_business`) is read from the profile's structured data and the platform's wording outside the visitor's areas, and the platform's reviews go into `reviews` without names
* **Peer-to-peer ads get their own checks and a full verdict (still behind `UNFRET_EXPANSION_CLASSES`, default off).** Platform extractors for Marktplaats, 2dehands, eBay, Vinted, Craigslist and Facebook Marketplace fill `listing` (price handling, condition, category, listing date, location, delivery, platform payment protection, the contact methods and payment requests the ad text mentions, and the ad's own text as `listing.description`) and `seller_profile` (seller type, member since, rating and count, badges, other ads, reply time; never the name), from a few page areas matched by wording in Dutch, English, German and French. Four new skills: `seller_profile` (new account, no or low ratings, many ads on a young account, slow replies), `off_platform` (WhatsApp, phone or e-mail in the ad, bank transfer, gift cards, crypto, payment apps, deposits, "shipping only", "I'm abroad", refusing the platform's payment; quoted from the ad, rules first, with the content read's own judgement for ads), `price_outlier` (the same product new from the price store, else conservative floors for the goods bait ads use most and for rentals) and `duplicate_ads` (the same ad text or phone number in other ads of the last 90 days). The photo check runs for every ad with a photo and compares it with other ads too. The verdict comes from cautious hand-written rules (`unfret.shopper_p2p`): "Looks fine" only with concrete positives and nothing worrying, "many warning signs" only for strong combinations, plain-English reasons and positives in `copy/en.json`
* **The ad store** (migration 20, `unfret.ad_store`): per ad only a simhash of its words, the platform, a hash of the listing and the day; contact details written in an ad only as a keyed hash; kept 90 days, a contact removable with `POST /v1/admin/contacts/forget`. Photos of ads in the image index are kept 90 days (shops' 400) and never match a shop's photo. An ad naming a barcode is compared with the shop prices in `market_prices` (ads are never recorded there)
* The privacy statement says what a check reads on a private seller's ad (never the seller's name) and what the ad fingerprints keep

### Changed

* Pages on the recognised peer-to-peer and service platforms (eBay among them) are never recorded or looked up in the shop fingerprint store as a web shop, like Amazon's

* **Unfret is on Firefox too.** The home page (hero and closing section) and `/extension` now offer "Add to Chrome" and "Add to Firefox" side by side, with "Check a link instead" as the quieter option; `/extension` says which browsers each store serves (Chrome Web Store: Chrome, Brave, Edge, Vivaldi, Opera; Firefox Add-ons: Firefox) and no longer says Firefox is on its way. The Firefox link comes from `UNFRET_FIREFOX_STORE_URL` (default the Unfret listing on addons.mozilla.org), filled in like the Chrome one

### Development

* **Automatic labels for the new page classes** (`p2p_listing`, `service_site`, `service_listing`), so their verdicts can be measured and later trained, without hand labelling. New collectors in `evals/sources/`: `patterns` (scam and normal patterns, each tied to the public pages that document it: Fraudehelpdesk, the Marktplaats safety centre, ACM, Consumentenbond, Kassa, FTC; fetched with robots.txt respected to confirm them, only the verdict is kept), `p2p_ads` (synthetic ads for Marktplaats, 2dehands, eBay private sellers, Vinted, Craigslist and Facebook Marketplace built from the confirmed patterns, since those platforms' robots.txt or terms forbid collecting real ads; never a seller's name), `service_pages` (synthetic service sites and Werkspot/Thumbtack profiles, NL and US), `acm_traders` (the rogue locksmith and plumber sites named in ACM's KeyService, Allfree and Leadle decisions, from the Internet Archive) and `osm_trades` (trade businesses on OpenStreetMap whose domain the Internet Archive has seen since before 2015, their 2020 home page). Synthetic pages say so in `source` and `notes`. `make new-classes` runs them all
* The second opinion has a rubric per new class and is strict there: a page keeps its label only when the labeller confidently agrees, anything else drops the label with the reason in `notes` (`reports/<class>/second_opinion.json` counts kept and dropped pages); nothing of theirs is queued for a human. `--class` limits a run to one class
* `evals.run --class p2p_listing service_site …` scores the new classes with `UNFRET_EXPANSION_CLASSES` on and reports metrics per class under `reports/<classes>/`. Product runs, metrics, reports, training (`evals.train`, `evals.markets`, `evals.us_market`) and the product train/test split only ever see product pages; the new classes have their own split strata

## [0.10.5](https://github.com/kroshtan/unfret_backend/compare/0.10.4...0.10.5) (2026-11-15)

### Bug fixes

* Hidden buttons stayed visible: the new button style (`display: inline-flex`) beat the `hidden` attribute, so checkout showed "Pay in your wallet app" above a card or code result. `[hidden]` now always hides

### Changed

* **Buy checks is four short paths instead of one long form.** Tabs: **Buy checks** (pick a pack, one "Pay $6.00 by card" button: the key is made just before the card checkout and shown after paying, with Copy buttons and how to use it), **I have a code** (one field and "Get my free checks": a new key is made and the invite or intro code redeemed in one click), **Top up my key** (paste the key, pay by card; "Lost your key?" with the recovery code lives here) and **Pay with crypto** (the wallet/key, SOL/USDC, country, wallet and QR flow as before). Before, a new buyer had to make a key, save two codes and tick a box before "Pay by card" even turned on, and a code took six steps. A key made on the page waits in the browser until its owner confirms saving it (so the card provider's round trip or a closed tab can't lose it) and signs in `/check` if nothing else is. `?mode=new|code|topup|crypto` picks a tab; `?code=` opens the code tab, `?for=wallet` (or a wallet signed in) the crypto tab

### Bug fixes

* **Pages always get the stylesheet and scripts of their own build**: every page now names its static files under a folder for the running commit (`/static/v-<commit>/site.css`), which the static server ignores when it finds the file, so each deploy uses addresses no cache has seen. Cloudflare overrides the `no-cache` header from 0.10.4 (a Cache Rule or Browser Cache TTL keeps `/static/` for 4 hours), and 0.10.4's new price tiles came out with 0.10.3's styles. Files under a build folder are served as immutable; the old addresses still work and are revalidated

## [0.10.4](https://github.com/kroshtan/unfret_backend/compare/0.10.3...0.10.4) (2026-11-15)

### Changed

* Intro codes give at most 250 free checks a UTC day across all codes (`UNFRET_INTRO_DAILY_CHECKS_CAP`, was 100): 25 people a day at 10 checks each

### Changed

* The home page's price tiles are links: each opens checkout with that pack selected (`/checkout?checks=25`), with a "Choose →" line and a hover state

### Bug fixes

* **A deploy no longer shows a new page with an old stylesheet**: files under `/static/` are now served with `Cache-Control: no-cache`, so Cloudflare and browsers check with the server before reusing them (an unchanged file is still reused after a quick ETag check). Before, Cloudflare kept them for up to 4 hours, and 0.10.3's new home page came out with 0.10.2's styles

## [0.10.3](https://github.com/kroshtan/unfret_backend/compare/0.10.2...0.10.3) (2026-11-15)

### Changed

* **A fresh, calmer website.** One design for every page (`site.css` rewritten: mint-tinted surfaces, deep green ink, soft result tones instead of alarm colours, light and dark, phone first, still system fonts under the same strict CSP), with a shared header (Check a link · Buy checks · Sign in · Get Unfret) and footer on every page. The home page is new: "Shop without the worry", the product drawn in HTML (a shop page with Unfret's "Looks fine" answer, no images), how it works in three steps, what Unfret looks at for you, live pack prices from `/v1/pricing` (card first, crypto as the discount), an intro-code box that opens checkout with the code filled in, privacy promises, "Checked by us recently", and a section for developers and agents (new `home.js`; the home page now gets the store link from `UNFRET_CHROME_STORE_URL` like `/extension`). `/extension` shows the answer and how to get started after installing; checkout's "Pay by card" is the primary button with "Or with crypto" below it

## [0.10.2](https://github.com/kroshtan/unfret_backend/compare/0.10.1...0.10.2) (2026-11-14)

### Bug fixes

* **A "Looks fine" result with a note no longer contradicts itself**: it showed "No warning signs: Nothing suspicious found" (the fallback for a reliable page with no positives) right next to its note, e.g. "Hard-to-believe claims". That fallback now appears only when there is nothing to note, and the template headline names the note ("Looks fine, but one thing to note: hard-to-believe claims.") instead of "Nothing worrying stood out on this page."

### Development

* The evals find the private corpus next to this checkout (`../unfret-corpus`) instead of at a fixed `~/repos/unfret-corpus`; `UNFRET_CORPUS_DIR` still overrides it. `.gitignore` also covers `venv/`, `node_modules/` and `coverage/`

### Features

* **Intro codes**: named codes per channel (`FRIENDS`, `DEV`, one per newsletter) worth 5 checks each to a new account, usable a set number of times, redeemed wherever invites are ("Have an invite or intro code?" on checkout, sign-in and /check). Same rule as an invite: new accounts only, one invite or code per account, the same `invite_redeemed` flag and never which code. All codes together give at most `UNFRET_INTRO_DAILY_CHECKS_CAP` checks a UTC day (default 100; then "back tomorrow"). The operator makes and watches them with `make codes` (`list`, `create CODE --uses N`, `off`, `on`, `add CODE N`, `link CODE`), and the list counts accounts that came through an invite or code, how many of those bought, and organic buyers (new `wallets.purchased` flag; card buyers backfilled, earlier crypto buyers can't be). Share links: `unfret.net/key?code=FRIENDS` opens checkout for a new key with the code filled in

### Changed

* Card and API keys come first across the site: `/check` lists "Use an API key" above "Solana wallet" and names the key first for invites and intro codes, and the home page says "pay by card, or a little less with crypto" and points invites and intro codes to a new API key at checkout first
* `/checkout` puts the API key first and picks it by default (a wallet when the link says `?for=wallet` or a wallet is signed in on the page), and "Pay by card" comes first under "3. Pay", above the crypto options ("Or with crypto"); the card option hides while a crypto payment is under way

## [0.10.1](https://github.com/kroshtan/unfret_backend/compare/0.10.0...0.10.1) (2026-11-14)

### Bug fixes

* **unfret-mcp 0.1.1**: a paid check whose analysis failed reported `status: "ok"` with `ok: false`; it is now `status: "refunded"`, and `ok` always means a verdict

### Changed

* A paid check whose analysis fails now logs where it failed (`where`: our own innermost frames and the frame that raised, as file:line:function) and, for an LLM provider error, its status, code, type and message with URLs removed; never the exception's own message, which can carry the analysed URL

## [0.10.0](https://github.com/kroshtan/unfret_backend/compare/0.9.3...0.10.0) (2026-11-12)

### Features

* **The MCP server is ready for publishing** as `unfret-mcp` on PyPI and `net.unfret/mcp` in the official MCP Registry (`mcp/server.json`, an `mcp-name` marker in its README, PyPI metadata, `py.typed`, its own `mcp/CHANGELOG.md`), released by the new manual workflow "Publish MCP server" (`.forgejo/workflows/publish-mcp.yaml`): it refuses unless CI passed on main's newest commit and the versions agree, builds the package, smoke-tests it in a clean environment, publishes to PyPI (`PYPI_TOKEN`) and then to the registry with DNS authentication on unfret.net (`MCP_REGISTRY_KEY`), and tags `mcp-v<version>`; each publish step skips a version that is already out

### Changed

* **The MCP server defaults to mainnet** (`UNFRET_MCP_NETWORK=solana-mainnet`), matching https://unfret.net, its default API; devnet is for `UNFRET_API_URL=https://dev.unfret.net`. An unknown network is refused at start, and a payment offer on the wrong network names the setting to fix
* The README's status note describes the live service instead of "prototype, not maintained for production use", and its License section says what is open (all code, the trained scorers) and what isn't (the training corpus, the image seed)
* **`/extension` points to the Chrome Web Store listing** (`UNFRET_CHROME_STORE_URL`, default the Unfret listing): one "Get Unfret from the Chrome Web Store" link, for every Chromium browser, plus the link checker for phones. The self-hosted install (browser-policy commands, CRX download, ungoogled-chromium and developer-mode instructions) is no longer offered; `updates.xml` and its release files are still served for copies already installed by policy, and Unfret Dev's channel is unchanged

### Removed

* Kamal no longer routes the old names (`unfret.kroshtan.com`, `dev.unfret.kroshtan.com`): the forge's Caddy redirects them to unfret.net and dev.unfret.net (308, any path and method)

### ⚠ BREAKING CHANGES

* **Unfret moves to unfret.net**: production is https://unfret.net and dev https://dev.unfret.net. `UNFRET_PUBLIC_BASE_URL` and `UNFRET_SIWS_DOMAIN` follow (sign-in messages name `unfret.net`, so sessions signed for the old name no longer verify: sign in again), as do the settings' defaults, the MCP server's default `UNFRET_API_URL`, the e2e sweep's target and the production gate. Kamal still routes the old names (`unfret.kroshtan.com`, `dev.unfret.kroshtan.com`) to the same containers until the forge's Caddy redirects them to the new ones; then they are removed here. Polar's webhook moves to `https://unfret.net/v1/webhooks/fiat`

## [0.9.3](https://github.com/kroshtan/unfret_backend/compare/0.9.2...0.9.3) (2026-11-10)

### Changed

* The privacy page reads better: plain section titles ("What a check sends", "What we keep", "For shop owners", "Reporting a wrong result"), card payments and shop owners as their own items, shorter sentences. Same content

## [0.9.2](https://github.com/kroshtan/unfret_backend/compare/0.9.1...0.9.2) (2026-11-10)

### Bug fixes

* The privacy page still described card payments as "once available" without naming the provider: it now names Polar as merchant of record (payment details, e-mail, country, VAT and receipt, under its own policy) and says Polar gets the pack and the key's public ID
* `/connect` is for wallets only and said nothing to someone holding an API key (for instance after paying by card): it now points them to `/check`, "Use an API key", which also shows the key's balance
* `/checkout`: "Pay by card" did nothing when clicked without a usable API key (none entered, or a new key whose "I've saved both" box wasn't ticked): it stayed enabled, silently returned, and with an empty key field showed no message. It is now disabled until a key is ready, and the key field says "Enter your API key or its ID, or create a new key, to pay."

## [0.9.1](https://github.com/kroshtan/unfret_backend/compare/0.9.0...0.9.1) (2026-11-10)

### Features

* **Card payments in production**: the deploy-production workflow passes the optional repo secrets `PROD_POLAR_ACCESS_TOKEN`, `PROD_POLAR_PRODUCTS` and `PROD_FIAT_WEBHOOK_SECRET` (all three or none, else it refuses) to the app, with `UNFRET_POLAR_ENVIRONMENT` fixed to `production` in `config/deploy.production.yml`. Without them card top-ups stay off, as before

### Bug fixes

* **A Polar order is credited only by the site that made its checkout.** Dev and production share one Polar organisation, so each receives the other's order webhooks; an order without our checkout record was credited to the `key:<id>` in its metadata, which on the other site names no key, creating a balance nobody holds and a payment row for someone else's order. Checkouts now carry the site's address (`UNFRET_PUBLIC_BASE_URL`) in their metadata, and an order for another site answers `{"status": "other_site"}` and changes nothing

### Removed

* The last of the rename's one-time machinery: the deploy jobs no longer run `free-host.sh` or `adopt-db.sh` (both scripts and the cutover runbook are gone; the services and databases from before the rename are removed from the server), and `/extension`'s install command no longer deletes a `veridex.json` policy file. The connect page's message types keep their names until the extensions in use understand the new ones

## [0.9.0](https://github.com/kroshtan/unfret_backend/compare/0.8.0...0.9.0) (2026-11-10)

### Features

* **Share a result** (migration 16, `unfret.shares`): every assessed `POST /v1/analyze[/stream]` result carries a `share_token`, an HMAC over the product address (without fragment, credentials and tracking parameters), a hash of the result as a share shows it, whether `checkout_content` was sent, the product title and an expiry (7 days), naming no account. `POST /v1/shares/preview` shows exactly what would be public and `POST /v1/shares` stores it (`{id, link: "/r/<id>", delete_code, expires_at}`), both sent without `Authorization` (refused with `AUTHORIZATION_NOT_ALLOWED`); the token must match the url and result (`BAD_SHARE_TOKEN`, `SHARE_TOKEN_EXPIRED`), and a check that included the checkout page is never shared (`CHECKOUT_NOT_SHAREABLE`). Public: the showcase's wording (no score, never "scam" or "fraud") without the page's own words (quoting details and headlines left out, flags without evidence), title, shop, product link and the day of the check. Table `shared_results`: no account, wallet, IP, time of day or view count; the delete code only as a SHA-256 hash; expires after 90 days (`UNFRET_SHARE_DAYS`, deleted by the hourly housekeeping). 20 shares per visitor network per day (`UNFRET_SHARES_PER_VISITOR_PER_DAY`, in memory) and `UNFRET_SHARES_DAILY_CAP` (1000). `GET /v1/shares/{id}` and the page `/r/{id}` (the showcase renderer: "Checked by an Unfret user on …; the page may have changed", a nofollow product link, `X-Robots-Tag: noindex` and a robots meta tag); `DELETE /v1/shares/{id}` with `X-Delete-Code`; `POST /v1/shares/{id}/object` hides a share at once for review via `GET /v1/admin/shares`, `POST /v1/admin/shares/{id}/unhide` and `DELETE /v1/admin/shares/{id}`. CORS allows `DELETE` and `X-Delete-Code`. `/check` gains "Share this result": a preview with "Anyone with the link sees this verdict and the product link. It never says who checked.", then the phone's share sheet or the clipboard, and the delete code shown once. The privacy page, `llms.txt`, the README and the API contract describe it
* Result pages fill their template in one pass, so a title can't bring in a placeholder of its own
* **Invites** (migration 15, `unfret.credits.invites`): every credited pack, with crypto or by card, comes with invite codes to give away, one per 25 checks (25 → 1, 50 → 2, 100 → 4), each worth 3 checks, so the packs bring 28, 56 and 112 checks. `/v1/pricing` adds `invites` per pack (`count`, `checks_each`, `checks`) and the pack labels say "comes with 1 invite of 3 checks to give away". Redeem with `POST /v1/invites/redeem` `{code}` and the account's own sign-in (wallet session or API key): only for an account that never had any credit (a new wallet, a key never topped up), once per account, each code once; 3 checks. Storage: only each code's SHA-256, the day issued and the day redeemed (no buyer, redeemer or IP), and `wallets.invite_redeemed`. **Shown once**: `purchase-tx`, the QR code (`X-Unfret-Claim`) and `fiat-checkout` hand the buyer's page a random claim token (stored hashed, never on chain or sent to Polar); the codes are made in the crediting transaction and kept with the quote or card checkout until the first status request carrying the token (`GET /v1/credits/quotes/{id}`, new `GET /v1/credits/fiat-checkouts/{id}`) takes them and wipes them in one statement; uncollected card codes are wiped after 7 days. A gift, not a sale: no VAT record, no `crypto_sales` or fiat revenue, a `credits_granted kind=invite` log line; `GET /v1/admin/invites` counts those outstanding. `/checkout` shows the codes with Copy and "Give this to someone who hasn't tried Unfret: it adds 3 checks to a new wallet or a new API key." on the "Payment received" panel (card payments too, back from Polar), keeps pending claims in localStorage for three days, and offers "Have an invite?" for a new API key; `/connect` (after a same-browser sign-in) and `/check` (signed in) offer it too. The home and checkout pages mention the invites, and the privacy page describes them. CORS allows `X-Unfret-Claim`
* **Production deploys are gated on an end-to-end sweep of dev** (`tests/e2e/test_dev_sweep.py`, `make e2e`): after "Check the release", the deploy-production workflow waits for dev to report the release's commit, then runs the sweep against it with real infrastructure on devnet (API keys, credit analyses and every free error path, wallet sign-in, x402 with the MCP client in USDC and wSOL with replay and refusals, credit packs in USDC and by Solana Pay reference alone, card-top-up webhooks against live Polar without a real order, the showcase, the operator endpoints, and no checked URL left in dev's logs or database). Nothing is tagged or deployed unless it passes. It refuses to run against anything that isn't devnet, checks the payer is funded first, and leaves the e2e key and wallet where they started. New repo secrets `DEV_E2E_PAYER_KEY` and `DEV_E2E_KEY` (README, "End-to-end sweep")
* **The vendored EU VAT rates and Cloudflare ranges are checked monthly in CI** (`.forgejo/workflows/vat-rates.yaml`, 06:00 UTC on the 1st, or by hand): `make vat-rates-check` on the public runner, no secrets. A change fails the run with the file that is out of date, what differs (each country's rate in the table and in TEDB; the ranges added and removed) and the script that rewrites it; an unreachable source says so instead. `make vat-rates-check` now runs both checks even when the first fails
* `GET /v1/credits` adds `invite_eligible`: whether an invite can still be redeemed onto the account (it never had credit), so clients offer "Have an invite?" only where it can work
* `/v1/health` and `/v1/health/ready` report `commit`: the commit the image was built from (Dockerfile `GIT_SHA` build argument, passed by Kamal from CI; `null` outside a built image)
* `POST /v1/admin/keys/{key_id}/debit` (operator token): take up to $10 back from an API key per call, never below zero (409); the sweep nets its test key with it
* **Made-up EU VAT numbers** (`unfret.facts.vat_ids`, `eu_registry`): a VAT number is checked against its country's check digit (AT, BE, CZ, DE, DK, FI, FR, IT, LT, LU, NL, PL, PT, SE; other countries aren't judged), with no lookup, so the quick tier and a VIES outage catch it too. When every VAT number on an EU shop's page is shown whole and can't exist, it earns no credit as a VAT number (`vat_present` 0), reads as not valid (`vat_valid` 0, finding `vat_fake`, the "Invalid VAT number" reason, high) and is never sent to VIES, and the verdict is "careful" at best, however good the rest looks. A number run into other digits on the page (a typo) isn't judged. On the corpus no legitimate page's VAT number fails its check; real numbers of the member states' examples and of large shops pass
* **Per-market evaluation** (`evals.markets`, `make markets`): the shipped full and quick scorers on TEST per market (all, EU, EU archived, EU fetched live, non-EU, US, other) with AUC, Brier, legit pages warned about ("careful" or worse, as the shopper verdict says it) and unreliable pages caught, recomputing the page-only skills with the current code and reusing an earlier run's LLM features, so no LLM calls; `--train` fits variant-C candidates at `--prior` 0.5 next to them and `--ship` writes one (`evals.train.write_artifact`, shared with `evals.train`). Measured on the shipped models: legit EU pages are warned about far more often than US ones (deep 14/103 vs 1/40, quick 12/103 vs 1/41)
* **Legitimate EU shops in the corpus** (`evals.sources.eu_shops`, `make eu-shops`): 287 hand-picked home pages of established retailers, brand shops and smaller brands in 18 EU countries, fetched the way a URL-only check is (`page_fetch.fetch_page`) where `robots.txt` lets Unfret in, kept when they look like a shop, as `legit` dataset labels with their own `source` (`live`), so the existing TEST set is unchanged. 91 were added (blocked, empty or not shop-like pages are recorded and skipped). The models are not retrained on them yet: the full model needs their LLM features first, and a quick model retrained with them is better on EU pages (warned 13 → 6 of 129) but worse elsewhere (non-EU AUC 0.79 → 0.75), so it isn't shipped

### Bug fixes

* Quick tier: a countdown that doesn't restart on reload is no longer enough to say "careful" on its own (it is an ordinary promotion, Black Friday banners, as often as pressure); one that restarts still is, and the deep tier is unchanged. On the corpus (`evals.markets --split all`): legitimate shops warned 54 → 27 of 573 (EU 50 → 23, live EU shops 9 → 1), unreliable caught 160 → 152 of 683
* The "'Was' price never charged" reason named the wrong prices: it read the first two numbers of the pricing evidence, the "30" of "the last 30 days" and the 30-day low, so a €79.95 "was" price against a €19.95 low read "sold for €30.00 …, yet the 'was' price is €19.95". It now reads the two prices
* **US shops' company names, from the copyright line and without stray words** (`us_identity`): the footer's "© 2026 Acme Outdoor LLC. All rights reserved." never reached the skill, because the page text has no footer; up to three copyright lines now travel in the new `PageFacts.copyright_lines` (100 characters after the © or "Copyright", not cut at "Co." or "L.L.C."; API contract and snapshot updated) and a company in a copyright line is preferred to any other on the page, with the year, © and "Copyright" left out, across the line breaks inline tags leave ("©<span>2026</span> <a>Acme</a> LLC"). Elsewhere a name no longer reaches across lines, so menu items and table labels don't join it ("About Us\nFAQ\nAcme Outdoor LLC", "Manufacturer\nKao USA Inc."). Names keep "&", "and" and "of" ("Smith & Sons LLC", not "Sons LLC") and may start with a digit ("5econds Pte. Ltd."); leading sentence words are dropped ("Sold By Acme LLC") and names made only of them don't count ("We Are Not An LLC", "Our Company LLC"); "The Acme Company LLC" now counts. Platforms, payment providers and the like are never the shop's company ("Powered by Shopify Inc.", "Square, Inc.", "Google Ireland Ltd" in a cookie notice), nor is whoever a credit names ("Website by Pixel Studio LLC"). On the corpus, 51 of 1,294 pages change: 17 gain a company (mostly from the footer), 8 lose a platform or a non-name and 26 lose stray words. A copyright line is no imprint: `eu_registry.imprint` outside the EU reads exactly as before (any company form in the page text and legal snippets, `names_any_company`), so the shipped scorer's scores don't change (`evals.us_market`)
* `eu_registry`: a bank account number that starts like a VAT number (a Lithuanian shop's `LT70 4010 …` IBAN) is no longer looked up in VIES, quoted as the VAT number or judged by its check digit, so a legitimate shop showing its IBAN before its VAT number isn't told its "VAT number" is invalid. It still counts as `vat_present`, as the scorers were trained
* `/checkout`: a failed card checkout re-enables the "Pay by card" button (it read `event.currentTarget` after an `await`, when it is already `null`)

### Removed

* **The Dutch translations: the app is in English only.** `Accept-Language` is no longer read (`shopper.pick_language`, `shopper.LANGUAGES` and `copy/nl.json` are gone; `shopper.copy_for(language)` is `shopper.catalogue()`), so the verdict's label, reasons, positives and template headline, the flag labels, the early-exit explanations and the pack labels are always English; numbers and amounts use English formatting. The LLM-written headline is asked for in English, and without a scorer the judge's own sentence is the headline for everyone. `verdict.language` stays on the wire, always `"en"`; `/v1/pricing` pack `label` keeps its shape with only `en`. The result cache no longer keys on a language (an analysis with a different `Accept-Language` is the same cached result), and report tokens issued before still match. The privacy page loses its Dutch section, `/check` no longer sends `Accept-Language`, and `/checkout` names countries in English. API contract: "English only"
* `scripts/grant_credits.py`, which wrote a column renamed long ago (`balance_lamports`) and so no longer worked; grant an API key credit with `POST /v1/admin/keys/<key_id>/grant`

## [0.8.0](https://github.com/kroshtan/unfret_backend/compare/0.7.0...0.8.0) (2026-11-10)

### ⚠ BREAKING CHANGES

* **The Veridex name is gone**: the Python package is `unfret` (`src/unfret`, `python -m unfret.…`, `uvicorn unfret.main:app`), the settings are `UNFRET_*` (no `VERIDEX_*` aliases), payment memos are `unfret:quote:<id>` and `unfret:credit:<account>` (purchases are matched by their Solana Pay reference first, so quotes issued before still settle), and the MCP server is the `unfret_mcp` package with only the `unfret-mcp` command, reading `UNFRET_*` only. Deploys: one Kamal service `unfret` and image `kroshtan/unfret` for both destinations (containers `unfret-web-dev-<sha>` / `unfret-web-production-<sha>`, so production deploys dev's image as it is), databases `unfret-dev-db` / `unfret-prod-db` with user and database `unfret`, and the shared repo secret `LLM_API_KEY`. Each deploy job first frees its host on kamal-proxy from any other service (`scripts/deploy/free-host.sh`: route removed, old web containers stopped, not removed) and, once, copies the old database into the new empty one (`scripts/deploy/adopt-db.sh`: `pg_dump` in one transaction, old container stopped, not removed; production refuses to start on an empty database) ([docs/production-cutover.md](docs/production-cutover.md)). The web pages' session storage key is `unfret-session` (sign in again); `/extension` no longer serves `veridex-*` file names; the local admin-token file defaults to `~/kroshtan_secrets/unfret-prod-admin-token` and the corpus to `~/repos/unfret-corpus`. Repository links point at `kroshtan/unfret_backend`

## [0.7.0](https://github.com/kroshtan/veridex_backend/compare/0.6.0...0.7.0) (2026-11-10)

### ⚠ BREAKING CHANGES

* **No free trial**: a wallet's first sign-in no longer grants free checks; every new account (wallet or API key) starts at zero and buys a pack. `/v1/pricing` loses `trial_checks`, and the `VERIDEX_TRIAL_*` settings (checks, wallet age, signatures, per-visitor limit, daily cap) are gone, with the on-chain history lookup and the in-memory limiter that served only the trial. Balances already granted stay. The `wallets.trial_granted` column (migration 5) is left in place, unused. The home page no longer mentions a trial. The reward of 3 checks for a good report is unchanged
* **Credit purchases are packs, with VAT** (pricing v2): `POST /v1/credits/purchase-tx` and `GET /v1/credits/solana-pay.svg` take `checks` (25, 50 or 100) and `country` instead of an amount; `POST /v1/credits/fiat-checkout` takes `checks` instead of `amount_usd_micros`. Purchase bonuses are gone. `/v1/pricing` loses `bonuses` and `purchase`. Analyses and x402 deep checks now cost $0.20

### Features

* **Deploy production from the Actions page alone**: the workflow's `ref` defaults to `main`, deploys main's newest commit and tags it `v<version>` itself (refused if that tag names another commit); an existing release tag still deploys that release again. The first run also takes `unfret.kroshtan.com` off the pre-split devnet service (route removed, containers stopped, not removed, for rollback)
* **Fewer false warnings** (models `v1-20261109-222323-C` and `v1-quick-20261109-222308-C`): both scorers are calibrated to an even base rate (`evals.train --prior`, default 0.5) instead of the corpus share of unreliable pages (57%), which tilted every page towards "unreliable". On TEST, legit pages scored "careful" or worse fall from 35 to 24 of 143 for deep (unreliable caught 168 → 151 of 190) and from 78 to 24 for quick (166 → 103); AUC unchanged. The quick tier also warns only with a concrete reason: things missing from the page alone (no imprint, return policy, withdrawal right or trust information) make it "concerns" at most, however sure its scorer is (`shopper.build(concrete_only=…)`)
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
* **Card payments for API keys through Polar** (`veridex.credits.polar`), off unless `VERIDEX_POLAR_ACCESS_TOKEN` (an Organization Access Token, scope `checkouts:write` only), `VERIDEX_POLAR_PRODUCTS` (`25=<id>,50=<id>,100=<id>`) and `VERIDEX_FIAT_WEBHOOK_SECRET` are all set; `VERIDEX_POLAR_ENVIRONMENT` picks `sandbox` (default) or `production`. A checkout is the pack's Polar product in USD (`currency: "usd"`, discount codes off), our account id in the metadata, success URL `/checkout?paid=1`, nothing about the buyer; a checkout Polar prices in another currency or amount is refused (503) before anyone pays. Webhooks at `/v1/webhooks/fiat`: `order.paid` credits the pack's value only for a USD order of a configured pack's product at exactly its card price before tax (`net_amount`), otherwise nothing (logged as `polar_order_not_credited`, no buyer details); `order.refunded` debits the refunded share (`refunded_amount`, before tax); `refund.created`/`refund.updated` carrying a lost dispute debit the whole order (Polar has no dispute webhook). `/v1/health/ready` reports `fiat` (`off`, `polar:sandbox`, `polar:production`). The dev deploy passes the `DEV_POLAR_*` and `DEV_FIAT_WEBHOOK_SECRET` repo secrets through. `FiatProvider.create_checkout` now takes the pack instead of an amount
* **Fiat interface, shaped for Polar** (`veridex.credits.fiat`, no real provider yet): hosted checkout (USD, at least $10) with our account id in its metadata and a server-side checkout → account mapping; Standard Webhooks verification with a replay window; paid orders credited once; refunds debit pro rata, disputes debit the whole order, and may take a balance below zero (which blocks paid use). Per order: provider, order id, account, amount, credits, refunded/disputed, for 180 days; never the buyer's details. Tested with a fake provider
* **Reports of wrong results** (migration 9, `veridex.reports`): results carry a `report_token`; `POST /v1/reports` (with consent) sends that one check's page back. Personal details are scrubbed on the server (greetings, delivery lines, emails, phones, IBANs, cards, URL query strings; raw HTML reduced to facts and text; no checkout page). Duplicates are closed before the queue (`already_reported` counts as confirmation, `already_known` within 90 days of a label); 5 reports per account per month, one per page, about 200 open. A label at least 30 points from the model's score pays the reporter 3 checks; the report is then deleted and the page kept as training data without the account. Unhandled reports go after 30 days. `make reports` labels the queue (most confirmed and least certain first) with one keypress per page, straight into the corpus. The privacy page (en/nl) describes keys, card payments and reports
* Extension 0.6.0 at `/extension` (same key and ID, permissions unchanged): "use an API key" in the settings as an alternative to a wallet sign-in, and "This is wrong" under a result (reason, consent for that one check, the outcome in plain words), in English and Dutch
* `shop_links` skill (quick and deep tiers): shops sharing an identifier, one hop only, ignoring identifiers held by more than 25 shops; evidence names the shared public ID and the shops, never a contact. Features are logged but have no weight in the scorer until the corpus has them

* **US market**: Walmart.com product pages (`platform: "walmart"`, read from the page's `__NEXT_DATA__`: price and "was" price, seller, Walmart fulfilment, rating histogram, verified reviews; product id `walmart:<id>` in the price store), and the current amazon.com buy box ("Ships from"/"Sold by" as sibling elements, `avp-badge-linkless` verified badges, the media "Format:" suffix off the byline). A shop counts as US (`veridex.market.shop_market`) only on positive evidence: US dollar prices (structured, or "$" prices outnumbering €/£ in the text), no EU VAT ID, not on an EU/UK country domain. On a US shop a named company or a US business address counts as the imprint, the missing-returns reason doesn't cite EU law (`no_return_policy`), and US sales tax is left out of the checkout drip-pricing comparison. New skills `us_identity` (company, US address and phone, mailbox-only addresses; no state registry lookup: there is no free, terms-permitted API) and `shop_policies` (15+ day delivery, returns abroad, return address withheld, restocking fees above 20%, doubtful "Made in USA"), in both tiers, with shopper reasons in en/nl; their features have no weight in the scorer until the corpus has them (`python -m evals.us_market` reports the effect per market). A skill can now report `<skill>.not_applicable`, which the scorer reads as neutral

### ⚠ BREAKING CHANGES (x402)

* **The x402 quick tier ($0.01) is a rules-only first-pass filter**: it no longer runs the LLM read (`content`), `reviews` or `reverse_image`, nor the price history; it never calls the LLM (classification by rules: a page without a product and a price is not assessed and is refunded) or the image index. It runs `listing`, `pricing` (on-page checks), `html_source_signals`, `dark_patterns`, `injection`, `eu_registry` (no VIES), `us_identity`, `shop_policies` and `shop_links`, scored by a new `model-quick.json` trained on those features only (`python -m evals.train --quick --ship C`, `v1-quick-20261109-203641-C`). Deep ($0.20) is unchanged: every skill and `model.json`, the same as an analysis. Each 402 option says what it buys (`extra.tier`, `extra.includes`), as do `/.well-known/x402` (`tiers.<tier>.includes`), `/llms.txt` and the README. Measured on TEST (333 pages): quick AUC 0.746 (US 0.782, EU 0.673; storefronts 0.769, marketplaces 0.606), legit pages flagged 29/143 (US 4/39, EU 25/103), recall 114/190 = 0.60; deep AUC 0.894 (US 0.964, EU 0.828; storefronts 0.836), legit flagged 25/143, recall 0.82. Before this change quick ran the deep skills minus domain age and VIES with the deep model, so it scored like deep. A quick model with the rule-mirroring columns (variant B) measured AUC 0.857, but its marketplace AUC of 0.9997 comes from relearning the rules that labelled the synthetic marketplace pages (storefronts: 0.761, no better than C), so C ships; scoring the quick features with the deep model was unusable (content and reviews neutral: 76/143 legit flagged; read as 0: recall 0.03)

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
