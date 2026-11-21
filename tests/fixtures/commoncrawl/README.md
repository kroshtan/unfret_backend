Common Crawl's answers as its servers give them, for `tests/unit/test_common_crawl.py`. Made up for the tests, after
the shape of real ones: `collinfo.json` (the crawls and their dates), the crawls' index answers for a home page
(`index.json`, JSON lines per crawl; a crawl not in it answers 404, "No Captures found") and for the first-copy probes
(`index_first.json`). The WARC records themselves are built by the test from the pages in `../wayback` (one gzip
member each, as a byte range of a WARC file is). No real shop or person is named.
