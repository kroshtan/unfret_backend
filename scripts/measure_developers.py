"""
Measure what /developers states: the median time of deep and quick checks, and a full sample response.

    uv run scripts/measure_developers.py --server https://dev.unfret.net [--runs 5] [--pass ufk_…]

Checks the test shop on that server with an Unfret pass (without --pass / UNFRET_PASS a developer pass is made: one a
day per network), each run with its own ``?v=`` so every check is a fresh analysis rather than a cached answer, timed
from request to answer. Each deep run uses one of the pass's checks; quick runs are free. Rewrites
``src/unfret/static/developers-measured.json``; review the diff and commit it.
"""

import argparse
import hashlib
import itertools
import json
import os
import secrets
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

OUT = Path(__file__).parents[1] / "src" / "unfret" / "static" / "developers-measured.json"


def _developer_pass(http: httpx.Client, server: str) -> str:
    work = http.get(f"{server}/v1/keys/challenge").raise_for_status().json()
    for n in itertools.count():
        digest = hashlib.sha256(f"{work['challenge']}:{n}".encode()).digest()
        if int.from_bytes(digest, "big") >> (256 - work["bits"]) == 0:
            break
    body = {"challenge": work["challenge"], "solution": str(n), "developer": True}
    issued = http.post(f"{server}/v1/keys", json=body).raise_for_status().json()
    print(f"developer pass {issued['key_id']} with {issued['free_checks']} free deep checks")
    return str(issued["key"])


def _timed(http: httpx.Client, server: str, unfret_pass: str, tier: str) -> tuple[float, dict[str, Any]]:
    url = f"{server}/test-shop?v={secrets.token_hex(4)}"
    started = time.monotonic()
    response = http.post(
        f"{server}/v1/check",
        json={"url": url, "tier": tier},
        headers={"Authorization": f"Bearer {unfret_pass}"},
    )
    elapsed = time.monotonic() - started
    response.raise_for_status()
    verdict: dict[str, Any] = response.json()
    if verdict.get("cached"):
        raise RuntimeError("a cached answer: not a fresh check")
    return elapsed, verdict


def _summary(times: list[float]) -> dict[str, float | int]:
    return {
        "runs": len(times),
        "median_s": round(statistics.median(times), 2),
        "min_s": round(min(times), 2),
        "max_s": round(max(times), 2),
        "all_s": [round(t, 2) for t in times],
    }


def main() -> None:
    """Measure and write the file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default="https://dev.unfret.net")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--pass", dest="unfret_pass", default=os.environ.get("UNFRET_PASS"))
    args = parser.parse_args()
    server = args.server.rstrip("/")
    with httpx.Client(timeout=180) as http:
        unfret_pass = args.unfret_pass or _developer_pass(http, server)
        deep, quick, sample = [], [], None
        for _ in range(args.runs):
            elapsed, verdict = _timed(http, server, unfret_pass, "deep")
            deep.append(elapsed)
            sample = sample or verdict
            print(f"deep  {elapsed:5.1f} s  {verdict['action']} {verdict.get('band')}")
        for _ in range(args.runs):
            elapsed, verdict = _timed(http, server, unfret_pass, "quick")
            quick.append(elapsed)
            print(f"quick {elapsed:5.1f} s  {verdict['action']} {verdict.get('band')}")
    measured = {
        "server": urlsplit(server).netloc,
        "measured_on": datetime.now(UTC).date().isoformat(),
        "deep": _summary(deep),
        "quick": _summary(quick),
        "sample": sample,
    }
    OUT.write_text(json.dumps(measured, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
