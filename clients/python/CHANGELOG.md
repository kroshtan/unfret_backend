# Changelog (unfret-client, Python)

## [0.1.1]

* `new_developer_pass()` asks for a developer challenge (`GET /v1/keys/challenge?developer=true`): the server now
  makes a developer pass only with one, a harder proof of work (a few seconds in Python). 0.1.0's call is refused
  with `400 BAD_CHALLENGE` by servers from this change on. A developer pass is for trying Unfret out: one per network
  a day, its free checks for the API and the MCP server.

## [0.1.0]

First release (not yet on PyPI).

* `Unfret(pass_=…).check(url, tier="deep", page_content=None)` → a `Decision`: `action` (`allow`, `warn` or
  `block`) from the verdict's band, with a configurable `policy`; the `band`, its `label`, the `flags` and Unfret's
  whole answer (`verdict`).
* `AsyncUnfret`, the same for asyncio.
* `checks_left()`, and `new_developer_pass()` (a pass with 20 free deep checks; solves the proof of work).
* Errors as exceptions: `InvalidPassError`, `NoChecksLeftError`, `RateLimitedError`, `PageUnreadableError`, all
  `UnfretError` (never charged).
