# Changelog (unfret-client, Python)

## [0.1.0]

First release (not yet on PyPI).

* `Unfret(pass_=…).check(url, tier="deep", page_content=None)` → a `Decision`: `action` (`allow`, `warn` or
  `block`) from the verdict's band, with a configurable `policy`; the `band`, its `label`, the `flags` and Unfret's
  whole answer (`verdict`).
* `AsyncUnfret`, the same for asyncio.
* `checks_left()`, and `new_developer_pass()` (a pass with 20 free deep checks; solves the proof of work).
* Errors as exceptions: `InvalidPassError`, `NoChecksLeftError`, `RateLimitedError`, `PageUnreadableError`, all
  `UnfretError` (never charged).
