# unfret-client (TypeScript reference client)

A minimal TypeScript/JavaScript client for [Unfret](https://unfret.net): before an AI agent buys, ask whether the
store or product page shows signs of a scam, dropshipped generic goods, fake reviews or pressure tricks, and get back
what to do. No dependencies (it uses `fetch` and Web Crypto: Node 20 or newer, Deno, Bun, browsers). MIT licensed.

**It is not published on npm.** It is a reference to copy: put [`src/index.ts`](src/index.ts) into your project (one
file, with [`LICENSE`](LICENSE)) and import it from there. For any other language, or to write your own,
[unfret.net/developers/http](https://unfret.net/developers/http) has the request, the response, the errors and the
proof of work for a developer pass. (The Python client, `unfret-client` in [`../python`](../python), is the one
published package.)

```ts
import { Unfret } from "./unfret"; // src/index.ts, copied into your project as unfret.ts

const unfret = new Unfret({ pass: "ufk_…" }); // or set UNFRET_PASS
const decision = await unfret.check("https://shop.example/p/42");
decision.action;  // "allow", "warn" (ask your human first) or "block"
decision.label;   // "Looks fine", "Worth a look", "Think twice" or "Serious concerns"
decision.reasons; // the evidence, one line per flag
```

It needs an **Unfret pass** (`ufk_…`). A deep check (the default: everything, including an AI read of the listing,
its reviews and photos) uses one of the pass's checks; `{ tier: "quick" }` (rules on the page itself) is free. Get a
**developer pass with 20 free deep checks** at [unfret.net/developers](https://unfret.net/developers), or in code:

```ts
const issued = await new Unfret().newDeveloperPass(); // solves a proof of work (a few seconds); keep issued.key and issued.recovery_code
```

A developer pass is for trying Unfret out: one per network a day. Its free checks work on the API and the MCP server,
not in the browser extension or on the website; checks you buy for it work everywhere.

Buy more checks for a pass at [unfret.net/checkout](https://unfret.net/checkout). (Agents with a Solana wallet can
also pay per check over x402 without a pass; see [unfret.net/developers](https://unfret.net/developers).) Keep the
pass on a server: in a browser page anyone can read it.

## From verdict to action

| Verdict (`band`) | Default action |
| --- | --- |
| Looks fine (`reliable`) | `allow` |
| Worth a look (`concerns`) | `warn` |
| Think twice (`careful`) | `block` |
| Serious concerns (`junk_or_scam`) | `block` |

Change it with `policy`, e.g. `new Unfret({ policy: { careful: "warn" } })`. A page Unfret couldn't assess (not a
store or product page) is `warn` (`unassessed` to change). `decision.verdict` has Unfret's whole answer, including
`risk_score`, `confidence`, its own `action` and what the pass has left (`free_checks_left`, `balance_usd_micros`).

## Options and errors

`check(url, { tier: "deep" | "quick", pageContent })`: send `pageContent` (the page's HTML) for marketplaces such as
Amazon, which refuse server-side visits. `checksLeft()` returns what the pass has left. The server is
`https://unfret.net` (`baseUrl`, or `UNFRET_API_URL`; `https://dev.unfret.net` is the test server).

A refused call throws an `UnfretError` and is never charged: `InvalidPassError` (401), `NoChecksLeftError` (402: buy
checks, or use quick), `RateLimitedError` (429; `retryAfter` in seconds, also when free deep checks are paused until
00:00 UTC) and `PageUnreadableError` (send `pageContent`). Each has `status`, `code` and `detail`.

Source: [git.kroshtan.com/kroshtan/unfret_backend](https://git.kroshtan.com/kroshtan/unfret_backend/src/branch/main/clients/typescript)
(mirror on [GitHub](https://github.com/kroshtan/unfret_backend/tree/main/clients/typescript)).
