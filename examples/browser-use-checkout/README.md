# Check before checkout: a browser-use agent with Unfret

A small [browser-use](https://github.com/browser-use/browser-use) agent that shops on a page, and asks
[Unfret](https://unfret.net) about the shop before it goes to checkout, with the Python client
([`clients/python`](../../clients/python), `unfret-client`). MIT licensed.

Two guards:

1. **In code, before the agent starts** (the hard gate): `unfret.check(shop)`; the agent is never handed a shop
   Unfret says `block` for, and `warn` asks you first. Nothing the model does can get past it.
2. **A tool the agent calls**, `unfret_check(url)`, for pages it finds on its way: it is told to call it before any
   checkout and to stop on `block` or `warn`.

It runs against Unfret's **test shop** by default (`https://dev.unfret.net/test-shop`), a made-up store with every
trick Unfret catches, so the expected outcome is that Unfret says `block` and nothing is bought. The agent never pays
or types personal details, and its browser stays on the shop's site.

## Run it

Needs [uv](https://docs.astral.sh/uv/). Without `UNFRET_PASS` the script makes a **developer pass** (20 free deep
checks, one a day per network) and prints it so you can reuse it.

```bash
cd examples/browser-use-checkout
uv run checkout_agent.py --gate-only              # the hard gate alone: no browser, no LLM
uv run checkout_agent.py --gate-only --expect block   # exit 1 unless Unfret blocks the test shop (CI)
```

With the agent (a local headless Chromium and an LLM):

```bash
uv run --extra agent checkout_agent.py                   # the gate, then the agent if the shop passes
uv run --extra agent checkout_agent.py --agent-decides   # no gate: the agent's own unfret_check must stop it
uv run --extra agent checkout_agent.py --url https://some.shop/product   # any other shop
```

| Setting | Default | |
| --- | --- | --- |
| `UNFRET_PASS` | a new developer pass | The Unfret pass that pays the deep checks |
| `UNFRET_API_URL` | `https://dev.unfret.net` | Unfret's server (`https://unfret.net` for real shopping) |
| `UNFRET_EXAMPLE_LLM` | browser-use's own model (`BROWSER_USE_API_KEY`) | The agent's model, as a Pydantic AI model string, e.g. `anthropic:<model>` with `ANTHROPIC_API_KEY`, or `openai:<model>` with `OPENAI_API_KEY` |

`--tier quick` uses the free quick check instead of a deep one; when the pass has no deep checks left, the gate falls
back to quick by itself. Built for browser-use 1.0.6 (`Browser("chromium", …)` is a local browser;
browser-use's default `Browser()` is its paid cloud browser).

## In CI

The workflow "Examples against dev" (`.forgejo/workflows/examples.yaml`, manual) runs the gate with
`--expect block` and the clients' live tests against dev, with a fresh developer pass. The agent itself needs an LLM
key and a browser, so it is a manual run.
