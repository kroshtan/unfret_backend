# Unfret

[![CI](https://github.com/kroshtan/unfret_backend/actions/workflows/ci.yaml/badge.svg)](https://github.com/kroshtan/unfret_backend/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)

![Unfret checking a test shop: Serious concerns, with a fake countdown, a pre-ticked extra and an unbelievable discount](docs/demo.gif)

**Unfret checks a web shop, an ad or a company's website and says in plain words whether it all adds up.** This
repository is the service behind it: the API, the website ([unfret.net](https://unfret.net), including the
[link checker](https://unfret.net/check)) and the MCP server for AI agents ([`mcp/`](mcp/)). The analysis is a
[LangGraph](https://github.com/langchain-ai/langgraph) pipeline of independent "skills" run in parallel; a small
trained model turns their features into a calibrated 0–100 score and one of four verdicts (Looks fine, Worth a look,
Think twice, Serious concerns), and an LLM writes the explanation from the signals that moved it.

| | |
| --- | --- |
| Website | [unfret.net](https://unfret.net) · [check a link](https://unfret.net/check) · [developers and AI agents](https://unfret.net/developers) · [API reference](https://unfret.net/docs) |
| This repository | [git.kroshtan.com/kroshtan/unfret_backend](https://git.kroshtan.com/kroshtan/unfret_backend) (primary) · [GitHub mirror](https://github.com/kroshtan/unfret_backend) |
| Browser extension | [git.kroshtan.com/kroshtan/unfret_extension](https://git.kroshtan.com/kroshtan/unfret_extension) · [GitHub mirror](https://github.com/kroshtan/unfret_extension) · [Chrome Web Store](https://chromewebstore.google.com/detail/unfret/giiengbpechhnphignbicmlcbklmcpjo) |
| Android app | [git.kroshtan.com/kroshtan/unfret_android](https://git.kroshtan.com/kroshtan/unfret_android) · [GitHub mirror](https://github.com/kroshtan/unfret_android) |

> **Project status: live, run by one person.** Unfret runs at [unfret.net](https://unfret.net) with the
> [Chrome extension](https://chromewebstore.google.com/detail/unfret/giiengbpechhnphignbicmlcbklmcpjo), prepaid checks
> (card via Polar, or USDC/SOL with a Solana wallet) and pay-per-check x402 for AI agents. It is a side project: no
> SLA, and scores are a signal, not a verdict (see [Limitations](#limitations-and-responsible-use)).

## How accurate it is

Measured on Unfret's labelled corpus of **1,762 product pages** (scam shops, junk and dropshipping, and legitimate
shops and listings, among them a golden set of well-known sellers' pages), each replayed through the analysis and
scored by the open baseline model in this repository. **ROC AUC 0.917.**

| Verdict | Pages | Scam or junk among them |
| --- | --- | --- |
| Serious concerns | 278 | 96% |
| Think twice | 415 | 83% |
| Worth a look | 430 | 44% |
| Looks fine | 629 | 5% |

Think twice or Serious concerns catches 73% of the scam and junk pages, and 9% of the legitimate pages get one
(false alarms); Serious concerns alone catches 32%, with 1% false alarms. The corpus includes the pages the model was
trained on, so expect somewhat lower numbers on pages it has never seen. It covers product pages only (not ads or
service providers), and the corpus itself is private (see below). The method, ads and tradespeople, false alarms per
market, the limits and the model's history are on [unfret.net/accuracy](https://unfret.net/accuracy), from
`make accuracy` (aggregates only, in [`static/accuracy.json`](src/unfret/static/accuracy.json)).

## The model

The scoring model in this repository ([`src/unfret/scoring/`](src/unfret/scoring/), a logistic regression over the
skills' features) is an **open baseline** (`v1-20261109-222323-C`, and `v1-quick-20261109-222308-C` for the x402
quick tier, as of v0.12.0). The service at unfret.net runs privately retrained versions of it. The labelled corpus
and the duplicate-product image dataset (the photo index's seed) are private, so the baseline can be inspected and
run but not retrained from this repository alone. `/v1/health` names the scorers a server runs (`model_version`,
`quick_model_version`).

**Self-hosting uses the open baseline model.** It is in the image; nothing to configure.

**Bring your own scorer.** The training code is here (`evals/train.py`), so you can train on a corpus of your own
(`UNFRET_CORPUS_DIR`, in the layout `evals/corpus.py` reads) and run the result:

```bash
make train TRAIN_ARGS="--ship C"            # → $UNFRET_CORPUS_DIR/models/<version>/model.json and RELEASE.md
make train TRAIN_ARGS="--ship C --quick"    # the quick tier's own scorer (optional)
UNFRET_MODEL_PATH=…/models/<version>/model.json make accuracy   # replay your corpus with it (→ accuracy.json there)
UNFRET_MODEL_PATH=… UNFRET_MODEL_QUICK_PATH=… make golden GOLDEN_ARGS="--replay --split test"   # false alarms
```

`make golden` measures both tiers on the corpus's **golden set** (`evals/sources/golden.py`: product pages of
well-known, legitimate sellers, from a seed list kept in the corpus) against a fixed rule: no "Serious concerns", and
under 5% "Worth a look" or worse, with the reasons and flags behind each failure. Every scorer version is run
through it before release.

A retrained scorer can also say that with it, "Worth a look" needs a reason to show (`--needs-reason`, the artifact's
`verdict_needs_reason`): a page it scores in that band with no reason, or only something missing from the page, then
reads "Looks fine", and the quick tier's needs a concrete reason (`unfret.shopper.build`). Whether that pays is
measured per scorer, on the golden set and the corpus replay; the open baseline doesn't set it (with its weaker
scorer it would call too many scam pages "Looks fine").

Then point the server at the files: `UNFRET_MODEL_PATH` for analyses and deep checks, `UNFRET_MODEL_QUICK_PATH` for
the quick tier (in Docker, mount them read-only, e.g. `-v /srv/scorer:/app/private-model:ro` and
`UNFRET_MODEL_PATH=/app/private-model/model.json`). Either left empty runs the baseline; without a quick scorer of
its own the quick tier keeps the baseline's. A path that is set but missing, unreadable or made for features no
skill produces stops the server at start-up instead of letting it score with something else. Retrained models go to
the model store (`UNFRET_MODELS_DIR`, default `<corpus>/models`), never into `src/`: a pre-commit hook
(`scripts/check_scorer_baseline.py`, also run by CI) refuses a changed baseline or any other scorer artifact in the
repository. The `/accuracy` page and `static/accuracy.json` keep the baseline's numbers and say so when the server
runs another scorer.

**Reported results feed the corpus.** A "Wrong result?" report (`POST /v1/reports`, [`src/unfret/reports/`](src/unfret/reports/))
stores the scrubbed page in a review queue, worth most first (how far the reporter says the score is off, pages and
shops the labelled data lacks, confirmations); a full queue (`UNFRET_REPORT_QUEUE_CAP`, 1000) gives up its least
valuable item for a better one. Each queued report gets one second opinion from a stronger model than the
analysis's own (`UNFRET_REPORT_SECOND_OPINION_MODEL`, at most `UNFRET_REPORT_SECOND_OPINIONS_PER_DAY` calls a day): a
proposed label, a confidence and a reason. `make reports` shows the queue with the proposals, confirms the
confident ones in bulk after a preview and goes through the rest one by one; each decision is written to the corpus
as a human label and only then pays the first reporter (5 checks when the score was 30+ points off, 1 when 10–29)
and becomes a training sample. A proposal nobody confirmed is never a label. Reports of the test shop never enter
the queue.

## Run your own

You need Docker and an API key for an OpenAI-compatible LLM API
([Scaleway Generative APIs](https://www.scaleway.com/en/generative-apis/) by default).

```bash
git clone https://git.kroshtan.com/kroshtan/unfret_backend.git && cd unfret_backend
cp .env.example .env       # then set the variables below
docker compose up --build  # website and API on http://localhost:8080, Postgres alongside
```

| Variable | |
| --- | --- |
| `UNFRET_LLM_API_KEY` | Required. With `UNFRET_LLM_BASE_URL` and `UNFRET_LLM_MODEL` for any OpenAI-compatible API (the model must support tool calling) |
| `UNFRET_SESSION_SECRET` | Required for passes and checks (they are off while it is empty): `openssl rand -hex 32` |
| `UNFRET_ADMIN_TOKEN` | Lets you give checks to a pass: `openssl rand -hex 32` |
| `UNFRET_NETWORK_LOOKUPS` | `true` by default: domain registration, certificate and EU VAT lookups about the shop; `false` keeps a check to the page itself |

Payments (card, crypto, x402) stay off until you configure them; the full list is under
[Configuration](#configuration). To run a check:

1. Open `http://localhost:8080/checkout` and choose **Make a new pass**. A pass looks like `ufk_<id>_<secret>`.
2. Give it checks (one check is `200000` micro-dollars):

   ```bash
   curl -X POST localhost:8080/v1/admin/keys/<id>/grant -H "Authorization: Bearer $UNFRET_ADMIN_TOKEN" \
     -H "Content-Type: application/json" -d '{"usd_micros": 2000000}'
   ```

3. Open `http://localhost:8080/check`, paste the pass under "I already have a pass" and check a link, or ask the API
   (the server fetches the page itself):

   ```bash
   curl -X POST localhost:8080/v1/analyze -H "Authorization: Bearer ufk_…" -H "Content-Type: application/json" \
     -d '{"url": "https://unfret.net/test-shop"}'
   ```

   [`/test-shop`](#the-test-shop) is a made-up shop with every trick Unfret catches, so it is a good first check.
   The browser extension can use your instance too: set its API base in the extension's settings.

## How it works

```mermaid
flowchart LR
    A[POST /v1/analyze] --> P[preprocess<br/><sub>PageFacts + text, one parse</sub>]
    P --> C{classify<br/><sub>rules first: known marketplaces<br/>and platforms, page facts; LLM for the rest</sub>}
    C -- article / non-product --> X[early exit<br/><sub>score = -1</sub>]
    C -- marketplace listing / storefront --> PR
    C -. p2p ad .-> PP
    C -. service provider .-> SV
    subgraph PR [product subgraph]
        S[cheap skills<br/><sub>in parallel</sub>] --> T{triage} -- photo check worth it --> R[reverse_image] --> T
        T --> J[judge<br/><sub>scorer + LLM headline</sub>]
    end
    subgraph PP [p2p subgraph]
        S2[skills for ads] --> T2{photo check} --> J2[ad rules]
    end
    subgraph SV [service subgraph]
        S3[skills for service providers] --> T3{triage<br/><sub>rules</sub>} --> J3[judge<br/><sub>hand-written rules</sub>]
    end
```

Preprocessing parses the page once into typed **`PageFacts`** (title, brand, prices, seller, rating histogram, dated
reviews, hidden text, checkboxes, timers, VAT IDs, policy links, and the business facts: public product IDs,
payment methods, published phone numbers and e-mail addresses, addresses, KvK and licence numbers, founding claims,
Organization/LocalBusiness data, same-site links, a service company's testimonials, a platform profile's track
record, …); a browser extension can send them directly (`page_facts`).
Contacts and addresses are read only from what the business publishes (structured data, `tel:`/`mailto:` links, the
footer and contact, legal or imprint sections), never from forms, account menus, checkout or delivery areas, so
nothing about the visitor ends up in the facts (`facts/business.py`). Each skill reads the facts and reports numeric
**features** plus factual **findings** (evidence, never a verdict). All page content is passed to the LLM as clearly
delimited untrusted data.

**One shared classify step, one LangGraph subgraph per page class.** `preprocess` and `classify` are shared; each
page class then runs in its own subgraph (`src/unfret/graph/__init__.py`), which fans out to the skills whose
`page_types` include the page's type (LangGraph's `Send` API, so they run concurrently), then a triage, then a judge:

- **product** (marketplace listings and storefronts): the cheap skills, the triage (rules first, the LLM only for
  unclear cases) that decides whether the reverse image search is worth it, and the judge (the trained scorer's
  band; an LLM writes the headline). The four bands read "Looks fine" (`reliable`), "Worth a look" (`concerns`),
  "Think twice" (`careful`) and "Serious concerns" (`junk_or_scam`); "Serious concerns" only when the scorer is sure
  (`UNFRET_VERDICT_SERIOUS_MIN_CONFIDENCE`, 0.6; the quick tier's scorer 0.7), otherwise "Think twice". Every band
  below "Looks fine" names at least one reason.
- **p2p** (`p2p_listing`: a private seller's ad on Marktplaats, 2dehands, Vinted, Craigslist, Facebook Marketplace,
  or a private seller on eBay) and **service** (`service_listing`: a provider's profile on Werkspot, Thumbtack, Angi,
  HomeAdvisor, …; `service_site`: a service company's own website). These exist only while
  `UNFRET_EXPANSION_CLASSES` is on (default **off**: classification is then exactly as before, and such pages are
  treated as they always were; dev and production set it on in `config/deploy.*.yml`). No scorer is trained for
  them yet:
  - **ads** get a full verdict from cautious hand-written rules (`src/unfret/shopper_p2p.py`): "Looks fine" only
    when nothing fired and the positives are concrete (an account of two years or more with the platform's
    payment protection or pickup, or well rated with a checked normal price); "Serious concerns" only for strong
    combinations (payment pushed off the platform with a new or copied seller, or a bait price; a photo from other
    ads with a bait price); "Think twice" for one serious sign or two middling ones; "Worth a look"
    otherwise. The photo check (reverse image) runs whenever the ad has a photo, within the per-analysis budget.
  - **service providers** get a full verdict from cautious hand-written rules (`graph/services.py`), for the
    Netherlands and the US: "Serious concerns" only for the combinations behind fake local businesses (town
    landing pages without an address plus links to other sites with the same phone numbers or tracking IDs; a
    premium-rate number plus payment up front or cash only; copied photos plus one of those or a brand-new site; a
    page saying its tradesperson isn't necessarily local plus another call-centre mark), "Think twice" for
    one serious or two medium signals, an anonymous local site (named after a trade and a town, or only national
    numbers, with no KvK number or licence and no street address) or a new platform profile without reviews that
    makes emergency, call-out or payment promises, "Looks fine" only with concrete positives (an
    established domain, a complete identity with KvK or licence number, a street address, no doorway pattern, no
    upfront or cash-only terms; on a platform, two years and twenty reviews averaging four stars), and "Worth a
    look" otherwise. Their triage runs the photo search, by rules, when the cheap skills found something copied
    photos would confirm. No register is looked up (no KvK API, Companies House or state licence boards): the
    signals come from the page itself, the fingerprint store and the photo index, plus VIES for a VAT number. Their
    results carry no share token.

Classification is rules first. A known marketplace's product page (Amazon, bol.com, Walmart) or a page with a
product and a price is settled from the facts. With the expansion classes on, a host and path table
(`graph/platforms.py`) comes first: a listing or profile on a known peer-to-peer or service platform is classified
without the LLM (an eBay listing by a seller the page labels a business stays a marketplace listing; otherwise it is
an ad), and the platform's other pages (search, home) go to the LLM rather than count as a shop; after the product
rules, structured data naming a trade (`Plumber`, `Locksmith`, `HomeAndConstructionBusiness`, …) makes a
`service_site`. The LLM classifies whatever is left, with the new types in its prompt only while the flag is on.

Nodes that call the LLM or fetch outside data (classify, the content, reviews, domain_age, eu_registry and
reverse_image skills, the triage, the judge) have LangGraph's built-in **RetryPolicy**: a transient error (dropped
connection, provider 5xx, rate limit) gets `UNFRET_GRAPH_RETRY_ATTEMPTS` attempts (default 2) before a skill counts
as unavailable. LangGraph, langchain-core and langchain-openai are pinned to their minor release and upgraded
deliberately; no LangSmith or other hosted LangChain service is used (its tracing is forced off).
`POST /v1/analyze/stream` streams every class's skill steps (`astream(..., subgraphs=True)`).

| Skill | What it checks |
| --- | --- |
| `content` | Every class: one LLM read of the page text: scam copy and fake urgency, generic white-label product, whether the brand is established, implausible claims, missing trust information |
| `listing` | Generated-looking brand names, inflated "was" prices, keyword-stuffed titles, seller ≠ brand, ships from China |
| `reviews` | Reviews per day since the listing appeared, rating shape, duplicate and very short reviews, bursts, and review/title embedding similarity (reviews borrowed from other products) |
| `html_source_signals` | Known dropship apps, supplier CDNs, AliExpress review importers, shipping-protection and countdown apps |
| `dark_patterns` | Product pages and service sites: countdown timers (and whether they reset per visitor), pre-ticked paid add-ons, fee wording, and checkout total vs listed price when the checkout page is supplied |
| `injection` | Every class: hidden text and instructions addressed to an AI, which honest shops have no reason to include |
| `eu_registry` | Storefronts and service sites: VAT ID checked against the EU VIES register (name match) and its check digit (a number that can't exist earns no credit and makes the verdict "careful" at best; a bank account that starts like a VAT ID is ignored), withdrawal right and imprint; outside the EU (a US shop, or one on a non-European country domain or currency) a named company or US business address counts as its imprint, and a missing imprint, VAT ID or return policy is not applicable (neutral), since no law there requires them |
| `us_identity` | US storefronts and service sites: a named company (LLC, Inc.), a US business address and phone number; an address that is only a mailbox (PO Box, PMB). Not in the scorer yet |
| `shop_policies` | Storefronts: delivery ranges of 15+ days, returns sent abroad, a return address given only on request, restocking fees above 20%, and "Made in USA" on goods shipped from overseas. Not in the scorer yet |
| `domain_age` | Storefronts and service sites: domain registration via RDAP and the first certificate in the CT logs; a "since 1998" / "founded 20 years ago" claim about the shop itself on a domain younger than a year by both measures (claims vs facts) |
| `shop_links` | Storefronts and service sites: other web shops (or service sites) with the same analytics/pixel IDs, payment account or (hashed) contact details, one hop, from the shop fingerprint store; how many of them are young (their own registration dates). Service sites are recorded like shops and also linked on their published phone numbers (national formats too) and their e-mail domain (not webmail), hashed: one call centre behind many "local" brands. Local lookup, also in the quick tier; not in the scorer yet |
| `payment_methods` | Storefronts: only payments that can't be reversed (bank transfer, crypto, Western Union, MoneyGram; gift cards don't count alone) and no card, PayPal, iDEAL, Klarna or other protected method in the text, logo alt texts or a payment provider's script (Shopify shops never: their checkout takes cards); protected methods offered (a positive). Not in the scorer yet |
| `claims` | Storefronts: claims against the facts: "a Dutch company", "family business in Amsterdam", "based in Germany" while every address the shop gives is in another country (told from the address text: country names, Dutch postcodes, US state and ZIP, UK postcodes; no geocoding); "visit our showroom" with no address, postcode or phone number anywhere. Not in the scorer yet |
| `market_price` | Product pages with a barcode (GTIN/EAN): the price against each other shop's median price for the same barcode over 90 days, from Unfret's own anonymous observations (`unfret.market_prices`); far below means at most 40% of the median of at least three other shops in the same currency. Off in evaluation; not in the scorer yet |
| `review_copies` | Product pages: the longer reviews (60+ characters, 10+ words) whose normalised text other shops show too, from the review fingerprint store (`unfret.review_store`: hashes only). Told to the shopper only for an unknown brand with other resale signs, since brands syndicate reviews to their resellers. Off in evaluation; not in the scorer yet |
| `brand_presence` | Product pages: the brand on Wikidata (a business or brand item with that label or alias) and its own `<brand>.com`/`.nl` in RDAP (registered, and how long ago), both cached as store-level facts. "No presence" is told to the shopper only on marketplace listings with other white-label signs. A network lookup: off in evaluation; not in the scorer yet |
| `safety_gate` | Product pages: the product in the EU Safety Gate alerts (dangerous non-food products), from the vendored index: the barcode exactly, or the brand and a whole model number (5+ characters with a letter and a digit, or 6+ digits) in the title, model, MPN or SKU. Local; not in the scorer yet |
| `service_identity` | Service sites, NL and US: what the site says about who it is. NL: a KvK number (Dutch businesses must show it on their website) of eight digits, a VAT number, the company name and legal form, a street address; US: the company name, a street address, and a licence number when the site says it is licensed ("says licensed but shows no licence number", never "unlicensed"); two different company names on one site. Nothing is looked up |
| `doorway_pages` | Service sites: landing pages for five or more towns ("Loodgieter Utrecht", "Plumber Austin", from the site's own links) with no street address, and whether its only number is a central one (national, toll-free, premium): the classic fake local business. On a single page: a site named after a trade and a town rather than a company, the page saying its tradesperson isn't necessarily local, only national numbers, an address in another town |
| `service_terms` | Service sites and profiles: payment in full or a deposit of half or more before the work, cash only, a call-out fee of (almost) nothing, a price set only on the doorstep, prices only on request, "24/7, there within 30 minutes", a premium-rate (0900, 1-900) number; a written quote or fixed price as the positive |
| `testimonials` | Service sites and profiles: testimonials (on a platform, its reviews) that repeat each other with a town or name swapped, all 5 stars, repeated or placeholder names, dates within a week |
| `platform_profile` | Platform profiles (Werkspot, Thumbtack, Angi, …): member since, jobs completed, rating and count, badges, response rate, as the platform shows them |
| `seller_profile` | Ads: the seller's account as the platform shows it (never the name): a new account (under 90 days), no ratings, low ratings, no verification on a new account, many ads on a young account, slow replies; and the positives (two years or more, well rated, verified) |
| `off_platform` | Ads: wording that takes the deal off the platform, in Dutch and English, over the ad's own text (not the platform's safety tips): contact by WhatsApp, Telegram, phone or e-mail; payment by bank transfer or payment link (not when paid at pickup), gift cards, crypto, money transfer or payment apps; a deposit or payment up front; "shipping only, no pickup"; "I'm abroad / on a ship"; refusing the platform's payment. Payments the ad rules out ("never pay in advance") don't count. Each finding quotes the ad; the content read adds its own judgement (`content.off_platform_push`) |
| `price_outlier` | Ads: the asking price against the same product new (an ad naming a barcode: the median shop price in `market_prices` over 90 days, at least two shops) or against a conservative floor for the goods non-delivery ads use most (recent phones, consoles, laptops, graphics cards) and rentals; accessories, broken items, swaps, wanted ads and starting bids are never compared |
| `duplicate_ads` | Ads: the same ad text (a simhash of its words) or the same phone number or e-mail address (keyed hash) in other ads checked in the last 90 days, from the ad store (`src/unfret/ad_store.py`: hashes, the platform and the day only; no seller, text or URL) |
| `reverse_image` | Every class (ads whenever they have a photo; service providers when their triage's rules call for it, on the first photo that isn't a logo): the main photo in Unfret's own image index: the same photo on other shops, or in the ABO catalogue; for an ad also in other ads (a copied ad, or someone else's photo of the item). pHash, dHash, a DINOv2-small embedding (run locally) and a colour grid must all agree; own shop and same listing ignored; stock images skipped. See [docs/image-index.md](docs/image-index.md). Deep tier only; not in the scorer yet |

The skills from `payment_methods` on (and `domain_age`'s founding claim, `shop_links`'s shared address) report new
features that the shipped scorer doesn't use: they reach the shopper as reasons and positives, and the scores stay
exactly as they were until the scorer is retrained with them (`evals.train` already reads them; the store and lookup
skills stay out of training, like `domain_age`).

**EU Safety Gate data.** `src/unfret/safety_gate_index.json.gz` is a compact index of the alerts in the Commission's
Safety Gate weekly reports ([XML list](https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/list/xml/en),
the "Weekly Reports - XML format" distribution of the data.europa.eu dataset
[rapex-rapid-alert-system-non-food](https://data.europa.eu/data/datasets/rapex-rapid-alert-system-non-food),
licence CC0 1.0, published by DG Justice and Consumers): alert number, case number, date, brand, product name, model,
barcodes, kind of product, risk type and level, for the alerts a page can be matched on (2005 to now). `make
safety-gate` adds the weekly reports it lacks (reports never change once published); the monthly "Vendored data"
workflow runs `make safety-gate-check`, which fails once a new report is out, so the refresh lands as a PR.

**What free checks record, and why a forged page can't poison it.** Unfret's own stores (the photo index, the price
history, the market prices, the review fingerprints) grow with every check. A quick check looks nothing up, but once
it has answered (in the background, never changing or slowing the answer) it records what a deep check would from
the same product page, through the same record paths: the main photo's fingerprints, today's price, the review
fingerprints (`src/unfret/recording.py`). Bounded: at most `UNFRET_QUICK_RECORD_QUEUE` pages at a time (more are
skipped), the photo check's own size (8 MB) and time (6 s) limits, two photos fingerprinted at a time. Quick checks
are free and anonymous, and the extension and the app send page content Unfret didn't fetch, so every row keeps its
provenance (a page Unfret fetched, client-supplied content; quick or deep) and a count of independent sources, and
counts in a verdict (a reused photo, a price far below market, a "was" price never charged, copied reviews) only
when Unfret fetched a page showing it, or `UNFRET_OBSERVATION_MIN_SOURCES` sources (each a pass and a network)
reported the same (`src/unfret/observations.py`). Which source vouched for which row is known only in memory, for
the current UTC day, as keyed hashes under a salt that is replaced daily and never stored: the database holds a
count, not a link from a pass or network to a page. A different value that doesn't count never overwrites one that
does. Each network records from at most `UNFRET_OBSERVATION_PAGES_PER_NETWORK_PER_DAY` quick checks or
client-supplied pages a day. Rows from before provenance was kept count as before. `GET /v1/admin/observations`
shows the counts.

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

Register an instance in `SKILLS` in [`src/unfret/graph/__init__.py`](src/unfret/graph/__init__.py). Its
`page_types` decide which page classes run it (default: product pages; `ANALYSED_PAGES`, `P2P_PAGES`,
`SERVICE_PAGES`, `SERVICE_SITES` in `graph/skills/__init__.py`), and `retry = True` marks one that calls the LLM or
fetches outside data, so transient errors are retried.

### The test shop

`/test-shop` is a made-up web shop (Nebulora Home, selling an invented LUMIQZEN star projector, in dollars, laid out
like a US Shopify store) that carries every trick Unfret catches, each one copied from real listings with the names
removed: a countdown that starts again on every load, "someone just bought this" pop-ups, a closing-down story
("everything must go"), a "compare at" price nobody paid (79% off), overseas shipping, returns to China, a withheld
return address, a restocking fee and a "Made in USA" claim in the small print, fees, a pre-ticked paid protection
add-on, dropshipping-app and supplier traces in the source, invented reviews (a burst of 2,847 in three weeks, repeats,
generic one-liners, reviews of other products, a review-import widget), hidden text addressed to AI, a generated
brand name and a keyword-stuffed title, and no company behind it. `/test-shop/checkout` adds the fees that show up
only at checkout. A banner on every view says what it is; the pages are `noindex`, served under the site's Content
Security Policy, and drawn (`scripts/test_shop_images.py`), with no real brand, shop, person or photo. Review dates are
relative to the day the page is served, so the burst stays a burst.

`/test-shop/sister-shop` is its sister shop (Quenvale Deals): the same made-up projector, cheaper ($17.99), under
another made-up brand (VELUNOX), with a re-uploaded copy of the same photo. A deep check of either page finds the
photo on the other ("Looks like the same product, sold elsewhere for $17.99", both photos side by side): test shop
pages are looked up only in the test shop's own photo index (`unfret.test_shop_photos`, made in memory from the
static photos, rows with negative IDs, thumbnails at `/v1/images/test-shop/<photo>`), never in the real one, and a real
page is never looked up there, so neither can match the other.

Use it to demo, screenshot and test: `tests/unit/test_test_shop.py` runs both tiers over it and pins the signals
each trick raises and the verdict. Its tricks are deliberate, so the test shop (by address: `/test-shop` on
`unfret.net`, its subdomains or this service, `unfret.test_shop`) never reaches what Unfret keeps about shops: the
fingerprint store, price history, market prices, review fingerprints, photo index and verdict store skip it, and a
report of it is answered `already_known` without being queued (`tests/integration/test_test_shop_stores.py`).

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
| `POST` | `/v1/invites/redeem` | Redeem a gift code `{code}` for the signed-in wallet or API key (not the buyer's; one a month): 5 checks |
| `GET` | `/v1/schema/page-facts` | JSON schema of `PageFacts`, for clients that extract facts themselves |
| `POST` | `/v1/shares/preview` \| `/v1/shares` | Share a result as a link, anonymously (no `Authorization`), see below |
| `GET` `DELETE` | `/v1/shares/{id}` | A shared result; delete it with `X-Delete-Code` |
| `POST` | `/v1/shares/{id}/object` | A shop owner's objection: hides the shared result until reviewed |
| `GET` | `/v1/health` | Liveness probe (version, commit, and the scorers' `model_version` / `quick_model_version`) |
| `GET` | `/v1/health/ready` | Readiness probe (checks the database; reports x402 and credits status, hot wallet balances, alerts) |
| `POST` | `/v1/check` | Pay-per-check for AI agents over [x402](https://www.x402.org), see below |
| `GET` | `/llms.txt` \| `/.well-known/x402` | Machine-readable description of `/v1/check`, its tiers and prices |

### Credits (browser extension and platforms)

There are no accounts. A wallet signs in by signing a plain-text [SIWS](https://github.com/phantom/sign-in-with-solana)
message (no spending rights; the nonce is stateless and expires in five minutes) and gets a session token. **The only
user state stored is `wallet → balance in micro-dollars`** (and whether it redeemed a gift code and the month of
its latest, and for a pass the affiliate it came through, if any): no emails, no purchase history (the chain is the
ledger), no record of what was analysed.

**Pricing.** One setting, `UNFRET_PRICE_PER_CHECK_USD` ($0.20), sets every price: an analysis costs one check in
credits, an x402 deep check costs one check, and credits are sold in packs of 25 and 100 checks
(`src/unfret/credits/packs.py`), after the free ones (a new pass's free deep checks, and quick checks). A pack's price
is its card price, $6 and $20 before the provider's sales tax/VAT (`UNFRET_CARD_PRICES_USD`, set by hand; a pack not
named there costs its value plus the card provider's worst-case cut of 6.5% + $0.50, rounded up to $0.50); paying
with SOL or USDC costs the pack's value ($5, $20) plus VAT where it applies, never more than by card (a card price
below the pack's value is refused at start-up). The card price is shown as *the* price and crypto as the discount
where there is one, never as a card surcharge (EU rules forbid surcharging consumer card payments). Either way the
account is credited the pack's value, so a check costs the same in credits however it was bought. Visitors from the
euro area (Cloudflare's country hint) pay by card in **euros** instead: €5.50, €17.50 (`UNFRET_CARD_PRICES_EUR`, set
by hand rather than converted, before VAT like the dollar prices). The 50-pack is no longer sold: what it credited,
and its gift codes, keep working, and an old `/checkout?checks=50` link picks the nearest pack and says so. Dutch visitors
also get a **"Pay with iDEAL | Wero"** button above the card one (see *Local payment methods* below).

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
wallet, or an API key) and records the right VAT. [`/checkout`](https://unfret.net/checkout) does it from a
browser wallet or a phone. A background scanner walks the hot wallet's finalized transactions (a second one its USDC
account) and credits every incoming transfer, top-level or inside a program call: what arrived (USDC at face value,
SOL at its dollar value at crediting time, Pyth SOL/USD, rounded down; it waits rather than guesses while the price is
stale) minus its VAT. Each credit leaves a sales record (day, country, net, rate, VAT, asset, amount, signature; no
IP, no wallet column) in the same database transaction, kept ten years; `GET /v1/admin/sales/<YYYY-Qn>` exports a
quarter as CSV. A transfer without a quote, a manual one, counts as a sale to an unknown country at the Dutch rate;
the older `unfret:credit:<beneficiary>` memo still names who gets it. The cursor moves by compare-and-swap, so each
transfer is credited exactly once. Sweeps, fees and x402 settlements are ignored.

[`/connect`](https://unfret.net/connect) signs a crypto wallet in and hands the session to the browser extension
(`window.postMessage`, relayed by the extension's content script); [`/checkout`](https://unfret.net/checkout) hands
every new API key it shows to the extension the same way (`unfret-key`, API contract), so a new user never pastes a key,
and [`/welcome`](https://unfret.net/welcome) is the page the extension opens once after installing. The pages are static HTML and vanilla JS with a
strict Content Security Policy: no framework, no third-party requests (no external fonts either: the one typeface,
Figtree, is served from `static/fonts/` with its SIL Open Font License, `font-src 'self'`), Wallet Standard
discovery written directly. An analysis costs one check ($0.20),
taken atomically before it runs and refunded when the page can't be analysed (score `-1`) or the analysis fails.

[`/check`](https://unfret.net/check) is the link checker for phones and browsers without the extension: it
sends only the link (`POST /v1/analyze/stream` with `{"url"}`), the server fetches the page itself before charging,
and a page it can't read (an error, a shop that refuses automatic visitors, a bot check, an empty shell that needs a
browser) is a free `422` that the page shows as such, never as a verdict. It installs as a web app with an Android
share target (**the phone app**); the sign-in is kept in the browser until it's removed, and there is no history.

**Words for shoppers.** The pages call an API key an **Unfret pass** (its ID shown as `ABCDE-FGHIJ`) and its recovery
code a **backup code**; "API key", `ufk_`/`ufr_` and wallets appear only where crypto buyers and developers look. A
pass bought in one browser gets to a phone by [`/my-checks`](https://unfret.net/my-checks) ("My checks": the pass or
wallet on this device, its checks, adding checks, a new pass from the backup code, and the wallet sign-in): "Use on
your phone" shows a QR code (`GET /v1/keys/phone-qr`, the key as `Authorization`) for `<site>/check#pass=<key>`; the
key sits in the fragment, which browsers never send, and `/check` keeps it and clears the address bar. A page's
verdict headline never repeats its label, which every client shows right above it.

There is no free trial: every new wallet or API key starts at zero checks and buys a pack. Grant an API key credit by
hand (support, a store reviewer's test checks) with `POST /v1/admin/keys/<key_id>/grant` and `{"usd_micros": n}`, at
most $10 a grant, with the admin token.

**Invites** (gift codes, `src/unfret/credits/invites.py`): every credited pack comes with gift codes to give away,
one per 25 checks (25 → 1, 100 → 4), each worth 5 checks. Any account but the buyer's redeems one with its own sign-in
(`POST /v1/invites/redeem`), each code once, and one gift code a calendar month (UTC) per pass or wallet: a second
one that month is refused (`INVITE_MONTHLY_LIMIT`) and stays unused. Only each code's SHA-256 is stored, with the day
issued and the day redeemed and a keyed hash of the buyer; per account only the month of its latest gift code: no
redeemer, no IP. The buyer sees the codes once: the purchase comes with a random claim token (never on chain or
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
| `UNFRET_POLAR_PRODUCTS` | The Polar product of every pack: `25=<product id>,100=<product id>`. A product for a pack no longer on sale (`50=…`) is ignored: no checkout starts for it and its orders are logged, not credited |
| `UNFRET_FIAT_WEBHOOK_SECRET` | The webhook endpoint's `whsec_…` secret |

**In Polar:** one one-time product per pack, priced at the pack's card price **in USD** before tax
(`UNFRET_CARD_PRICES_USD`: $6.00, $20.00), **plus a second price in EUR** equal to `UNFRET_CARD_PRICES_EUR` (€5.50,
€17.50; keep USD as the organisation's default currency). A product whose price differs from the pack's (say, not
yet changed in Polar after the setting) refuses its checkouts, so nobody pays the old price; the other packs sell on. A checkout is created for the pack's product with `currency:
"usd"`, or `"eur"` for visitors from the euro area (or when the page asks for it), discount codes off, our account id
(`key:<id>`) and this site's address (`UNFRET_PUBLIC_BASE_URL`) in its metadata and
`/checkout?paid=1&checkout={CHECKOUT_ID}` as the success URL (Polar fills in the checkout's id); nothing about the
buyer is sent. If Polar answers with another currency or price, the checkout is refused before
anyone pays; a refused euro checkout falls back to the dollar one (logged as `fiat_checkout_eur_fallback`), so a
product still missing its euro price costs euro-area buyers iDEAL | Wero, not the sale.

**Local payment methods** (`src/unfret/credits/local_methods.py`). Polar picks the methods its checkout shows from
the checkout's currency and the buyer's billing country, and with no country it shows cards only: iDEAL | Wero
appears only on a EUR checkout whose billing country is the Netherlands. So a visitor whose country has an entry in
`LOCAL_METHODS` (today only `NL` → `wero`, "iDEAL | Wero", billing country `NL`, `eur`) gets a second button on
`/checkout`, and its checkout is created in that currency with `customer_billing_address: {"country": "NL"}`
prefilled (Polar's `AddressInput` needs only the country; checked on dev: the checkout still asks for the country
only, `require_billing_address` stays false). Polar's page then shows an **iDEAL | Wero** tab next to the card tab
(the card tab is still selected first; the API has no way to preselect a method). The buyer can still change the
country on Polar's page. Nothing else differs: the same products,
prices, metadata, crediting and refunds. `/v1/pricing` says which method a visitor gets (`local_payment_method`), so
the page has no country list of its own. **To add a country**, add one entry to `LOCAL_METHODS` (visitor country →
method id, button label, billing country, currency), but first check by hand that Polar really offers that method:
create a checkout in that currency, set that billing country on Polar's page and look. An entry for a method Polar
doesn't show there would only put a card form behind a button that promises something else.

**Prices as shown:** Polar adds VAT on top of a product's price. The pages show each card price with the VAT Polar
will add for the chosen EU country (`/v1/pricing`'s `vat.card_rates_pct`: each country's standard rate, whatever
`UNFRET_VAT_MODE` says about our own crypto sales), so a price never goes up at Polar's checkout; outside the EU they
say that tax is added at checkout where it applies.

**Back in another browser:** the pass (API key) for a card purchase is made in the buying browser and kept only there,
with the claim token that collects the pack's invites. A bank app or a phone can return the buyer to another browser,
which has neither. There `/checkout` reads the checkout's id from the return link and calls
`POST /v1/credits/fiat-checkouts/{checkout_id}/pass`, which rotates the paid key (a new key and recovery code, the
same id and balance; the old key stops working) and hands over the invites: once, within 24 hours of the payment, and
only while the buying browser hasn't picked the purchase up itself (its status poll with the claim token after
payment sets `collected_at`), so an old link in a browser's history can't take over a pass in use. Only a pass made
for the purchase is ever rotated: a checkout whose key had been credited before (a top-up), or got credit since (a
code, another card order), is refused (`CHECKOUT_TOPPED_UP`), so a pass in use in the extension or on a phone is
never signed out from another browser. The buying browser, opened later, learns from the status (`reissued: true`)
that its key was replaced and forgets it. The page removes the checkout's id from the address bar at once.

**Webhook:** add an endpoint at `https://<host>/v1/webhooks/fiat` (format *Raw*), subscribed to **`order.paid`**,
**`order.refunded`**, **`refund.created`** and **`refund.updated`**. A paid order is credited the pack's value once,
and only if it is for a configured pack's product at exactly that pack's card price before tax (`net_amount`) in the
order's currency (USD, or EUR at the pack's euro price; the currency is kept in `fiat_payments.currency`); anything else credits nothing and logs `polar_order_not_credited` (order id, reason, currency and
amount; never the buyer) for a person to sort out in Polar. Refunds take back the refunded share of the credits
(`refunded_amount`, before tax); a refund that settles a dispute Polar lost takes back the whole order. Polar has no
webhook for disputes themselves. Other events are acknowledged and ignored.

**Dev and production in one Polar organisation:** each site gets its own webhook endpoint (and secret), and Polar
sends every order to both. A site credits only orders whose checkout it made (its own checkout record, or the
metadata's site); another site's order answers `other_site` and leaves no trace (no balance, no payment row).

### Affiliates and bonus codes

People who recommend Unfret can earn from it (`src/unfret/credits/affiliates.py`, the program at `/affiliates`):

- **Attribution.** An affiliate's link is `unfret.net/?ref=CODE`, on any page. The site keeps the code in the
  browser's own storage (no cookie, no third party) until a pass exists, then sends it once: with `POST /v1/keys`
  (`/check`, `/checkout`), or, for the extension's install pass, from `/welcome` through the extension
  (`POST /v1/affiliates/attribute`). A code typed at checkout counts too, once the purchase is paid. Only a pass that has
  never bought checks can be attributed, and only once (the first affiliate stays). Nothing is stored but the pass, the
  affiliate and the time; no IP address.
- **The buyer** gets 20% more checks on the first pack bought on an attributed pass (`UNFRET_AFFILIATE_BONUS_PCT`),
  credited through the ledger in the purchase's own transaction, once per pass.
- **The affiliate** earns 25% (`UNFRET_AFFILIATE_COMMISSION_PCT`) of the net revenue of every purchase on attributed
  passes for 12 months (`UNFRET_AFFILIATE_MONTHS`): for a card order, Polar's `net_amount` (before
  VAT/sales tax) less Polar's fee (`platform_fee_amount`; when Polar doesn't report it in the order's currency, the
  configured worst case `UNFRET_CARD_FEE_PCT` + `UNFRET_CARD_FEE_FLAT_USD`, marked as estimated); for crypto, what
  arrived less its VAT. Each purchase is recorded once (unique per order or transaction). Refunds reverse the refunded
  share and chargebacks all of it, in the month they happen. Dollars and euros are kept apart.
- **Bonus codes** made by the operator give more checks with a pack (`--bonus PCT`), optionally with a last day, a
  number of uses (counted at payment, atomically) and an affiliate to count the sales for. Codes never lower the
  price: card and crypto prices stay the same for everyone. One code per purchase; `/checkout` checks it before
  anyone pays (`POST /v1/credits/codes/check`).
- **Payouts** are made by hand, monthly. Commission counts as payable 30 days after the purchase
  (`UNFRET_AFFILIATE_HOLD_DAYS`, Polar's refund window). A payout needs $25 or €25 per currency
  (`UNFRET_AFFILIATE_MIN_PAYOUT_CENTS`, never converted), except for an affiliate who was switched off, who is paid
  everything earned. A refund or chargeback after a payout leaves a negative balance that the next payouts make up for.
- **Stats without an account**: each affiliate gets a private link `/affiliates/stats#ufa_…` (256 random bits, kept
  as a hash, replaceable and revocable) with their passes, first checks, purchases and commission per month, and
  never a pass, a buyer or a page.

Operator commands, against the running service's admin API (`UNFRET_URL`, `UNFRET_ADMIN_TOKEN[_FILE]`, as for
`make codes`):

```bash
make affiliates ARGS="create ANNA --name 'Anna (YouTube)' --note 'IBAN …'"   # prints her link and stats link
make affiliates ARGS="list"                     # codes, passes, owed, payout notes
make affiliates ARGS="report [--month 2026-12]" # per month: passes, first checks, purchases, earned, reversed, paid;
                                                # per affiliate now: due, carried over, held
make affiliates ARGS="paid ANNA --note 'SEPA ref'"   # records a payout of what is due now
make affiliates ARGS="off ANNA | on ANNA | note ANNA TEXT | stats-link ANNA | revoke ANNA | reverse EARNING_ID | delete ANNA"
make bonus-codes ARGS="create SPRING20 --bonus 20 --expires 2026-12-31 --uses 200 [--affiliate ANNA]"
make bonus-codes ARGS="list | off SPRING20 | on SPRING20"
```

**Gift codes** (the invites that come with a pack) work on any pass or wallet, except the one whose purchase issued
them, one a calendar month per pass or wallet. **Intro codes** are retired (`UNFRET_INTRO_CODES_ENABLED`).

### Sharing a result

A shopper can make their own check public as a link (`unfret.shares`, migration 16), and nothing on it says who
they are. Every assessed result carries a `share_token`: an HMAC over the product address (without fragment,
credentials and tracking parameters), a hash of the result as a share shows it, whether the check included the
checkout page, the product title and an expiry (7 days). It names no account. `POST /v1/shares/preview` shows
exactly what would be public; `POST /v1/shares` stores it. Both are sent **without `Authorization`** (a request with
one is refused) and the token must match the url and the result exactly, so a share can only show a verdict Unfret
really gave for that page. A check that included the checkout page is never shared.

What is public, and stored in `shared_results`: the verdict in public wording (`unfret.public_result`: the band as
what Unfret would tell a shopper about the listing, no score, never "scam" or "fraud"), without the page's own words (a reason whose detail quotes the page keeps only its title, a quoting
headline becomes the band's catalogue sentence, flags keep their labels but not their evidence), the product title,
the shop, the product link and the day of the check. Plus a SHA-256 hash of the delete code (shown once), the expiry
(90 days, on a day boundary) and, after an objection, why it is hidden. No account, wallet, IP address, time of day
or view count; expired rows go in the hourly housekeeping. Limits: 20 shares per visitor network per day (in
memory) and a daily cap.

`/r/{id}` shows it in the link checker's presentation (the same renderer, `static/check-render.js`): "Checked by an Unfret user on …; the
page may have changed", a `nofollow` link to the product, `X-Robots-Tag: noindex` and a robots meta tag. The sharer
deletes it with `DELETE /v1/shares/{id}` and `X-Delete-Code`; a shop owner's `POST /v1/shares/{id}/object` hides it
at once, and the operator reviews it under the admin token: `GET /v1/admin/shares` (hidden ones),
`POST /v1/admin/shares/{id}/unhide`, `DELETE /v1/admin/shares/{id}`. On `/check`, "Share this result" shows the
preview, then shares through the phone's share sheet or copies the link, and shows the delete code once.

### Requests from shop owners

`/objection` tells shop owners and sellers what Unfret keeps about shops and lets them object to a result, ask for a
correction, or ask for their site's data to be removed (`unfret.objections`, migration 27). Its form posts to
`POST /v1/objections` without sign-in; what is stored is what they sent (the kind of request, the site as host and
path, the message, a reply address if given) and the day, never an IP address. Limits: 5 per visitor network per
day, in memory, and a daily cap. The operator works through `GET /v1/admin/objections`, answers by e-mail, acts
(`DELETE /v1/admin/shops/{domain}`, `POST /v1/admin/contacts/forget`, or the report queue for a wrong result) and
deletes the request (`DELETE /v1/admin/objections/{id}`); the housekeeping deletes any still open after 90 days.

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
| LLM / image index | Never (classification by rules: a page without a product and a price is not assessed, and refunded); after the answer, the page's photo, price and reviews are recorded for later checks, not looked up | Yes |
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

**Ownership proof.** x402 directories (x402scan) mark an origin as verified when its `payTo` key signed the origin.
While x402 is on, the server signs the origin of `UNFRET_PUBLIC_BASE_URL` (`https://unfret.net`: no trailing slash,
as a browser's `URL.origin`) with the hot wallet's key, in process, once per start (`unfret.x402.ownership`). The
signature (ed25519 over the origin's UTF-8 bytes, base58) is published as `ownershipProofs` in the OpenAPI document
(`x-agentcash-provenance` and `x-discovery`) and in `/.well-known/x402`, one per `payTo` key the server holds; a new
hot key gives a new proof at the next start, and with x402 off there is none. The key signs nothing else this way:
only a bare origin, far too short to parse as a transaction. Anyone can check it against the hot wallet's address
(the `payTo` of the 402's Solana options). While Base is on, a second proof follows: the origin signed by the Base hot
wallet as an EIP-191 `personal_sign` message (`0x` hex; the message prefix keeps it from ever being a transaction),
checked against Base's `payTo`.

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

**With an Unfret pass instead of a wallet.** `POST /v1/check` also takes `Authorization: Bearer ufk_…` (an Unfret
pass, `POST /v1/keys`) in place of a payment, charged like `/v1/analyze`: a deep check uses one of the pass's checks
(free ones first, from the same daily budget, then bought ones), a quick check is free (`UNFRET_QUICK_CHECKS_PER_MINUTE`
per pass), and a cached answer costs a deep check like a fresh one, except for the pass that paid for that listing
within the hour. A page that can't be read, assessed or analysed is given back. The verdict is the same, plus
`charged`, `free_check`, `balance_usd_micros` and `free_checks_left`; no `PAYMENT-RESPONSE`. A request that carries
`PAYMENT-SIGNATURE` is always an x402 payment, and one without either header still gets the `402` above. A
**developer pass** (`POST /v1/keys` with `"developer": true`) comes with `UNFRET_DEVELOPER_FREE_CHECKS` (20) free deep
checks, one a day per visitor network.

Every error from the API is JSON with `detail` and `error_code` (validation errors add `errors`). For `/v1/check`:

| error_code | HTTP | charged | what to do |
| --- | --- | --- | --- |
| `PAYMENT_REQUIRED` | 402 | no | pay one of `accepts` and retry with PAYMENT-SIGNATURE; `error` says why (`reason`: why a simulation failed) |
| `INVALID_PAYMENT` | 400 | no | the PAYMENT-SIGNATURE header couldn't be decoded |
| `INVALID_API_KEY` | 401 | no | the Authorization header isn't a valid Unfret pass: fix it, or pay with x402 |
| `INSUFFICIENT_CREDITS` | 402 | no | the pass has no checks left: top it up, or pay with x402 |
| `FREE_CHECKS_PAUSED` | 429 | no | the pass has only free checks and today's are used: after 00:00 UTC, or pay |
| `VALIDATION_ERROR` | 422 | no | fix the request body (see `errors`) |
| `PAGE_UNAVAILABLE` | 422 | no | the page couldn't be fetched: send page_content |
| `PAGE_BLOCKED` | 422 | no | the page is a bot check: send the real page as page_content |
| `RATE_LIMITED` | 429 | no | wait Retry-After seconds |
| `PRICE_UNAVAILABLE` | 503 | no | nothing can be priced right now: retry after Retry-After |
| `SETTLEMENTS_PAUSED` | 503 | no | too many failed settlements: retry after Retry-After |
| `SERVICE_UNAVAILABLE` | 503 | no | payments or analysis are off: retry later |
| `SETTLEMENT_FAILED` | 402 | no | the network refused the payment (`reason` on Base): pay again |
| `SETTLEMENT_PENDING` | 402 | maybe | Solana: don't pay again; if it lands within 2 min its price is credited |
| `SETTLEMENT_MISMATCH` | 402 | yes | it landed differently than required: no verdict (alerted) |
| `GATEWAY_TIMEOUT` | 504 | refunded / no | the analysis timed out: Solana credits the price to the paying wallet; Base never settled it |
| `INTERNAL_ERROR` | 500 | refunded / no | the analysis failed: Solana credits the price to the paying wallet; Base never settled it |

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
move its value). Its cursor and every flagged transaction are kept in the database (per hot wallet), so a restart
continues where the last process stopped instead of reading the wallet's history again, and an alarm stays raised
across restarts; only the very first audit of a hot wallet, with no cursor yet, looks at its newest 100 transactions.
A compromise signal and post-settlement mismatches stay raised until the operator, having checked the flagged
transactions (and rotated the hot wallet if it was real), clears them. Readiness only counts the alerts; the operator
lists each transaction with its reason (`signature`, `reason`, `found_at`), then clears:

```bash
curl -H "Authorization: Bearer $UNFRET_ADMIN_TOKEN" https://…/v1/admin/x402/alerts
curl -X POST -H "Authorization: Bearer $UNFRET_ADMIN_TOKEN" https://…/v1/admin/x402/clear-alerts
```

A cleared transaction doesn't raise the alarm again when it is audited a second time; a new one always does.

**Changing the cold address.** Sweeps to the old cold address are in the hot wallet's history, and once
`UNFRET_COLD_ADDRESS` names another wallet the audit would take them for theft (an audit without a cursor, such as a
new database, reads them again). Declare the old one, with the moment sweeps to it stopped:

1. Note when the last sweep to the old address landed (an explorer, or `x402_sweep` / `x402_usdc_sweep` in the log).
2. Set `UNFRET_COLD_ADDRESS` to the new wallet, and add the old one to `UNFRET_FORMER_COLD_ADDRESSES`
   (comma-separated public addresses) with `UNFRET_FORMER_COLD_UNTIL` (ISO 8601 with a time zone, e.g.
   `2026-11-16T06:00:00Z`): a moment after that last sweep and before the new address takes over. Both are public,
   so they can go in the deploy file's clear environment. Start-up refuses former addresses without a cut-off, an
   address that isn't a public key, and the cold address itself as a former one.
3. Deploy, and check readiness with the operator token: `x402` is `ok`.

Outflows to a former cold address (SOL, and USDC into its USDC account) are accepted only from transactions whose block
time is before both the cut-off and the start of the running process: everything the service signs goes to the
current cold address, so a later outflow to an old wallet (which might be abandoned, or why the address was changed) is
flagged like any other, marked "a former cold address, after its cut-off", and so is a transaction without a block
time. Keep the cut-off tight: it is the window in which a stolen hot key could drain to the old wallet unnoticed. When
the cold address changes again, add that one too and move the cut-off to the newer change.

#### USDC on Base

`/v1/check` also takes **USDC on Base**, off by default (`UNFRET_X402_BASE_ENABLED=false`). While it is off nothing
about x402 changes. While it is on, the 402 lists one more option after Solana's: the `exact` scheme on
`eip155:84532` (Base Sepolia, the default `UNFRET_BASE_NETWORK`) or `eip155:8453` (Base mainnet, refused unless
`UNFRET_X402_BASE_ALLOW_MAINNET=true`), Circle's USDC, the tier's dollar price exactly (6 decimals, the same amount as
Solana's USDC option), `payTo` = the Base hot wallet (the address of `UNFRET_X402_BASE_HOT_KEY`),
`maxTimeoutSeconds` 300, and the token's EIP-712 domain in
`extra.name` / `extra.version` (`USDC`/`2` on Sepolia, `USD Coin`/`2` on mainnet, read from the contracts). The payer
signs an EIP-3009 `transferWithAuthorization`; a **facilitator** checks it (`/verify`) and submits it, paying the gas
(`/settle`). Neither Unfret nor the facilitator can change the amount or recipient, which the payer signed. The
default facilitator is Coinbase's (CDP, `UNFRET_BASE_FACILITATOR_URL` =
`https://api.cdp.coinbase.com/platform/v2/x402`), authenticated with a CDP secret API key
(`UNFRET_CDP_API_KEY_ID`, and an **Ed25519** `UNFRET_CDP_API_KEY_SECRET`, CDP's default key type; an ECDSA key leaves
Base off as `misconfigured`). Any other facilitator URL is used without a key (`https://x402.org/facilitator`:
Base Sepolia only). CDP settles 1,000 transactions a month free, then $0.001 each (verification is free).

- **A hot wallet, swept to cold, as on Solana.** `payTo` is the Base hot wallet: the address of
  `UNFRET_X402_BASE_HOT_KEY` (`0x` and 64 hex digits; a secret, never logged, never in the image or the repository).
  `UNFRET_BASE_PAY_TO` is optional: set to the hot wallet's address, it checks that the right key is deployed (a
  mismatch leaves Base `misconfigured`). The cold wallet, `UNFRET_BASE_COLD_ADDRESS`, is a plain public address whose
  key the server never holds, and not tied to the hot key: a software wallet now, a hardware wallet later, swapped
  by changing that one setting (see "Changing the Base cold address"). Addresses are checked at start-up (EIP-55
  checksum when mixed-case): a typo leaves Base `misconfigured` rather than paying into it, and so do a hot wallet
  that is the cold address or a former one. Base is off unless `UNFRET_X402_BASE_ENABLED`, the hot key and the cold
  address are all set.
- **Sweeps.** Once the hot wallet holds `UNFRET_BASE_USDC_SWEEP_THRESHOLD_UNITS` (5 USDC), or at least
  `UNFRET_BASE_USDC_SWEEP_DUST_UNITS` (0.5 USDC) with nothing swept for `UNFRET_BASE_USDC_SWEEP_MAX_INTERVAL_S` (a
  day), all of its USDC goes to the cold address as USDC in one transaction, `USDC.transfer(cold, balance)`, checked
  each `sweep_interval_s`. It is the only transaction the server ever signs on Base: one function signs, and it
  accepts only that shape, re-checked field by field (an EIP-1559 transaction on this chain to the USDC contract, no
  ETH value, no access list, `transfer` to the cold address, gas and fee within the caps). The gas is paid from the
  hot wallet's own ETH (a USDC transfer on Base costs a small fraction of a cent): keep a little ETH on it, about
  0.0005 ETH lasts many sweeps; below `UNFRET_BASE_MIN_GAS_WEI` (0.00001 ETH) `fee_reserve_low` asks for more, and
  a sweep the ETH can't pay waits. Payments go on meanwhile (the facilitator pays their gas), and USDC piling up
  beyond three times the threshold raises `usdc_above_threshold`. (A gasless sweep, a `transferWithAuthorization`
  the facilitator submits, isn't used: Coinbase documents its facilitator for settling x402 payments and says
  nothing of other transfers, each settlement is billed and may list the endpoint in its Bazaar, and the treasury
  would depend on a third party.) Fees: twice the latest base fee plus the node's suggested tip, waiting while that
  is above `UNFRET_BASE_MAX_FEE_PER_GAS_WEI` (1 gwei); the gas limit is the estimate plus 30%, at most
  `UNFRET_BASE_SWEEP_MAX_GAS` (120,000; the estimate itself runs the transfer against the latest state). One sweep
  at a time, across processes too (a Postgres advisory lock), and none while a transaction from the hot wallet is
  still unmined; each nonce can only be used once, so a retry never pays twice. The receipt is read from Base: it
  must show the USDC `Transfer` of the whole amount from the hot wallet to cold. A failed or refused sweep is logged
  at error level and retried after twice the interval, doubling up to an hour; three failures in a row raise
  `sweep_failing`.
- **Settled only when there is an answer** (x402's default `authorization` flow, unlike Solana's `upfront`): local
  checks (the option, recipient, exact value, a 32-byte nonce, `validBefore` at least the analysis timeout plus a
  minute ahead), the facilitator's `/verify` (signature, balance, simulation), fetching and analysing, then `/settle`.
  An unreadable page (`422`), a failed analysis (`500`/`504`, "you were not charged") or a page that can't be
  assessed (`200`, `"charged": false`) is never settled, so nothing on Base needs refunding. A settlement that was
  broadcast but not confirmed still gets its answer, with `PAYMENT-RESPONSE` `errorReason: "settlement_pending"`; a
  refused one answers `402 SETTLEMENT_FAILED` with `reason` and no verdict; each counts toward the same breaker. A
  facilitator that can't be reached at all answers `503 SERVICE_UNAVAILABLE` (nothing was sent, nothing charged).
- **Checked on chain.** After `/settle`, the transaction's receipt is read from Base (`UNFRET_BASE_RPC_URL`; the
  network's public RPC when empty): it must show the USDC `Transfer` from the payer to `payTo` of exactly the price
  and the authorization's `AuthorizationUsed`. Without a receipt, the authorization's state on chain decides. Anything
  else is `SETTLEMENT_MISMATCH`, no verdict, and the sticky `post_settlement_mismatch` alert.
- **Replays.** The same authorization (payer and nonce) gets the same answer for 15 minutes and is settled once; the
  USDC contract refuses a used nonce anyway.
- **Outflow audit.** Every `sweep_interval_s` the USDC contract's logs are read for `Transfer` and `Approval` events
  with the hot wallet as the first address, from the last audited block to `UNFRET_BASE_AUDIT_CONFIRMATIONS` (5)
  behind the head, in chunks of 1,000 blocks (a backlog over ten chunks is skipped and flagged). A transfer anywhere
  but the cold address (or a declared former one, before its cut-off), or any approval, raises
  `outflow_to_non_cold`: Base payments pause (`compromise_suspected`) until the operator clears it. ETH leaves an
  account only through a transaction its key signs, so the audit also reads the hot wallet's nonce: it may grow only
  by the sweeps to cold in those logs (and sweeps of this process that reverted); any other transaction the key
  signed (ETH sent anywhere, a call to any contract) raises the same alarm, and so does code at the hot wallet's
  address (an EIP-7702 delegation). Sweeping goes on while the alarm is raised: moving what is left to cold is the
  right thing to do with a stolen key. Zero-value transfers (anyone can make one "from" any address:
  address-poisoning spam) and zero approvals (revocations) are ignored. The cursor (block and nonce) and the flags
  live in the same tables as Solana's, keyed `<network>:<hot wallet>`, so an alarm survives restarts.
  `GET /v1/admin/x402/alerts` lists them under `base` (with the hot wallet, the cold address, its USDC and ETH);
  `POST /v1/admin/x402/clear-alerts?network=base` clears them (no `network`: both). After a real compromise: move
  what is left, deploy a new `UNFRET_X402_BASE_HOT_KEY` (a new `payTo`; the cold address stays), then clear.
- **Discovery.** With Base on, the 402 carries the Bazaar extension (`extensions.bazaar`: the request body and an
  example verdict, with its JSON Schema) and the resource's `tags` and `iconUrl`; Unfret sends its own copy of both
  with every settlement, so Coinbase's facilitator lists `/v1/check` in its Bazaar after the first settled payment
  (about 15 minutes later, testnet included). `/.well-known/x402` gains `networks` (Solana's top-level fields are
  unchanged), `/llms.txt` an "On Base" section, and `/developers` names both networks.

Readiness reports `x402_base` (`off`, `ok`, `mainnet_refused`, `misconfigured`, `rpc_unavailable`,
`compromise_suspected`) and `x402_base_alert_count`; with the operator token also the alerts and the hot wallet's
USDC and ETH (`base_hot_usdc_units`, `base_hot_eth_wei`).

**Changing the Base cold address** (a new software wallet, or the move to a hardware wallet) needs no new key: the
hot wallet and `payTo` stay the same. Sweeps to the old address are in the hot wallet's history, so declare it, as
on Solana:

1. Check that no sweep is pending (readiness with the operator token: no `sweep_failing`) and note when the last
   sweep to the old address landed (an explorer, or `x402_base_sweep` in the log).
2. Set `UNFRET_BASE_COLD_ADDRESS` to the new wallet, and add the old one to `UNFRET_BASE_FORMER_COLD_ADDRESSES`
   (comma-separated) with `UNFRET_BASE_FORMER_COLD_UNTIL` (ISO 8601 with a time zone): a moment after that last
   sweep and before the new address takes over. Both are public, so they can go in the deploy file's clear
   environment. Start-up refuses former addresses without a cut-off, one that isn't an EVM address, and the cold
   address (or the hot wallet) as a former one.
3. Deploy, and check readiness with the operator token: `x402_base` is `ok`.

A transfer to a former cold address counts as a sweep only if its block time is before both the cut-off and the start
of the running process; later it is flagged like any other, marked "a former cold address, after its cut-off".
`make e2e-base` (marker `e2e_base`, never part of the dev sweep or the production gate) checks against Base Sepolia:
with `UNFRET_E2E_BASE_FACILITATOR_URL` (plus `UNFRET_E2E_CDP_API_KEY_ID` / `_SECRET` for CDP), that the facilitator
takes Unfret's payment shape (a throwaway, unfunded payer gets as far as the balance check); with
`UNFRET_E2E_BASE_PAYER_KEY` (a Base Sepolia key holding test USDC), a quick check on dev paid in USDC on Base,
confirmed on chain and answered once for a retried payment; with `UNFRET_E2E_BASE_HOT_KEY` (a throwaway Base Sepolia
key holding a little Sepolia ETH and at least 0.5 test USDC; never a mainnet key, nor dev's own hot key) and
`UNFRET_E2E_BASE_COLD_ADDRESS`, one real sweep of that key's USDC to the cold address, then a clean audit. The MCP
server (`mcp/`) pays on Solana only for now.

The Solana side is off (`503`, or Base alone) until `UNFRET_HOT_WALLET_KEY` and `UNFRET_COLD_ADDRESS` (a cold wallet;
the server never holds its key) are set. Devnet is the default, and mainnet needs `UNFRET_X402_ALLOW_MAINNET=true`. Run one instance:
Solana's sweeper isn't coordinated across processes (Base's is). [`mcp/`](mcp/) is an MIT-licensed MCP server that pays these checks from an agent's own wallet within a
spend cap, or from an Unfret pass (`UNFRET_PASS`): `uvx unfret-mcp` from PyPI, listed in the MCP Registry as
`net.unfret/mcp`, released by the manual "Publish MCP server" workflow (`.forgejo/workflows/publish-mcp.yaml`).

### Remote MCP server (`/mcp`)

The API also serves MCP itself, over streamable HTTP at **`/mcp`** (`src/unfret/mcp_remote.py`), for hosted clients
that can't run a local process (Claude's connectors and the like). It is part of this service rather than a separate
one: its tools call the API's own routes in-process (`POST /v1/check`, `GET /v1/credits`, `GET /v1/pricing`) as the
MCP request's caller, so a check from MCP is authenticated, limited and charged by exactly the code behind
`/v1/check`, and there is nothing extra to deploy. The transport is **stateless with JSON responses** (the SDK's
`stateless_http`, `json_response`): any process can answer any request, and a deploy drops no sessions. It speaks the
current protocol (2026-07-28) and the initialize handshake of earlier versions.

It takes **an Unfret pass only**: `Authorization: Bearer ufk_…`, checked before the MCP layer (401 with
`WWW-Authenticate: Bearer realm="Unfret"` otherwise; too many bad passes from one network get 429 first). It never pays
from a wallet and has no code that could: quick checks are free, deep checks use one of the pass's checks, as on
`/v1/check` with a pass (where `/v1/check`'s per-minute limit counts per pass, so agents behind one host network don't
share it). Tools, each with a title and explicit `readOnlyHint` / `destructiveHint` / `idempotentHint` /
`openWorldHint`; the free check and the one that spends are separate tools, so a client can let the free one run
without asking:

| Tool | Hints | |
| --- | --- | --- |
| `check_store_quick` (`url`, `policy`, `page_content`) | read-only (free), idempotent, open world (reads a third-party page) | The verdict; or `status` (`page_unreadable`, `unavailable`, …) with a `next_step` |
| `check_store_deep` (`url`, `policy`, `page_content`) | not read-only (spends one of the pass's checks), not destructive, not idempotent, open world | The verdict, `charged` and what the pass has left; or `status` (`no_checks_left`, …) with a `next_step` |
| `get_checks_left` | read-only, idempotent, closed world | Deep checks left (free and bought), whether free ones are paused, where to buy more |
| `get_pricing` | read-only, idempotent, closed world | `GET /v1/pricing` |

```json
{"mcpServers": {"unfret": {"type": "http", "url": "https://unfret.net/mcp",
  "headers": {"Authorization": "Bearer ufk_…"}}}}
```

### Clients (`clients/`)

Two minimal clients, MIT licensed, each `check(url) → allow / warn / block` with an Unfret pass, mapped from the four
verdicts (Looks fine → allow, Worth a look → warn, Think twice and Serious concerns → block; the mapping is a
configurable policy), plus `checks_left` and a `new_developer_pass` helper that solves the proof of work:

| | Package | |
| --- | --- | --- |
| [`clients/python`](clients/python) | `unfret-client` (import `unfret_client`; sync and asyncio, on `httpx`) | `uv run --project clients/python pytest clients/python/tests` |
| [`clients/typescript`](clients/typescript) | `unfret-client` (ESM, no dependencies: `fetch` and Web Crypto) | `cd clients/typescript && npm ci && npm test` |

They are named `unfret-client` rather than `unfret` because `unfret` is this backend's own package. Unit tests run in
CI; the live tests run against a server and make a developer pass there:
`UNFRET_LIVE_URL=https://dev.unfret.net uv run --project clients/python pytest clients/python/tests -m live` and
`UNFRET_LIVE_URL=https://dev.unfret.net npm run test:live`.
Both are published by the manual "Publish clients" workflow (`.forgejo/workflows/publish-clients.yaml`; repo secrets
`PYPI_CLIENT_TOKEN` and `NPM_TOKEN`).

### Example: check before checkout (`examples/browser-use-checkout`)

A small [browser-use](https://github.com/browser-use/browser-use) agent that asks Unfret about the shop before it goes
to checkout, with the Python client: a hard gate in code before the agent starts (it is never handed a shop Unfret
says `block` for; `warn` asks the human) and an `unfret_check` tool for pages it finds on its way. It runs against the
test shop on dev, which Unfret should block. `uv run checkout_agent.py --gate-only` needs neither a browser nor an
LLM; the manual workflow "Examples against dev" (`.forgejo/workflows/examples.yaml`) runs that gate and the clients'
live tests against dev.

## Configuration

Settings are defined and validated in [`src/unfret/config.py`](src/unfret/config.py) (with
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)). Override any of them with a
`UNFRET_`-prefixed environment variable or a git-ignored `.env` file.

| Variable | Default | |
| --- | --- | --- |
| `UNFRET_LLM_API_KEY` | — | Required |
| `UNFRET_LLM_BASE_URL` | `https://api.scaleway.ai/v1` | Any OpenAI-compatible chat completions API |
| `UNFRET_LLM_MODEL` | `mistral-small-3.2-24b-instruct-2506` | Used by every node and skill; must support tool calling |
| `UNFRET_EMBEDDING_MODEL` | `bge-multilingual-gemma2` | Review/title similarity, same provider |
| `UNFRET_NETWORK_LOOKUPS` | `true` | RDAP, certificate transparency and EU VIES lookups about the shop, Wikidata and RDAP about the brand |
| `UNFRET_FINGERPRINTS` / `UNFRET_CONTACT_HASH_KEY` | `true` / derived from the session secret | Shop and review fingerprint stores; the key for contact and address hashes (no key and no session secret: contacts and addresses aren't stored) |
| `UNFRET_OBSERVATION_MIN_SOURCES` | `3` | Independent sources (a pass and a network, counted per day in memory) before a photo, price or review fingerprint seen only in client-supplied content counts in a verdict; one from a page Unfret fetched counts at once |
| `UNFRET_OBSERVATION_PAGES_PER_NETWORK_PER_DAY` | `300` | Quick checks and client-supplied pages per visitor network a day that may record observations (in memory, salted hashes); past it a check answers as always and records nothing |
| `UNFRET_QUICK_RECORD_QUEUE` | `16` | Quick checks whose photo, price and reviews are being recorded at once, after their answer; more are skipped (0: quick checks record nothing) |
| `UNFRET_IMAGE_MODEL_PATH` / `UNFRET_IMAGE_THREADS` | `models/dinov2-small-int8.onnx` / `1` | The image index's embedding model (baked into the container image, pinned by SHA-256) and its CPU threads; the photo check is off while the model file is missing. The catalogue seed is imported by hand: see docs/image-index.md |
| `UNFRET_MODEL_PATH` / `UNFRET_MODEL_QUICK_PATH` | — (the open baseline) | A scorer of your own for analyses and deep checks / for the x402 quick tier; see [The model](#the-model) |
| `UNFRET_MAX_PAGE_BYTES` | `2097152` | Larger pages are rejected with 413/422 |
| `UNFRET_SKILL_TIMEOUT_S` / `UNFRET_ANALYSIS_TIMEOUT_S` | `45` / `90` | A slow skill is skipped; a slow analysis returns 504 |
| `UNFRET_ANALYZE_FETCH_PER_MINUTE` | `10` | Pages fetched per account per minute for URL-only analyses (the `/check` link checker); unreadable pages are free, so this bounds them |
| `UNFRET_POSTGRES_URI` | `postgresql://unfret:unfret@localhost:5432/unfret` | Tables are created on startup |
| `UNFRET_SESSION_SECRET` | — | Signs nonces and session tokens; sign-in and credits are off (503) while empty |
| `UNFRET_SIWS_DOMAIN` | `unfret.net` | Sign-in messages must be bound to this domain |
| `UNFRET_PRICE_PER_CHECK_USD` | `0.20` | The price of a check: an analysis, an x402 deep check, and what packs are made of |
| `UNFRET_X402_PRICE_QUICK_USD_MICROS` | `10000` | The x402 quick tier ($0.01) |
| `UNFRET_PACKS` | `[25,100]` | Pack sizes in checks |
| `UNFRET_INVITE_PER_CHECKS` / `UNFRET_INVITE_CHECKS` | `25` / `5` | One gift code per this many checks in a pack (0: none issued), worth this many checks; a pass or wallet redeems one a month |
| `UNFRET_INTRO_DAILY_CHECKS_CAP` | unset (no cap) | Emergency brake for intro codes (`make codes`): when set, all codes together give at most this many checks a UTC day (0 pauses them all); each code already has its own max uses |
| `UNFRET_INTRO_CODES_ENABLED` | `false` | Intro codes are retired now that new passes get free checks: redeeming one answers `INVITE_ENDED`. `true` brings them back |
| `UNFRET_AFFILIATE_BONUS_PCT` / `UNFRET_AFFILIATE_COMMISSION_PCT` / `UNFRET_AFFILIATE_MONTHS` | `20` / `25` / `12` | Affiliates: more checks on an attributed pass's first pack; the affiliate's share of net revenue (after VAT and payment fees) of purchases on it, for this many months after attribution |
| `UNFRET_AFFILIATE_HOLD_DAYS` / `UNFRET_AFFILIATE_MIN_PAYOUT_CENTS` | `30` / `2500` | Commission counts as payable this many days after the purchase; a payout needs this much per currency ($25 / €25), except for an affiliate who was switched off |
| `UNFRET_CODE_CHECKS_PER_IP_PER_DAY` | `30` | Bonus-code look-ups and attributions per visitor network a day (in memory, salted hashes) |
| `UNFRET_AFFILIATE_RECORDS_RETENTION_DAYS` / `UNFRET_AFFILIATE_PASS_RETENTION_DAYS` | `2557` / `548` | Commission and payout records (bookkeeping: seven years); which affiliate a pass came through (eighteen months) |
| `UNFRET_FREE_CHECKS_PER_PASS` | `5` | Free deep checks on a new pass made with `"free": true` (the extension on install, `/check` on a first visit); 0 turns free passes off |
| `UNFRET_FREE_PASSES_PER_IP_PER_DAY` | `2` | New passes with free checks per visitor network a day (in memory, salted hashes); past it a pass is made empty |
| `UNFRET_DEVELOPER_FREE_CHECKS` / `UNFRET_DEVELOPER_PASSES_PER_IP_PER_DAY` | `20` / `1` | Developer passes (`"developer": true`): their free deep checks (from the same daily budget), and how many per visitor network a day; past it a pass is made empty |
| `UNFRET_FREE_DAILY_BUDGET_EUR` | `5.00` | All free deep checks together a UTC day, in euros of estimated cost; then "back tomorrow" (bought and quick checks go on). 0 turns free deep checks off |
| `UNFRET_FREE_CHECK_COST_EUR` | `0.005` | The estimated cost of one deep check (LLM and embedding tokens, retries included): the budget over this is the day's number of free deep checks (1,000 by default) |
| `UNFRET_QUICK_CHECKS_PER_MINUTE` | `10` | Free quick checks (rules only) per account, or per visitor network without one |
| `UNFRET_CHECK_EXAMPLES` | the test shop | Links `/check` offers to try, as JSON `[["label", "address"], ...]` (a path for this site) |
| `UNFRET_CARD_PRICES_USD` | `25=6.00,100=20.00` | Each pack's card price in dollars (before tax), set by hand; a pack not named here gets the derived price below. Never below the pack's value (refused at start-up). Each Polar product needs the same USD price |
| `UNFRET_CARD_FEE_PCT` / `UNFRET_CARD_FEE_FLAT_USD` / `UNFRET_CARD_PRICE_STEP_USD` | `6.5` / `0.50` / `0.50` | How the card price of a pack without one in `UNFRET_CARD_PRICES_USD` is derived |
| `UNFRET_CARD_PRICES_EUR` | `25=5.50,100=17.50` | Each pack's card price in euros (before VAT) for visitors from the euro area; one per pack on sale (others are ignored), or empty for dollars only. Each Polar product needs the same EUR price |
| `UNFRET_VAT_MODE` | `nl_under_threshold` | VAT on crypto purchases: Dutch VAT for all EU buyers, or `per_country` |
| `UNFRET_QUOTE_TOLERANCE_BPS` | `200` | How far a wSOL payment may drift from the quote |
| `UNFRET_HOT_WALLET_KEY` / `UNFRET_COLD_ADDRESS` | — | x402 and credit purchases (see above); off while empty |
| `UNFRET_FORMER_COLD_ADDRESSES` / `UNFRET_FORMER_COLD_UNTIL` | — | Earlier cold addresses (comma-separated) whose sweeps before the cut-off (ISO 8601) the outflow audit accepts; see "Changing the cold address" |
| `UNFRET_X402_BASE_ENABLED` / `UNFRET_X402_BASE_HOT_KEY` / `UNFRET_BASE_COLD_ADDRESS` | `false` / — / — | x402 in USDC on Base (see "USDC on Base"): the hot wallet's key (`payTo`) and the cold wallet; off unless all three are set |
| `UNFRET_BASE_PAY_TO` | — | Optional: the hot wallet's address, to check the deployed key (a mismatch leaves Base off) |
| `UNFRET_BASE_FORMER_COLD_ADDRESSES` / `UNFRET_BASE_FORMER_COLD_UNTIL` | — | Earlier Base cold addresses and the cut-off before which sweeps to them are accepted; see "Changing the Base cold address" |
| `UNFRET_BASE_NETWORK` / `UNFRET_X402_BASE_ALLOW_MAINNET` / `UNFRET_BASE_RPC_URL` | `base-sepolia` / `false` / the public RPC | Base mainnet stays refused until allowed; set a provider's RPC for production |
| `UNFRET_BASE_USDC_SWEEP_THRESHOLD_UNITS` / `_DUST_UNITS` / `_MAX_INTERVAL_S` | `5000000` / `500000` / `86400` | When the Base hot wallet's USDC is swept to cold |
| `UNFRET_BASE_MIN_GAS_WEI` / `UNFRET_BASE_MAX_FEE_PER_GAS_WEI` / `UNFRET_BASE_SWEEP_MAX_GAS` | `10000000000000` / `1000000000` / `120000` | The hot wallet's ETH for gas below which `fee_reserve_low` alerts; the largest fee per gas and gas limit a sweep uses |
| `UNFRET_BASE_FACILITATOR_URL` / `UNFRET_CDP_API_KEY_ID` / `UNFRET_CDP_API_KEY_SECRET` | CDP / — / — | The facilitator; Coinbase's needs a CDP secret API key (Ed25519) |
| `UNFRET_SHARES_PER_VISITOR_PER_DAY` / `UNFRET_SHARES_DAILY_CAP` / `UNFRET_SHARE_DAYS` | `20` / `1000` / `90` | Shared results: per visitor network per day, in total per day, and how long one stays up |
| `UNFRET_OBJECTIONS_PER_VISITOR_PER_DAY` / `UNFRET_OBJECTIONS_DAILY_CAP` / `UNFRET_OBJECTION_DAYS` | `5` / `200` / `90` | Requests from shop owners (`/objection`): per visitor network per day, in total per day, and how long an open one is kept |
| `UNFRET_TERMS_DRAFT` | `false` | Show a "draft, not in force" note at the top of `/terms` (on in dev until the terms are approved) |
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
make safety-gate       # add the EU Safety Gate's new weekly reports to the vendored index (safety-gate-check: CI, monthly)
make run               # uvicorn with auto-reload
make train             # retrain the scorer into the model store outside the repository (see The model)
make golden            # false alarms on well-known sellers' pages, both tiers (the corpus's golden set; see The model)
```

Unit tests cover the deterministic parts of every skill, the graph routing (with a stubbed LLM), SSRF protection, x402
payment verification and SIWS. Integration tests exercise the HTTP API end-to-end against a real Postgres (sign-in,
concurrent debits, the credits scanner), with the analysis graph replaced by a test double so no LLM calls are made.
A test that hangs fails the run after two minutes, printing every thread's stack (pytest-timeout).

### Pull requests and CI

CI (`.forgejo/workflows/ci.yaml`) checks a pull request once it is ready to merge, not every push while it's being
worked on:

- **Open it as a draft:** start the title with `WIP:` (for example `WIP: Shorter result card`). Forgejo shows it as
  a work in progress and CI skips it, however often you push.
- **Ready to merge? Take `WIP:` off the title.** That starts CI on the newest commit. Merge when it's green.
- A push to a ready pull request runs CI again and cancels the run still going for the commit before. Editing its
  description or title doesn't check it again: that run reports the result the commit already has (to retry a
  failure that wasn't the code's, re-run its run under Actions).
- Back to work on it: put `WIP:` back in front.

On `main`, runs queue one behind the other and are never cancelled, so a dev deploy is never cut off or dropped. A
run whose commit is no longer `main`'s newest when it starts checks nothing and doesn't deploy: the newest commit's
run checks and deploys it.

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
├── shares.py          # shared results: share tokens, storage
├── public_result.py   # a result in public wording (no score, never "scam")
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
| URL | https://dev.unfret.net | https://unfret.net |
| Solana | devnet (test money) | **mainnet** |
| Kamal destination | `config/deploy.dev.yml` (containers `unfret-web-dev-<sha>`, DB `unfret-dev-db`) | `config/deploy.production.yml` (`unfret-web-production-<sha>`, `unfret-prod-db`) |
| Deployed | **automatically** on every push to `main`, after the checks (`.forgejo/workflows/ci.yaml`, job `deploy-dev`) | **only by hand** (`.forgejo/workflows/deploy-production.yaml`) |
| Repo secrets | `DEV_POSTGRES_PASSWORD`, `DEV_SESSION_SECRET`, `DEV_HOT_WALLET_KEY`, `DEV_COLD_ADDRESS`; optional `DEV_ADMIN_TOKEN`, and for card payments `DEV_POLAR_ACCESS_TOKEN`, `DEV_POLAR_ENVIRONMENT`, `DEV_POLAR_PRODUCTS`, `DEV_FIAT_WEBHOOK_SECRET`; for the e2e sweep (production's gate) `DEV_E2E_PAYER_KEY`, `DEV_E2E_KEY` and `DEV_ADMIN_TOKEN` | `PROD_POSTGRES_PASSWORD`, `PROD_SESSION_SECRET`, `PROD_HOT_WALLET_KEY`, `PROD_COLD_ADDRESS`, `PROD_SOLANA_RPC_URL` (all required); for card payments `PROD_POLAR_ACCESS_TOKEN`, `PROD_POLAR_PRODUCTS`, `PROD_FIAT_WEBHOOK_SECRET` (all three or none; Polar's production environment is fixed in `config/deploy.production.yml`) |

**x402 on Base** is off in both environments (`UNFRET_X402_BASE_ENABLED: "false"` in dev's deploy file; production
doesn't set it). Dev reads the optional repo secrets `DEV_BASE_HOT_KEY` (a Base Sepolia key holding a little Sepolia
ETH for the sweeps' gas), `DEV_BASE_COLD_ADDRESS`, `DEV_BASE_PAY_TO` (optional: the hot key's address, as a check),
`DEV_CDP_API_KEY_ID` and `DEV_CDP_API_KEY_SECRET`; once they exist (or `UNFRET_BASE_FACILITATOR_URL` names a keyless
testnet facilitator), dev is switched on in a PR that sets `UNFRET_X402_BASE_ENABLED: "true"`, on Base Sepolia only.
Production needs its own plumbing first (`PROD_BASE_HOT_KEY`, `PROD_BASE_COLD_ADDRESS`, `PROD_CDP_API_KEY_ID`,
`PROD_CDP_API_KEY_SECRET`, `PROD_BASE_RPC_URL`, with `UNFRET_BASE_NETWORK: base-mainnet` and
`UNFRET_X402_BASE_ALLOW_MAINNET: "true"`), only on the owner's decision.

The `LLM_API_KEY` repo secret is shared. The Kamal secrets files map these onto the app's `UNFRET_*` settings
(`.kamal/secrets-common`, `.kamal/secrets.dev`, `.kamal/secrets.production`).

**The scorer** never goes into the image, which is built from this public repository. Both deploys run
`scripts/private-model.sh`: with the `MODEL_READ_TOKEN` repo secret (read-only access to the private model store),
they fetch the store's current release, copy it to the server (`/home/kamal/unfret-model/<destination>`, swapped in
whole) and mount it read-only at `/app/private-model`, setting `UNFRET_MODEL_PATH` / `UNFRET_MODEL_QUICK_PATH`;
without the secret, or when the store names no release, the baseline in the image runs. After the deploy they wait
for `/v1/health` to report the expected `model_version` and `quick_model_version`. The production deploy also
refuses unless dev, which just passed the sweep, runs exactly the release it fetched; a new scorer therefore reaches
production by deploying dev again (*Actions → CI → Run workflow* on `main`), testing it, then running the production
deploy.

**Changing a cold address** (`DEV_COLD_ADDRESS`, `PROD_COLD_ADDRESS`): in the same change, add the old address and the
moment sweeps to it stopped to that destination's `config/deploy.<destination>.yml`, under `env.clear`
(`UNFRET_FORMER_COLD_ADDRESSES`, `UNFRET_FORMER_COLD_UNTIL`; see [the x402 section](#pay-per-check-for-agents-x402)).
Without them, the outflow audit flags the old sweeps, x402 pauses (`compromise_suspected`) and the production gate's
readiness check fails. Dev lists its first cold address, which it swept to until 16 Nov 2026.

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

- dev runs the scorer the private model store names now (see "The scorer" above).

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
`llms.txt`; API keys (proof-of-work creation, an empty key's 402, an operator grant, recovery); analyses charged once
per pass (the same pass's repeat free, a cached result charged to another pass, which without a check gets a 402),
streamed, and every page the server can't read (`PAGE_BLOCKED`, `PAGE_UNAVAILABLE`,
`INVALID_URL`, `PAGE_TOO_THIN`, a validation error) free; sign-in with the payer's wallet; x402 with the MCP client
(quick in USDC, deep in wSOL, confirmed on devnet, a replayed payment answered once and debited once, an underpayment
and a malformed header refused, a bot-check page free); credit packs bought in USDC (memo) and in SOL by the Solana Pay
reference alone; card top-ups (a Polar checkout page, then made-up signed `order.paid`/`order.refunded` webhooks: dev
runs **live** Polar, so no real order is ever completed); the retired showcase's 404 and the operator endpoints' 401. Fixture pages
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

## Roadmap

- **x402 on Base.** Pay-per-check is Solana only (USDC and wSOL). Most x402 tooling for agents starts from an EVM
  wallet, so `/v1/check` should also accept USDC on Base: a second entry in the 402's `accepts`, a facilitator or
  our own settlement on Base, and the MCP server able to pay from either. `/developers` names it as coming next.

## Limitations and responsible use

- Scores come from a logistic regression over skill features (see [The model](#the-model); trained with
  `make train`). They are a calibrated signal, not a verdict, and can be wrong in both directions. Part of the training data is synthetic (marketplace pages rendered from public review data),
  so marketplace accuracy on real pages is likely lower than measured.
- Page content is passed to the LLM, so a hostile page can attempt prompt injection to influence its own score. It is
  delimited as untrusted data, and attempts to address the model are detected and count against the page.
- Registration data (RDAP) is often redacted; the first-certificate date is a second, registry-independent age.

## Data and models

- The image index is seeded with photo fingerprints from [Amazon Berkeley Objects](https://amazon-berkeley-objects.s3.amazonaws.com/index.html) © Amazon.com, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); only derived fingerprints, ASINs and brands are kept.
- Photo embeddings come from [DINOv2-small](https://huggingface.co/facebook/dinov2-small) (Apache-2.0), run locally through ONNX Runtime.

## License

The code is [MIT](LICENSE): this backend (including the trained scorers in `src/unfret/scoring/`), the MCP server in
[`mcp/`](mcp/), the [browser extension](https://git.kroshtan.com/kroshtan/unfret_extension) and the
[Android app](https://git.kroshtan.com/kroshtan/unfret_android). Not published: the labelled training corpus (scraped
pages and third-party review data that can't be redistributed) and the image index seed (see
[The model](#the-model)).
