"""
Manage intro codes from the command line, against the running service's admin API (``/v1/admin/codes``).

An intro code is a named code for one channel (``FRIENDS``, ``DEV``, a newsletter), worth a few checks to a new
account, usable a set number of times (see :mod:`unfret.credits.invites`).

Commands::

    list                              every code, its uses, today's checks against the cap, buyers by how they came
    create CODE --uses N [--checks 5] make a code
    off CODE / on CODE                switch a code off (it answers "ended") or back on
    add CODE N                        give a code N more uses (negative takes them away)
    link CODE                         the link to share: it opens checkout with a new key and the code filled in

Usage: ``make codes ARGS="create FRIENDS --uses 40"``. Environment as for ``make showcase``: ``UNFRET_URL`` (default
https://unfret.net) and ``UNFRET_ADMIN_TOKEN`` or ``UNFRET_ADMIN_TOKEN_FILE``.
"""

import argparse
import os
import sys
from typing import Any

import httpx

from unfret.showcase.cli import DEFAULT_URL, admin_client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m unfret.credits.codes", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every code and its uses")
    create = commands.add_parser("create", help="make a code")
    create.add_argument("code")
    create.add_argument("--uses", type=int, required=True)
    create.add_argument("--checks", type=int, default=5)
    for name in ("off", "on", "link"):
        commands.add_parser(name).add_argument("code")
    add = commands.add_parser("add", help="give a code more uses")
    add.add_argument("code")
    add.add_argument("n", type=int)
    return parser


def link(code: str) -> str:
    """
    The link to share for a code.

    :param code: The code.
    :return: ``<site>/key?code=CODE``.
    """
    return f"{os.getenv('UNFRET_URL', DEFAULT_URL).rstrip('/')}/key?code={code.strip().upper()}"


def describe(body: dict[str, Any]) -> str:
    """
    The ``list`` answer as text.

    :param body: The admin API's answer.
    :return: The text.
    """
    lines = [f"{'code':<24} {'checks':>6} {'used':>10}  state"]
    for c in body["codes"]:
        state = "on" if c["active"] else "off"
        lines.append(f"{c['code']:<24} {c['checks']:>6} {c['uses']:>4} / {c['max_uses']:<4}  {state}")
    if not body["codes"]:
        lines.append("(no codes yet)")
    today, accounts = body["today"], body["accounts"]
    lines += [
        "",
        f"today: {today['checks_given']} of {today['cap']} free checks given (UTC day)",
        f"accounts via an invite or code: {accounts['via_code']}, of which bought: {accounts['via_code_bought']}",
        f"organic buyers: {accounts['organic_bought']}",
    ]
    return "\n".join(lines)


def _send(api: httpx.Client, args: argparse.Namespace) -> httpx.Response:
    match args.command:
        case "list":
            return api.get("/v1/admin/codes")
        case "create":
            return api.post("/v1/admin/codes", json={"code": args.code, "checks": args.checks, "max_uses": args.uses})
        case "off" | "on":
            return api.post(f"/v1/admin/codes/{args.code.upper()}", json={"active": args.command == "on"})
        case _:  # add
            return api.post(f"/v1/admin/codes/{args.code.upper()}", json={"add_uses": args.n})


def run(api: httpx.Client, argv: list[str]) -> int:
    """
    Run one command and print the outcome.

    :param api: The admin client.
    :param argv: The command line, without the program name.
    :return: The exit status.
    """
    args = _parser().parse_args(argv)
    if args.command == "link":
        print(link(args.code))  # noqa: T201 - command-line tool
        return 0
    response = _send(api, args)
    if response.is_error:
        detail = response.json().get("detail") if "json" in response.headers.get("content-type", "") else None
        print(f"Failed ({response.status_code}): {detail or response.text[:300]}", file=sys.stderr)  # noqa: T201
        return 1
    body = response.json()
    if args.command == "list":
        print(describe(body))  # noqa: T201
    else:
        state = "on" if body["active"] else "off"
        print(f"{body['code']}: {body['checks']} checks, {body['uses']} / {body['max_uses']} used, {state}")  # noqa: T201
        if args.command == "create":
            print(f"Share: {link(body['code'])}")  # noqa: T201
    return 0


def main() -> None:
    """Command-line entry point."""
    with admin_client() as api:
        sys.exit(run(api, sys.argv[1:]))


if __name__ == "__main__":
    main()
