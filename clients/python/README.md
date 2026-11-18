# unfret-client

A minimal Python client for [Unfret](https://unfret.net): before an AI agent buys, ask whether the store or product
page shows signs of a scam, dropshipped generic goods, fake reviews or pressure tricks, and get back what to do.
MIT licensed.

```bash
pip install unfret-client
```

The first release on PyPI comes with Unfret's next release; until it's there, install it from the source:
`pip install "unfret-client @ git+https://git.kroshtan.com/kroshtan/unfret_backend.git#subdirectory=clients/python"`.
For other languages, see [unfret.net/developers/http](https://unfret.net/developers/http).

```python
from unfret_client import Unfret

unfret = Unfret(pass_="ufk_…")  # or set UNFRET_PASS
decision = unfret.check("https://shop.example/p/42")
decision.action  # "allow", "warn" (ask your human first) or "block"
decision.label  # "Looks fine", "Worth a look", "Think twice" or "Serious concerns"
decision.reasons  # the evidence, one line per flag
```

It needs an **Unfret pass** (`ufk_…`). A deep check (the default: everything, including an AI read of the listing,
its reviews and photos) uses one of the pass's checks; `tier="quick"` (rules on the page itself) is free. Get a
**developer pass with 20 free deep checks** at [unfret.net/developers](https://unfret.net/developers), or in code:

```python
issued = Unfret().new_developer_pass()  # solves a short proof of work; keep issued["key"] and issued["recovery_code"]
```

Buy more checks for a pass at [unfret.net/checkout](https://unfret.net/checkout). (Agents with a Solana wallet can
also pay per check over x402 without a pass; see [unfret.net/developers](https://unfret.net/developers).)

## From verdict to action

The action comes from the verdict a shopper would see:

| Verdict (`band`) | Default action |
| --- | --- |
| Looks fine (`reliable`) | `allow` |
| Worth a look (`concerns`) | `warn` |
| Think twice (`careful`) | `block` |
| Serious concerns (`junk_or_scam`) | `block` |

Change it with `policy`, e.g. `Unfret(policy={"careful": "warn"})`. A page Unfret couldn't assess (not a store or
product page) is `warn` (`unassessed=` to change). `decision.verdict` has Unfret's whole answer, including
`risk_score`, `confidence`, its own `action` and what the pass has left (`free_checks_left`, `balance_usd_micros`).

## Options and errors

`check(url, tier="deep" | "quick", page_content=None)`: send `page_content` (the page's HTML) for marketplaces such as
Amazon, which refuse server-side visits. `checks_left()` returns what the pass has left. `AsyncUnfret` is the same
for asyncio. The server is `https://unfret.net` (`base_url=`, or `UNFRET_API_URL`; `https://dev.unfret.net` is the
test server).

A refused call raises an `UnfretError` and is never charged: `InvalidPassError` (401), `NoChecksLeftError` (402: buy
checks, or use `quick`), `RateLimitedError` (429; `retry_after` in seconds, also when free deep checks are paused
until 00:00 UTC) and `PageUnreadableError` (send `page_content`). Each has `status`, `code` and `detail`.

Source: [git.kroshtan.com/kroshtan/unfret_backend](https://git.kroshtan.com/kroshtan/unfret_backend/src/branch/main/clients/python)
(mirror on [GitHub](https://github.com/kroshtan/unfret_backend/tree/main/clients/python)).
