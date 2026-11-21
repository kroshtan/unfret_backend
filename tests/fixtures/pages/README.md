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
| `amazon-nl-mobile-reference.html` | The mobile Amazon.nl product page, as a phone and the Android app's WebView get it (checked 2026-11-20; ids, brand and wording our own): the price's offscreen copy empty (the price in an accessibility label and in its visible parts), a sponsored product's price first in the page, the "Adviesprijs" reference price, a price per unit, the mobile rating link and review count, the combined "Verzender / Verkoper" row named by `offer-display-attribute-name`, and the top reviews (`mobley-review-content`, `reviewTitle`, `reviewRichContentContainer`); no star histogram | `tests/unit/test_facts_reviews_prices.py`; the extension's parity fixtures |
| `amazon-de-mobile-uvp.html` | The mobile Amazon.de price box: the "UVP" reference price after a price per unit, a sponsored price first | `tests/unit/test_facts_reviews_prices.py` |
| `amazon-com-typical-price-reviews.html` | The desktop Amazon.com price box with a "Typical price", the star histogram's rows and the top reviews under the current hooks | `tests/unit/test_facts_reviews_prices.py` |
| `bolcom-reviews-partner.html` | A bol.com product page (checked 2026-11-20; names and wording our own): the offer's price and "Adviesprijs" for a screen reader, a partner seller with its score, a "Meestal" price of another offer, the review summary, the star distribution's bars and two reviews | `tests/unit/test_facts_reviews_prices.py` |
| `woocommerce-sale-reviews.html` | A WooCommerce product page without structured data: the sale price (`<del>`/`<ins>`), the rating and count, two reviews, and a related product's sale price | `tests/unit/test_facts_reviews_prices.py` |
| `shopify-judgeme-reviews.html` | A Shopify product page with Judge.me's review widget (average, count, histogram, reviews) and a Loox badge | `tests/unit/test_facts_reviews_prices.py` |
| `generic-jsonld-strikethrough.html` | A shop's Product JSON-LD with a `StrikethroughPrice` price specification, an `ImageObject` by `contentUrl`, and its rating in a separate `AggregateRating` block | `tests/unit/test_facts_reviews_prices.py` |
| `generic-microdata-rating.html` | A product's rating in microdata (`aggregateRating`) and a Stamped badge | `tests/unit/test_facts_reviews_prices.py` |
| `ebay-was-price.html` | An eBay item's price section with a struck "List price" | `tests/unit/test_facts_reviews_prices.py` |
| `shopify-gallery.html` | A Shopify product page's photos (Dawn-style media gallery with `srcset`, a lazy URL template, thumbnails, the theme's `ProductJson-…` with one photo only there, a related product, logo, banner and payment icons) | `tests/unit/test_facts.py` (the product gallery) |
| `woocommerce-gallery.html` | A WooCommerce product page: one photo in the JSON-LD, the gallery's `data-large_image` copies (`-scaled`, `-600x800` thumbnails), related products | `tests/unit/test_facts.py` |
| `ebay-item-gallery.html` | An eBay item page: JSON-LD `image` array, the `ux-image-carousel` with `data-zoom-src`, similar items | `tests/unit/test_facts.py` |
| `bolcom-gallery.html` | A bol.com product page's filmstrip (`data-zoom-image-url`, a video's still, recommendations) | `tests/unit/test_facts.py` |
| `aliexpress-gallery.html` | An AliExpress item's `imagePathList` in its page data, thumbnail copies on the page | `tests/unit/test_facts.py` |
| `temu-gallery.html` | A Temu goods page's `gallery` in its page data | `tests/unit/test_facts.py` |
| `script-shell.html` | A single-page app's empty shell: no visible text, price or product data until scripts run | the e2e sweep (`PAGE_TOO_THIN`) |

The e2e sweep (`tests/e2e`) has dev fetch these from the forge at the commit dev runs
(`https://git.kroshtan.com/kroshtan/unfret_backend/raw/commit/<sha>/tests/fixtures/pages/<file>`), so their content
can't change under a run; renaming or deleting one breaks the sweep.
