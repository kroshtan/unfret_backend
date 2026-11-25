"""
Review leaderboard nicknames for the public ``/live`` page: list them, approve or reject one.

Environment as :mod:`unfret.admin_cli` (``UNFRET_URL``, ``UNFRET_ADMIN_TOKEN`` or ``UNFRET_ADMIN_TOKEN_FILE``)::

    python -m unfret.nicknames_cli list [--review pending|approved|rejected|all]
    python -m unfret.nicknames_cli approve NICKNAME
    python -m unfret.nicknames_cli reject NICKNAME

Or ``make nicknames ARGS="approve some-name"``.
"""

import argparse
import sys
from urllib.parse import quote

import httpx

from unfret.admin_cli import admin_client, base_url


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m unfret.nicknames_cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="nicknames to review (pending by default)")
    listing.add_argument("--review", choices=["pending", "approved", "rejected", "all"], default="pending")
    for name in ("approve", "reject", "pending"):
        command = commands.add_parser(name, help=f"{name} a nickname")
        command.add_argument("nickname")
    return parser


def run(api: httpx.Client, argv: list[str]) -> int:
    """
    Run one command and print the outcome.

    :param api: The admin client.
    :param argv: The command line, without the program name.
    :return: The exit status: 1 when the request failed.
    """
    args = _parser().parse_args(argv)
    if args.command == "list":
        response = api.get("/v1/admin/nicknames", params={"review": args.review})
    else:
        response = api.post(f"/v1/admin/nicknames/{quote(args.nickname, safe='')}/{args.command}")
    if response.is_error:
        print(f"Failed ({response.status_code}): {response.text[:300]}", file=sys.stderr)  # noqa: T201
        return 1
    if args.command == "list":
        rows = response.json()
        print(f"{len(rows)} nickname(s), {args.review}, on {base_url()}")  # noqa: T201 - command-line tool
        for r in rows:
            opted = "on leaderboard" if r["public"] else "not opted in"
            print(f"  {r['handle']:<26}{r['review']:<10}{r['agreed']:>6} verified   {opted}")  # noqa: T201
    else:
        r = response.json()
        print(f"{r['handle']}: {r['review']}")  # noqa: T201
    return 0


def main() -> None:
    """Command-line entry point."""
    with admin_client() as api:
        sys.exit(run(api, sys.argv[1:]))


if __name__ == "__main__":
    main()
