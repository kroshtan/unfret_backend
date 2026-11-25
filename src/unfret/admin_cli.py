"""
What the operator's command-line tools share: a client for the running service's admin API; and one command.

Environment: ``UNFRET_URL`` (default https://unfret.net) and ``UNFRET_ADMIN_TOKEN``, or ``UNFRET_ADMIN_TOKEN_FILE``
(default ``~/kroshtan_secrets/unfret-prod-admin-token``).

Commands::

    free-pool [add EUR [EUR ...] --note TEXT]

Show the free pool (:mod:`unfret.spend`), or add a donation to it by hand: the amount in euros and a note for the
ledger, never who gave; several amounts are several donations, one ledger entry each. Usage:
``make free-pool ARGS="add 25 10 5 --note 'Revolut, 25 Nov'"``.

    retire-passes --before ISO [--keep KEY_ID]... [--keep-from FILE]... [--confirm]
                  [--include-wallet-balances] [--include-purchases]

Retire every pass made before a moment, and every wallet account (:mod:`unfret.credits.retire`: what goes, what
stays, and what a time of day on the cut-off day means). Without ``--confirm`` it is a dry run that changes nothing.
Either way it prints counts only, never a key, a key id or a wallet. ``--keep`` names a pass to keep by its public key
id; ``--keep-from`` reads the ``key_id`` field of a JSON file (the store reviewers' pass), never printing the rest.
Usage: ``make retire-passes ARGS="--before 2026-11-20T09:30Z --keep-from reviewer.json"``.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from unfret.credits import keys

DEFAULT_URL = "https://unfret.net"


def admin_token() -> str:
    """
    The admin token: ``UNFRET_ADMIN_TOKEN``, else the file ``UNFRET_ADMIN_TOKEN_FILE`` names.

    :return: The token.
    """
    if token := os.getenv("UNFRET_ADMIN_TOKEN"):
        return token
    path = Path(os.getenv("UNFRET_ADMIN_TOKEN_FILE", "~/kroshtan_secrets/unfret-prod-admin-token")).expanduser()
    return path.read_text().strip()


def base_url() -> str:
    """
    The service the tools talk to.

    :return: ``UNFRET_URL`` or production, without a trailing slash.
    """
    return os.getenv("UNFRET_URL", DEFAULT_URL).rstrip("/")


def admin_client() -> httpx.Client:
    """
    A client for the service's admin API.

    :return: The client (close it, or use it as a context manager).
    """
    return httpx.Client(base_url=base_url(), headers={"Authorization": f"Bearer {admin_token()}"}, timeout=60)


# ── retire-passes ────────────────────────────────────────────────────────────────────────────────────────────────


def _moment(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("not an ISO date or time (e.g. 2026-11-20T09:30Z)") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _key_id(value: str) -> str:
    try:
        return keys.key_id_from(value)
    except keys.ApiKeyError as exc:
        raise argparse.ArgumentTypeError("not a key id") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m unfret.admin_cli", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    pool = commands.add_parser("free-pool", help="show the free pool, or add a donation to it")
    pool.add_argument("action", nargs="?", choices=["add"], help="add a donation")
    pool.add_argument("amount", nargs="*", help="the amount(s) in euros, one per donation, e.g. 25 or 5 10 12.50")
    pool.add_argument("--note", default="", help="a short note for the ledger (never the donor)")
    retire = commands.add_parser("retire-passes", help="retire every pass made before a moment (dry run by default)")
    retire.add_argument("--before", type=_moment, required=True, help="ISO date or time, UTC unless it says otherwise")
    retire.add_argument("--keep", type=_key_id, action="append", default=[], help="a pass's key id to keep")
    retire.add_argument("--keep-from", type=Path, action="append", default=[], help="a JSON file with a key_id")
    retire.add_argument("--confirm", action="store_true", help="retire them (otherwise: a dry run, counts only)")
    retire.add_argument("--include-wallet-balances", action="store_true", help="also wallets with a balance")
    retire.add_argument("--include-purchases", action="store_true", help="also accounts that bought checks")
    return parser


def keep_ids(args: argparse.Namespace) -> list[str]:
    """
    The key ids to keep: ``--keep`` and the ``key_id`` of every ``--keep-from`` file.

    :param args: The parsed command line.
    :return: The ids.
    :raises SystemExit: If a file has no valid ``key_id`` (without saying what it has).
    """
    ids = list(args.keep)
    for path in args.keep_from:
        try:
            ids.append(_key_id(str(json.loads(path.expanduser().read_text())["key_id"])))
        except OSError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError:
            raise SystemExit(f"{path}: no readable key_id in it.") from None
    return ids


def describe(r: dict[str, Any], target: str) -> str:
    """
    A retire-passes answer as text: counts only.

    :param r: The admin API's answer.
    :param target: The service it ran on.
    :return: The text.
    """
    day = r["cutoff_day"]
    mode = (
        "RETIRED" if r["retired"] else "dry run, nothing changed" if not r["confirm"] else "REFUSED, nothing changed"
    )
    made = f"made before {day}" + (f", or on {day} without free checks" if r["same_day"] else "")

    def row(label: str, value: object, note: str = "") -> str:
        return f"{label:<44}{value:>6}" + (f"   {note}" if note else "")

    lines = [
        f"Retire passes on {target}: {made} ({mode})",
        "",
        row("passes to retire", r["passes"]),
        row("  made on an earlier day", r["passes_earlier_days"]),
        row(f"  made on {day}", r["passes_same_day"]) if r["same_day"] else None,
        row("  with a balance", r["with_balance"], f"({r['balance_checks']} checks)"),
        row("  free checks left on them", r["free_checks"]),
        row("  card checkouts", r["card_checkouts"]),
        row("  crypto quotes", r["crypto_quotes"]),
        row("  open reports", r["open_reports"], "(deleted with them: label them first to keep them)"),
        row("wallet accounts (crypto)", r["wallets"]),
        row("  with a non-zero balance", r["wallets_with_balance"], f"({r['wallet_balance_checks']} checks)"),
        row("accounts that bought checks", r["purchases"]),
        row("report limit entries", r["report_limits"]),
        row("rows of passes already gone", r["orphan_rows"]),
        "",
        row(
            "kept: named with --keep", r["kept_named"], f"(of {r['keep_named']} named; not found: {r['keep_unknown']})"
        ),
        row(f"kept: made on {day} with free checks", r["kept_same_day_free"]) if r["same_day"] else None,
        row("untouched: made later", r["passes_later"]),
        "Wallet sign-ins are signed tokens, not stored: a retired wallet signs in again, with nothing on it.",
    ]
    text = [line for line in lines if line is not None]
    if r["refused"]:
        text += ["", "Refused:", *(f"  - {reason}" for reason in r["refused"])]
    elif not r["confirm"]:
        text += ["", "Run again with --confirm to retire them."]
    return "\n".join(text)


def free_pool(api: httpx.Client, args: argparse.Namespace) -> int:
    """
    Show the free pool, or add a donation to it, and print the pool.

    :param api: The admin client.
    :param args: The parsed command line.
    :return: The exit status: 1 when the service refused (or the request failed).
    """
    if args.action == "add":
        if not args.amount:
            print("Say how much: free-pool add 25 [10 5 ...] --note '...'", file=sys.stderr)  # noqa: T201
            return 1
        body = {"amounts_eur": list(args.amount), "note": args.note}  # one ledger entry per donation
        response = api.post("/v1/admin/free-pool/donations", json=body)
    else:
        response = api.get("/v1/admin/free-pool")
    if response.is_error:
        print(f"Failed ({response.status_code}): {response.text[:300]}", file=sys.stderr)  # noqa: T201
        return 1
    pool = response.json()
    print(  # noqa: T201 - command-line tool
        f"Pool €{pool['pool_eur']} (base €{pool['base_eur']}, donated €{pool['donated_eur']}); today €"
        f"{pool['spent_today_eur']} of €{pool['day_limit_eur']}; a free deep check ≈ €{pool['average_check_eur']}; "
        f"{pool['checks_left_today']} left today{' (paused)' if pool['paused'] else ''}"
    )
    for donation in pool["donations"][:10]:
        print(f"  #{donation['id']} {donation['day']} €{donation['eur']} {donation['note']}")  # noqa: T201
    return 0


def run(api: httpx.Client, argv: list[str], target: str = "") -> int:
    """
    Run one command and print the outcome.

    :param api: The admin client.
    :param argv: The command line, without the program name.
    :param target: The service, for the text.
    :return: The exit status: 1 when the service refused (or the request failed).
    """
    args = _parser().parse_args(argv)
    if args.command == "free-pool":
        return free_pool(api, args)
    body = {
        "before": args.before.isoformat(),
        "keep": keep_ids(args),
        "confirm": args.confirm,
        "include_wallet_balances": args.include_wallet_balances,
        "include_purchases": args.include_purchases,
    }
    response = api.post("/v1/admin/passes/retire", json=body)
    if response.is_error:
        detail = response.json().get("detail") if "json" in response.headers.get("content-type", "") else None
        print(f"Failed ({response.status_code}): {detail or response.text[:300]}", file=sys.stderr)  # noqa: T201
        return 1
    report = response.json()
    print(describe(report, target or base_url()))  # noqa: T201 - command-line tool
    return 1 if report["refused"] else 0


def main() -> None:
    """Command-line entry point."""
    with admin_client() as api:
        sys.exit(run(api, sys.argv[1:]))


if __name__ == "__main__":
    main()
