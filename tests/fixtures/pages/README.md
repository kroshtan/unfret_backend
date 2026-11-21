# Product page fixtures

Hand-made pages that reproduce the structure of real product pages as Unfret's server-side fetch
(`unfret.page_fetch.fetch_page`) receives them, for regression tests of the whole pipeline. They are written for
these tests (text, markup and data are our own); no third-party page content, images or markup is copied.

| File | Reproduces | Used by |
| --- | --- | --- |
| `shopify-productgroup-us.html` | A US brand's own Shopify product page (allbirds.com, checked 2026-11-09): schema.org `ProductGroup` JSON-LD with variants carrying the offers, the rating in a separate `AggregateRating` block, Open Graph price in USD, footer links to help, returns, contact and privacy, and no company name or address on the page | `tests/unit/test_storefront_precision.py`; the e2e sweep's readable product page |
| `bot-check-interstitial.html` | A bot-check interstitial served instead of the page (Cloudflare-style "Just a moment..." title and challenge script path) | the e2e sweep (`PAGE_BLOCKED`, free x402 422) |
| `marketplace-piece-count-nl.html` | A Dutch marketplace listing of a felt-tip pen set (checked 2026-11-20; brand, seller and wording our own): the title's piece count ("91-delig") is padded with clips, and the parts it lists (80 pens and 12 clips) make 92 | `tests/unit/test_claim_audit.py` (the claim audit's piece count) |
| `amazon-nl-gallery.html` | An Amazon.nl product page's photos (desktop, checked 2026-11-20; ids, brand and wording our own): the image block's script naming the gallery as a JSON string to `parseJSON` (one photo without a high-resolution copy, the main photo named twice), the thumbnails, a video's thumbnail, banners, a sprite, a badge, A+ content and tracking pixels | `tests/unit/test_facts.py` (the Amazon gallery) |
| `amazon-com-gallery-json.html` | The same image block on Amazon.com with the gallery as a JSON literal, one entry a pixel | `tests/unit/test_facts.py` |
| `amazon-nl-gallery-mobile.html` | The mobile Amazon.nl page's photo carousel (`data-a-hires` per slide, a video slide, a video card outside it) | `tests/unit/test_facts.py` |
| `amazon-de-gallery-thumbnails.html` | An Amazon.de page with only the thumbnails (no image block script): a pixel, a 360° icon and a video among them | `tests/unit/test_facts.py` |
| `shopify-gallery.html` | A Shopify product page's photos (Dawn-style media gallery with `srcset`, a lazy URL template, thumbnails, the theme's `ProductJson-…` with one photo only there, a related product, logo, banner and payment icons) | `tests/unit/test_facts.py` (the product gallery) |
| `woocommerce-gallery.html` | A WooCommerce product page: one photo in the JSON-LD, the gallery's `data-large_image` copies (`-scaled`, `-600x800` thumbnails), related products | `tests/unit/test_facts.py` |
| `ebay-item-gallery.html` | An eBay item page: JSON-LD `image` array, the `ux-image-carousel` with `data-zoom-src`, similar items | `tests/unit/test_facts.py` |
| `bolcom-gallery.html` | A bol.com product page's filmstrip (`data-zoom-image-url`, a video's still, recommendations) | `tests/unit/test_facts.py` |
| `aliexpress-gallery.html` | An AliExpress item's `imagePathList` in its page data, thumbnail copies on the page | `tests/unit/test_facts.py` |
| `temu-gallery.html` | A Temu goods page's `gallery` in its page data | `tests/unit/test_facts.py` |
| `amazon-de-variants-bait.html` | An Amazon.de listing whose "Größe" picker groups a phone case, a screen protector, a charging cable and a bundle (checked 2026-11-21 against amazon.nl's and amazon.de's twister; ASINs, brand and wording our own): the twister's script data with nothing selected, each swatch's price, and a price range from the cable's €6.99 | `tests/unit/test_variant_check.py` (the bait price) |
| `amazon-com-variants-counts.html` | Amazon.com's classic twister (`#variation_size_name`, `data-defaultasin`, `data-dp-url`, swatch prices) for battery pack sizes, one selected | `tests/unit/test_variant_check.py` (plain amounts cost nothing) |
| `shopify-variants-refill.html` | A Shopify shop's toothbrush whose "Style" options put a brush head refill first and at the price shown, with Shopify's `var meta` and `ProductJson-…` (prices in cents) and a radio picker | `tests/unit/test_variant_check.py` |
| `woocommerce-variants-sizes.html` | A WooCommerce variable product: `data-product_variations`, the "Maat" select with a default, and an AggregateOffer price range | `tests/unit/test_variant_check.py` |
| `generic-productgroup-colours.html` | A schema.org `ProductGroup` with `variesBy` colour and three variants, one out of stock | `tests/unit/test_variant_check.py` |
| `walmart-variants-grouped.html` | A Walmart listing's `__NEXT_DATA__` `variantCriteria` and `variantsMap`: a patio set, a single chair and a cushion as "Size" | `tests/unit/test_variant_check.py` (grouping alone) |
| `generic-select-picker.html` | A shop's own size select with a price at the end of each label, next to a language select in the header and a quantity select | `tests/unit/test_variant_check.py` |
| `script-shell.html` | A single-page app's empty shell: no visible text, price or product data until scripts run | the e2e sweep (`PAGE_TOO_THIN`) |

The e2e sweep (`tests/e2e`) has dev fetch these from the forge at the commit dev runs
(`https://git.kroshtan.com/kroshtan/unfret_backend/raw/commit/<sha>/tests/fixtures/pages/<file>`), so their content
can't change under a run; renaming or deleting one breaks the sweep.
