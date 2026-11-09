#!/usr/bin/env python3
"""
Refresh the vendored Cloudflare edge ranges (``src/veridex/credits/cloudflare_ips.json``).

The checkout trusts Cloudflare's ``CF-IPCountry`` header only when the request reached our edge (Caddy) from one of
these ranges; Caddy passes the peer it saw on as ``X-Edge-Peer``.

Usage:
    uv run scripts/update_cloudflare_ips.py           # rewrite the table (review the diff, then commit)
    uv run scripts/update_cloudflare_ips.py --check   # exit 1 if Cloudflare's list differs
"""

import argparse
import ipaddress
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

TABLE = Path(__file__).resolve().parent.parent / "src" / "veridex" / "credits" / "cloudflare_ips.json"
SOURCES = ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6")


def fetch() -> list[str]:
    """
    Cloudflare's published edge ranges.

    :return: CIDR ranges, IPv4 then IPv6.
    """
    ranges: list[str] = []
    for url in SOURCES:
        response = httpx.get(url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        ranges += [str(ipaddress.ip_network(line.strip())) for line in response.text.splitlines() if line.strip()]
    return ranges


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="only compare; exit 1 on a difference")
    args = parser.parse_args()
    ranges = fetch()
    if args.check:
        current = json.loads(TABLE.read_text())["ranges"]
        if ranges != current:
            print("Cloudflare ranges changed:", sorted(set(ranges) ^ set(current)))
            sys.exit(1)
        print(f"Cloudflare ranges unchanged ({len(ranges)})")
        return
    today = datetime.now(UTC).date().isoformat()
    TABLE.write_text(json.dumps({"source": " ".join(SOURCES), "checked_on": today, "ranges": ranges}, indent=2) + "\n")
    print(f"wrote {len(ranges)} ranges to {TABLE}")


if __name__ == "__main__":
    main()
