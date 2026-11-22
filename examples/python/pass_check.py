"""
Check a page with an Unfret pass: ``POST /v1/check`` with ``Authorization: Bearer ufk_…``.

Without ``UNFRET_PASS`` it first makes a developer pass (20 free deep checks, one a day per network) by solving the
server's proof of work, and prints it so you can reuse it. Run: ``uv run pass_check.py``.
"""

import hashlib
import itertools
import json
import os
import sys

import requests

API = os.environ.get("UNFRET_API_URL") or (
    "https://unfret.net" if os.environ.get("UNFRET_ENV") == "production" else "https://dev.unfret.net"
)
PAGE = os.environ.get("UNFRET_CHECK_URL") or f"{API}/test-shop"
TIER = os.environ.get("UNFRET_TIER", "deep")  # deep uses one of the pass's checks; quick is free


def solve(challenge: str, bits: int) -> str:
    """
    Find a solution: SHA-256(challenge + ":" + solution) starts with ``bits`` zero bits.

    :param challenge: The challenge from ``GET /v1/keys/challenge``.
    :param bits: Its difficulty.
    :return: The solution, a decimal counter.
    """
    for n in itertools.count():
        digest = int.from_bytes(hashlib.sha256(f"{challenge}:{n}".encode()).digest())
        if digest >> (256 - bits) == 0:
            return str(n)
    raise AssertionError


def new_developer_pass() -> str:
    """
    Make a developer pass: get a challenge, solve it (a few seconds), and exchange it for the pass.

    :return: The pass (``ufk_…``).
    """
    work = requests.get(f"{API}/v1/keys/challenge", params={"developer": "true"}, timeout=30).json()
    body = {"challenge": work["challenge"], "solution": solve(work["challenge"], work["bits"]), "developer": True}
    response = requests.post(f"{API}/v1/keys", json=body, timeout=30)
    response.raise_for_status()
    issued = response.json()
    print(f"New developer pass ({issued['free_checks']} free checks). Keep it: export UNFRET_PASS={issued['key']}")
    print(f"Its recovery code, shown only once: {issued['recovery_code']}")
    return str(issued["key"])


def main() -> int:
    """
    Run one check and print the verdict.

    :return: The exit status.
    """
    unfret_pass = os.environ.get("UNFRET_PASS") or new_developer_pass()
    response = requests.post(
        f"{API}/v1/check",
        json={"url": PAGE, "tier": TIER},
        headers={"Authorization": f"Bearer {unfret_pass}"},
        timeout=100,  # a deep check answers within about a minute
    )
    verdict = response.json()
    if response.status_code != 200:
        print(f"HTTP {response.status_code} {verdict.get('error_code')}: {verdict.get('detail')}", file=sys.stderr)
        return 1
    print(f"{verdict['action'].upper()} ({verdict['band']}, risk {verdict['risk_score']:.2f}) for {PAGE}")
    for flag in verdict["flags"]:
        print(f"  - {flag['type']}: {flag['evidence']}")
    print(json.dumps({k: verdict.get(k) for k in ("charged", "free_check", "free_checks_left")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
