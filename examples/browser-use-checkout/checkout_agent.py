"""
Buy only where Unfret says so: a browser-use agent that checks the shop with Unfret before it goes to checkout.

Two guards, both with the Python client (``unfret-client``):

1. **In code, before the agent starts** (the hard gate): the shop is checked, and the agent is never handed a shop
   Unfret says ``block`` for; ``warn`` asks you first. A model can't talk its way past this one.
2. **As a tool the agent calls** (``unfret_check``), for pages it finds on its way: it is told to call it before any
   checkout and to stop on ``block``.

Run it against Unfret's test shop, a made-up store with every trick Unfret catches (so Unfret should block it)::

    uv run checkout_agent.py --gate-only            # the hard gate alone: no browser, no LLM
    uv run --extra agent checkout_agent.py          # the gate, then the agent if the shop passes
    uv run --extra agent checkout_agent.py --agent-decides   # skip the gate: the agent's own check must stop it

Settings: ``UNFRET_PASS`` (an Unfret pass; without one a developer pass with 20 free deep checks is made, one a day
per network), ``UNFRET_API_URL`` (default ``https://dev.unfret.net``, the test server), ``UNFRET_EXAMPLE_LLM``
(the agent's model as a Pydantic AI model string, e.g. ``anthropic:claude-…`` with ``ANTHROPIC_API_KEY``; default:
browser-use's own, with ``BROWSER_USE_API_KEY``).
"""

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit

from unfret_client import AsyncUnfret, Decision, NoChecksLeftError, Tier

BASE_URL = os.environ.get("UNFRET_API_URL", "https://dev.unfret.net").rstrip("/")
TEST_SHOP = f"{BASE_URL}/test-shop"

TASK = """Go to {url} and buy the product shown there, up to the checkout page, but never pay and never type any
personal or payment details. Before you open the checkout, call unfret_check with the product page's address.
If it answers block, stop right there and report why. If it answers warn, stop and report that a human must decide.
Only on allow may you open the checkout page. Finish by saying how far you got and what Unfret said."""


def show(decision: Decision, url: str) -> None:
    """
    Print a decision.

    :param decision: Unfret's decision.
    :param url: The checked page.
    """
    print(f"Unfret on {url}: {decision.action.upper()} ({decision.label or 'not assessed'})")
    for reason in decision.reasons[:6]:
        print(f"  - {reason}")
    left = decision.verdict.get("free_checks_left")
    print(f"  (charged: {decision.verdict.get('charged')}, free checks left: {left})")


async def gate(unfret: AsyncUnfret, url: str, tier: Tier) -> Decision:
    """
    The hard gate: Unfret's decision on the shop, before any agent sees it.

    :param unfret: The client.
    :param url: The shop or product page.
    :param tier: ``deep`` (one check) or ``quick`` (free).
    :return: The decision.
    """
    try:
        decision = await unfret.check(url, tier=tier)
    except NoChecksLeftError:
        print("The pass has no deep checks left; using a quick check (free) instead.")
        decision = await unfret.check(url, tier="quick")
    show(decision, url)
    return decision


def human_agrees(question: str) -> bool:
    """
    Ask the human (``warn``): no answer, as in CI, is a no.

    :param question: The question.
    :return: Whether they said yes.
    """
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


async def run_agent(unfret: AsyncUnfret, url: str) -> None:
    """
    The browser-use agent, with Unfret as a tool it must call before checkout.

    :param unfret: The client (its pass pays for the agent's deep checks).
    :param url: The shop to buy from.
    """
    from browser_use import Agent, Browser, Tools  # noqa: PLC0415 - only this mode needs browser-use (--extra agent)

    tools = Tools()

    @tools.action(
        "Check a shop or product page with Unfret before buying from it. Answers allow, warn or block with the "
        "reasons. Call it with the product page's address before opening any checkout."
    )
    async def unfret_check(url: str) -> str:
        decision = await unfret.check(url)
        show(decision, url)
        reasons = "; ".join(decision.reasons[:5])
        return f"{decision.action}: {decision.label}. {reasons}"

    agent = Agent(
        TASK.format(url=url),
        llm=os.environ.get("UNFRET_EXAMPLE_LLM") or None,
        # A local headless Chromium (browser-use's default is its paid cloud browser), kept on the shop's site.
        browser=Browser("chromium", headless=True, allowed_domains=[urlsplit(url).netloc]),
        tools=tools,
        extend_system_message="Never go to a checkout that Unfret said block (or warn) for.",
    )
    history = await agent.run(max_steps=25)
    print("Agent:", history.final_result())


async def main() -> int:
    """
    Gate the shop, then (unless told otherwise) let the agent shop.

    :return: The exit status: 0, or 1 when ``--expect`` didn't match.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=TEST_SHOP, help="the shop to buy from (default: Unfret's test shop)")
    parser.add_argument("--tier", choices=["deep", "quick"], default="deep")
    parser.add_argument("--gate-only", action="store_true", help="only the hard gate: no browser, no LLM")
    parser.add_argument("--agent-decides", action="store_true", help="skip the gate: the agent's own check decides")
    parser.add_argument("--expect", choices=["allow", "warn", "block"], help="exit 1 unless the gate decides this")
    args = parser.parse_args()

    async with AsyncUnfret(base_url=BASE_URL) as unfret:
        if not unfret.unfret_pass:
            issued = await unfret.new_developer_pass()
            print(f"Made a developer pass ({issued['free_checks']} free deep checks). Keep it for next time:")
            print(f"  export UNFRET_PASS={issued['key']}")
        if not args.agent_decides:
            decision = await gate(unfret, args.url, args.tier)
            if args.expect and decision.action != args.expect:
                print(f"Expected {args.expect}, got {decision.action}.")
                return 1
            if args.gate_only:
                return 0
            if decision.action == "block":
                print("Not handing this shop to the agent: Unfret says block.")
                return 0
            if decision.action == "warn" and not human_agrees("Unfret says: worth a look. Let the agent buy anyway?"):
                print("Stopped: a human didn't approve.")
                return 0
        await run_agent(unfret, args.url)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
