# Label collection fixtures

Offline inputs for the tests of the new-class label collectors (`evals/sources/patterns.py`, `osm_trades.py`,
`acm_traders.py`; `tests/unit/test_label_collection.py`). Hand-made: the text, markup and data are our own, shaped
like what the collectors receive; no third-party content is copied.

| File | Reproduces |
| --- | --- |
| `overpass-trades.json` | An Overpass API response: craft businesses with and without their own website, a duplicate domain, a social-media "website", a shop that isn't a craft |
| `warning-page.html` | A consumer-warning page in Dutch describing off-platform contact, a seller's payment link and the platform's own payment |
