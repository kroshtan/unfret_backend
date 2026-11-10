# Product page fixtures

Hand-made pages that reproduce the structure of real product pages as Unfret's server-side fetch
(`unfret.page_fetch.fetch_page`) receives them, for regression tests of the whole pipeline. They are written for
these tests (text, markup and data are our own); no third-party page content, images or markup is copied.

| File | Reproduces | Used by |
| --- | --- | --- |
| `shopify-productgroup-us.html` | A US brand's own Shopify product page (allbirds.com, checked 2026-11-09): schema.org `ProductGroup` JSON-LD with variants carrying the offers, the rating in a separate `AggregateRating` block, Open Graph price in USD, footer links to help, returns, contact and privacy, and no company name or address on the page | `tests/unit/test_storefront_precision.py` |
