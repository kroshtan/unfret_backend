"""
Manage affiliates and bonus codes from the command line, against the running service's admin API.

Affiliates (``make affiliates ARGS="…"``)::

    list                                       every affiliate, their codes, passes and what they are owed
    create CODE --name NAME [--note TEXT]      make an affiliate; prints their link and stats link (shown once)
    off CODE / on CODE                         switch an affiliate off (no new passes or commission) or back on
    note CODE TEXT                             change how they are paid (IBAN, PayPal, wallet)
    stats-link CODE                            a new stats link (the old one stops working)
    revoke CODE                                switch their stats link off
    report [--month YYYY-MM]                   per affiliate and month: passes, first checks, purchases, money
    paid CODE [--note TEXT]                    record a payout of what is due now (see report)
    reverse EARNING_ID                         take a commission back in full (a purchase refunded by hand)
    delete CODE                                remove an affiliate made by mistake (only while nothing was earned)

Bonus codes (``make bonus-codes ARGS="…"``)::

    list                                       every bonus code and its uses
    create CODE --bonus PCT [--expires YYYY-MM-DD] [--uses N] [--affiliate CODE]
    off CODE / on CODE                         switch a code off or back on

Environment as for ``make reports``: ``UNFRET_URL`` (default https://unfret.net) and ``UNFRET_ADMIN_TOKEN`` or
``UNFRET_ADMIN_TOKEN_FILE``. Amounts are shown in the currency's units (USD and EUR apart: no conversion).
"""

import argparse
import sys
from typing import Any

import httpx

from unfret.admin_cli import admin_client


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m unfret.credits.affiliates_cli")
    kinds = parser.add_subparsers(dest="kind", required=True)
    aff = kinds.add_parser("affiliate").add_subparsers(dest="command", required=True)
    aff.add_parser("list")
    create = aff.add_parser("create")
    create.add_argument("code")
    create.add_argument("--name", required=True)
    create.add_argument("--note", default="")
    for name in ("off", "on", "stats-link", "revoke", "delete"):
        aff.add_parser(name).add_argument("code")
    note = aff.add_parser("note")
    note.add_argument("code")
    note.add_argument("text")
    report = aff.add_parser("report")
    report.add_argument("--month")
    paid = aff.add_parser("paid")
    paid.add_argument("code")
    paid.add_argument("--note", default="")
    aff.add_parser("reverse").add_argument("earning_id", type=int)
    disc = kinds.add_parser("bonus").add_subparsers(dest="command", required=True)
    disc.add_parser("list")
    dcreate = disc.add_parser("create")
    dcreate.add_argument("code")
    dcreate.add_argument("--bonus", type=int, required=True, help="percent more checks with a pack")
    dcreate.add_argument("--expires", help="the last day it works, YYYY-MM-DD")
    dcreate.add_argument("--uses", type=int)
    dcreate.add_argument("--affiliate", help="count sales with this code for the affiliate with this code")
    for name in ("off", "on"):
        disc.add_parser(name).add_argument("code")
    return parser


def money(minor: int) -> str:
    """
    Cents as an amount.

    :param minor: Cents (may be negative).
    :return: E.g. ``12.34``.
    """
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"


def _balance(balance: dict[str, dict[str, int]]) -> str:
    parts = [
        f"{c.upper()}: due {money(b['due'])}, carried over {money(b['carried_over'])}, held {money(b['held'])}"
        for c, b in sorted(balance.items())
    ]
    return "; ".join(parts) or "nothing yet"


def describe_affiliates(body: dict[str, Any]) -> str:
    """
    The ``list`` answer as text.

    :param body: The admin API's answer.
    :return: The text.
    """
    lines = []
    for a in body["affiliates"]:
        state = "on" if a["active"] else "off"
        stats = "stats link on" if a["stats_link"] else "no stats link"
        lines.append(f"{', '.join(a['codes']):<24} {a['name']:<28} {state:<3} {a['passes']:>5} passes  {stats}")
        lines.append(f"{'':<24} {_balance(a['balance'])}")
        lines.append(f"{'':<24} payout: {a['payout_note'] or '(no note)'}")
    return "\n".join(lines) or "(no affiliates yet)"


def describe_report(body: dict[str, Any]) -> str:
    """
    The ``report`` answer as text.

    :param body: The admin API's answer.
    :return: The text.
    """
    names = {k: f"{', '.join(v['codes'])} ({v['name']})" for k, v in body["affiliates"].items()}
    lines = [f"{'month':<8} {'affiliate':<36} {'passes':>6} {'1st chk':>7} {'buys':>5}  earned, reversed, paid"]
    for row in body["months"]:
        moneys = "; ".join(
            f"{c.upper()} net {money(m['net'])} earned {money(m['commission'])} reversed {money(m['reversed'])} "
            f"paid {money(m['paid'])}"
            for c, m in sorted(row["money"].items())
        )
        lines.append(
            f"{row['month']:<8} {names.get(str(row['affiliate_id']), '?'):<36} {row['passes']:>6} "
            f"{row['first_checks']:>7} {row['purchases']:>5}  {moneys}"
        )
    rules = body.get("rules", {})
    lines += [
        "",
        (
            f"Balances now (commission counts {rules.get('hold_days', 30)} days after the purchase; a payout needs "
            f"{money(rules.get('min_payout_cents', 2500))} per currency, except for an affiliate switched off; "
            "refunds after a payout are carried over as a negative balance):"
        ),
    ]
    for k, v in body["affiliates"].items():
        off = "" if v.get("active", True) else " [switched off: everything payable is due]"
        lines.append(f"  {names[k]}{off}: {_balance(v['balance'])}")
    return "\n".join(lines)


def describe_codes(body: dict[str, Any]) -> str:
    """
    The bonus codes' ``list`` answer as text.

    :param body: The admin API's answer.
    :return: The text.
    """
    lines = []
    for c in body["codes"]:
        gives = f"+{c['bonus_pct']}% checks"
        uses = f"{c['uses']} / {c['max_uses']}" if c["max_uses"] else f"{c['uses']} / -"
        lines.append(
            f"{c['code']:<24} {gives:<14} used {uses:<12} until {c['expires_on'] or '-':<10} "
            f"{'on' if c['active'] else 'off':<3} {c['affiliate'] or ''}"
        )
    return "\n".join(lines) or "(no bonus codes yet)"


def _send(api: httpx.Client, args: argparse.Namespace) -> httpx.Response:  # noqa: C901, PLR0911, PLR0912 - one per command
    if args.kind == "bonus":
        match args.command:
            case "list":
                return api.get("/v1/admin/bonus-codes")
            case "create":
                body = {
                    "code": args.code,
                    "bonus_pct": args.bonus,
                    "expires_on": args.expires,
                    "max_uses": args.uses,
                    "affiliate": args.affiliate,
                }
                return api.post("/v1/admin/bonus-codes", json=body)
            case _:  # off / on
                return api.post(f"/v1/admin/bonus-codes/{args.code.upper()}", json={"active": args.command == "on"})
    match args.command:
        case "list":
            return api.get("/v1/admin/affiliates")
        case "create":
            return api.post(
                "/v1/admin/affiliates", json={"code": args.code, "name": args.name, "payout_note": args.note}
            )
        case "off" | "on":
            return api.post(f"/v1/admin/affiliates/{args.code.upper()}", json={"active": args.command == "on"})
        case "delete":
            return api.delete(f"/v1/admin/affiliates/{args.code.upper()}")
        case "note":
            return api.post(f"/v1/admin/affiliates/{args.code.upper()}", json={"payout_note": args.text})
        case "stats-link" | "revoke":
            stats = "rotate" if args.command == "stats-link" else "revoke"
            return api.post(f"/v1/admin/affiliates/{args.code.upper()}", json={"stats": stats})
        case "report":
            return api.get("/v1/admin/affiliates/report", params={"month": args.month} if args.month else None)
        case "paid":
            return api.post(f"/v1/admin/affiliates/{args.code.upper()}/paid", json={"note": args.note})
        case _:  # reverse
            return api.post(f"/v1/admin/affiliates/earnings/{args.earning_id}/reverse")


def _print(args: argparse.Namespace, body: dict[str, Any]) -> None:
    out: str
    if args.command == "list":
        out = describe_codes(body) if args.kind == "bonus" else describe_affiliates(body)
    elif args.command == "report":
        out = describe_report(body)
    elif args.kind == "bonus" and args.command == "create":
        out = f"{body['code']}: {body['label']}"
    elif args.command == "paid":
        paid = ", ".join(f"{money(v)} {c.upper()}" for c, v in body["paid"].items())
        out = f"{body['code']}: recorded a payout of {paid}" if paid else f"{body['code']}: nothing is due now"
    elif "links" in body:
        links = body["links"]
        out = f"{body['code']}\nLink:  {links['ref']}" + (
            f"\nStats: {links['stats']}  (shown once: send it to them)" if "stats" in links else "\nStats link off."
        )
    else:
        out = f"{body.get('code', args.code.upper())}: done"
    print(out)  # noqa: T201 - command-line tool


def run(api: httpx.Client, argv: list[str]) -> int:
    """
    Run one command and print the outcome.

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
    _print(args, response.json())
    return 0


def main() -> None:
    """Command-line entry point."""
    with admin_client() as api:
        sys.exit(run(api, sys.argv[1:]))


if __name__ == "__main__":
    main()
