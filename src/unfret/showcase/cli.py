"""
Review the "Spotted recently" showcase from the command line, against the running service's admin API.

The analysis runs on the server (``POST /v1/admin/showcase``): it fetches the page and runs the full check there,
so this needs only the admin token. Nothing is public until ``publish``.

Commands::

    add URL [--title TEXT]      check a public product page and keep it as a draft
    list [--status STATUS]      the cases, newest first
    show ID                     one case, with what the public would see
    publish ID                  put a draft on the site
    remove ID --reason TEXT     take a case off the site for good
    screenshot ID FILE          attach a PNG, JPEG or WebP screenshot (at most 800 kB)
    title ID TEXT               change the title shown

Usage: ``make showcase ARGS="add https://…"`` (or ``uv run python -m unfret.showcase …``). Environment:
``UNFRET_URL`` (default https://unfret.kroshtan.com) and ``UNFRET_ADMIN_TOKEN``, or ``UNFRET_ADMIN_TOKEN_FILE``
(default ``~/kroshtan_secrets/unfret-prod-admin-token``), as for ``make reports``.
"""

import argparse
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx

DEFAULT_URL = "https://unfret.kroshtan.com"
# A full analysis may take up to the service's analysis timeout (90 s by default), plus the page fetch.
ADD_TIMEOUT_S = 180.0


def admin_token() -> str:
    """
    The admin token: ``UNFRET_ADMIN_TOKEN``, else the file ``UNFRET_ADMIN_TOKEN_FILE`` names.

    :return: The token.
    """
    if token := os.getenv("UNFRET_ADMIN_TOKEN"):
        return token
    path = Path(os.getenv("UNFRET_ADMIN_TOKEN_FILE", "~/kroshtan_secrets/unfret-prod-admin-token")).expanduser()
    return path.read_text().strip()


def admin_client() -> httpx.Client:
    """
    A client for the service's admin API.

    :return: The client (close it, or use it as a context manager).
    """
    base = os.getenv("UNFRET_URL", DEFAULT_URL).rstrip("/")
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {admin_token()}"}, timeout=30)


def promote(api: httpx.Client, url: str) -> httpx.Response:
    """
    Have the server check a reported page again itself and keep that check as a draft case.

    Only the page's address is sent: nothing from the report (its result, its text, its reporter) goes into a case.

    :param api: The admin client.
    :param url: The reported page's address.
    :return: The service's answer.
    """
    return api.post("/v1/admin/showcase", json={"url": url, "origin": "report"}, timeout=ADD_TIMEOUT_S)


def describe(case: dict[str, Any]) -> str:
    """
    A case as a screen of text.

    :param case: The case from the admin API.
    :return: The text.
    """
    public = case["public"]
    result = public["result"]
    lines = [
        f"#{case['id']}  [{case['status']}]  {case['host']}  ·  from {case['origin']}",
        f"title:   {case['title']}",
        f"url:     {case['product_url']}",
        f"checked: {case['checked_at']}  ·  score {case['response'].get('score')}",
    ]
    if case["published_at"]:
        lines.append(f"published: {case['published_at']}  ·  page {public['page_url']}")
    if case["removed_at"]:
        lines.append(f"removed: {case['removed_at']}  ·  {case['removed_reason']}")
    shot = case["screenshot"]
    lines.append(f"screenshot: {shot['type']}, {shot['bytes']} bytes" if shot else "screenshot: none")
    lines += ["", f"{public['symbol']}  {public['label']}", textwrap.fill(public["headline"], 110)]
    for heading, key in (("What we noticed", "reasons"), ("What looks good", "positives")):
        if items := result["verdict"][key]:
            lines.append(f"\n{heading}:")
            lines += [f"  - {item['title']}" + (f": {item['detail']}" if item["detail"] else "") for item in items]
    if case["left_out"]:
        lines.append("\nLeft out of the public page (a forbidden word): " + "; ".join(case["left_out"]))
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m unfret.showcase", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="check a public product page and keep it as a draft")
    add.add_argument("url")
    add.add_argument("--title", help="the title to show (default: the page's product title)")
    listing = commands.add_parser("list", help="the cases, newest first")
    listing.add_argument("--status", choices=["draft", "published", "removed"])
    for name, text in (("show", "one case"), ("publish", "put a draft on the site")):
        commands.add_parser(name, help=text).add_argument("id", type=int)
    remove = commands.add_parser("remove", help="take a case off the site for good")
    remove.add_argument("id", type=int)
    remove.add_argument("--reason", required=True)
    shot = commands.add_parser("screenshot", help="attach a screenshot")
    shot.add_argument("id", type=int)
    shot.add_argument("file", type=Path)
    title = commands.add_parser("title", help="change the title shown")
    title.add_argument("id", type=int)
    title.add_argument("text")
    return parser


def _send(api: httpx.Client, args: argparse.Namespace) -> httpx.Response:  # noqa: PLR0911 - one per command
    match args.command:
        case "add":
            body = {"url": args.url, "origin": "operator"} | ({"title": args.title} if args.title else {})
            return api.post("/v1/admin/showcase", json=body, timeout=ADD_TIMEOUT_S)
        case "list":
            return api.get("/v1/admin/showcase", params={"status": args.status} if args.status else None)
        case "show":
            return api.get(f"/v1/admin/showcase/{args.id}")
        case "publish":
            return api.post(f"/v1/admin/showcase/{args.id}/publish")
        case "remove":
            return api.post(f"/v1/admin/showcase/{args.id}/remove", json={"reason": args.reason})
        case "title":
            return api.post(f"/v1/admin/showcase/{args.id}/title", json={"title": args.text})
        case _:  # screenshot
            data = args.file.read_bytes()
            return api.put(f"/v1/admin/showcase/{args.id}/screenshot", content=data)


def run(api: httpx.Client, argv: list[str]) -> int:
    """
    Run one command against the admin API and print the outcome.

    :param api: The admin client.
    :param argv: The command line, without the program name.
    :return: The exit status.
    """
    args = _parser().parse_args(argv)
    response = _send(api, args)
    if response.is_error:
        detail = response.json().get("detail") if "json" in response.headers.get("content-type", "") else None
        print(f"Failed ({response.status_code}): {detail or response.text[:300]}", file=sys.stderr)  # noqa: T201
        return 1
    body = response.json()
    if args.command == "list":
        for case in body["items"]:
            public = case["public"]
            print(f"#{case['id']:<5} {case['status']:<9} {public['symbol']} {case['host']:<28} {case['title'][:60]}")  # noqa: T201
        if not body["items"]:
            print("No cases.")  # noqa: T201
        return 0
    print(describe(body))  # noqa: T201 - command-line tool
    if args.command == "add":
        print(f'\nDraft #{body["id"]} added. Review it, then: make showcase ARGS="publish {body["id"]}"')  # noqa: T201
    return 0


def main() -> None:
    """Command-line entry point."""
    with admin_client() as api:
        sys.exit(run(api, sys.argv[1:]))
