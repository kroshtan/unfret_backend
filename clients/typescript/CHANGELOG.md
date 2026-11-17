# Changelog (unfret-client, TypeScript)

## [0.1.0]

First release (not yet on npm).

* `new Unfret({ pass }).check(url, { tier, pageContent })` → a `Decision`: `action` (`allow`, `warn` or `block`)
  from the verdict's band, with a configurable `policy`; the `band`, its `label`, the `reasons` and Unfret's whole
  answer (`verdict`).
* `checksLeft()`, and `newDeveloperPass()` (a pass with 20 free deep checks; solves the proof of work).
* Errors as classes: `InvalidPassError`, `NoChecksLeftError`, `RateLimitedError`, `PageUnreadableError`, all
  `UnfretError` (never charged).
* No dependencies: `fetch` and Web Crypto (Node 20+, Deno, Bun, browsers).
