# Product page fixtures

Hand-made pages that reproduce the structure of real product pages as Unfret's server-side fetch
(`unfret.page_fetch.fetch_page`) receives them, for regression tests of the whole pipeline. They are written for
these tests (text, markup and data are our own); no third-party page content, images or markup is copied.

| File | Reproduces | Used by |
| --- | --- | --- |
| `shopify-productgroup-us.html` | A US brand's own Shopify product page (allbirds.com, checked 2026-11-09): schema.org `ProductGroup` JSON-LD with variants carrying the offers, the rating in a separate `AggregateRating` block, Open Graph price in USD, footer links to help, returns, contact and privacy, and no company name or address on the page | `tests/unit/test_storefront_precision.py`; the e2e sweep's readable product page |
| `bot-check-interstitial.html` | A bot-check interstitial served instead of the page (Cloudflare-style "Just a moment..." title and challenge script path) | the e2e sweep (`PAGE_BLOCKED`, free x402 422) |
| `marketplace-piece-count-nl.html` | A Dutch marketplace listing of a felt-tip pen set (checked 2026-11-20; brand, seller and wording our own): the title's piece count ("91-delig") is padded with clips, and the parts it lists (80 pens and 12 clips) make 92 | `tests/unit/test_claim_audit.py` (the claim audit's piece count) |
| `script-shell.html` | A single-page app's empty shell: no visible text, price or product data until scripts run | the e2e sweep (`PAGE_TOO_THIN`) |

The e2e sweep (`tests/e2e`) has dev fetch these from the forge at the commit dev runs
(`https://git.kroshtan.com/kroshtan/unfret_backend/raw/commit/<sha>/tests/fixtures/pages/<file>`), so their content
can't change under a run; renaming or deleting one breaks the sweep.
