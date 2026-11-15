#!/usr/bin/env python3
"""
Refresh the vendored EU Safety Gate index (``src/unfret/safety_gate_index.json.gz``) from the Commission's open data.

Safety Gate (formerly RAPEX) is the EU's rapid alert system for dangerous non-food products. The Commission
publishes every alert in weekly reports, as XML, listed at :data:`LIST_URL` (data.europa.eu dataset
``rapex-rapid-alert-system-non-food``, distribution "Weekly Reports - XML format", licence CC0 1.0; publisher: the
Directorate-General for Justice and Consumers). The index keeps only what matching a product page needs: the alert's
number and case number, its publication date, brand, product name, model, barcodes (valid GTINs only), the kind of
product, the risk type and the risk level, for the alerts a page can be matched on (a barcode, or a brand with a model
number). No descriptions, pictures or company details.

Weekly reports never change once published, so a refresh fetches only the reports the index doesn't have yet.

Usage:
    uv run scripts/update_safety_gate.py           # add the new weekly reports (review the diff, then commit)
    uv run scripts/update_safety_gate.py --check   # exit 1 if the Commission published reports the index lacks
    uv run scripts/update_safety_gate.py --cache DIR   # keep the downloaded XML in DIR (re-runs read it from there)
"""

import argparse
import gzip
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "src" / "unfret" / "safety_gate_index.json.gz"
LIST_URL = "https://ec.europa.eu/safety-gate-alerts/api/download/weeklyReport/list/xml/en"
SOURCE = {
    "name": "EU Safety Gate weekly reports (XML)",
    "url": LIST_URL,
    "dataset": "https://data.europa.eu/data/datasets/rapex-rapid-alert-system-non-food",
    "licence": "CC0 1.0 Universal",
    "publisher": "European Commission, Directorate-General for Justice and Consumers",
}
_ALERT_ID_RE = re.compile(r"alertDetail/(-?\d+)")  # alerts from before 2011 have negative numbers
_DIGITS_RE = re.compile(r"\d{8,14}")
_HEADERS = {"User-Agent": "Unfret Safety Gate importer (+https://unfret.net)"}


def valid_gtin(digits: str) -> bool:
    """
    Whether a string is a GTIN-8/12/13/14 with a correct check digit (as :func:`unfret.price_store.valid_gtin`).

    :param digits: The candidate.
    :return: ``True`` for a valid GTIN.
    """
    if not digits.isdigit() or len(digits) not in {8, 12, 13, 14}:
        return False
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == int(digits[-1])


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return re.sub(r"\s+", " ", child.text or "").strip() if child is not None and child.text else ""


def _date(value: str) -> str:
    # "10-11-2026" or "10/11/2026" → "2026-11-10".
    m = re.match(r"(\d{2})[-/](\d{2})[-/](\d{4})", value)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


def reports(list_xml: bytes) -> list[tuple[str, str]]:
    """
    The weekly reports the Commission lists.

    :param list_xml: The list document.
    :return: ``(reference, detail URL)`` pairs, e.g. ``("Report-2026-45", "https://…")``.
    """
    root = ET.fromstring(list_xml)  # noqa: S314 - see the import
    found = []
    for report in root.iter("weeklyReport"):
        ref, url = _text(report, "reference"), _text(report, "URL")
        if ref and url:
            found.append((ref, url))
    return found


def alerts(report_xml: bytes) -> list[list[Any]]:
    """
    The alerts in one weekly report, in the index's compact form.

    :param report_xml: The weekly report document.
    :return: ``[id, case, date, brand, name, model, gtins, product, risk, level]`` per alert.
    """
    root = ET.fromstring(report_xml)  # noqa: S314 - see the import
    rows: list[list[Any]] = []
    for item in root.iter("notifications"):
        m = _ALERT_ID_RE.search(_text(item, "reference"))
        if not m:
            continue
        # Older alerts give the barcode in the model field ("EAN code: 7586998000466.").
        written = f"{_text(item, 'barcode')} {_text(item, 'type_numberOfModel')}"
        barcodes = sorted({d.zfill(14) for d in _DIGITS_RE.findall(written) if valid_gtin(d)})
        if not barcodes and not (_text(item, "brand") and re.search(r"\d", _text(item, "type_numberOfModel"))):
            continue  # nothing a page could be matched on: no barcode, no brand with a model number
        rows.append(
            [
                int(m.group(1)),
                _text(item, "caseNumber")[:30],
                _date(_text(item, "publicationDate")),
                _text(item, "brand")[:100],
                _text(item, "name")[:200],
                _text(item, "type_numberOfModel")[:200],
                barcodes[:20],
                _text(item, "product")[:100],
                _text(item, "riskType")[:100],
                _text(item, "level")[:40],
            ]
        )
    return rows


def _get(client: httpx.Client, url: str, cache: Path | None, name: str) -> bytes:
    path = cache / f"{name}.xml" if cache else None
    if path and path.exists():
        return path.read_bytes()
    for attempt in range(4):
        try:
            response = client.get(url)
            response.raise_for_status()
            break
        except httpx.HTTPError:
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt + 1))
    if path:
        path.write_bytes(response.content)
    return response.content


def load() -> dict[str, Any]:
    """
    The vendored index, or an empty one.

    :return: The index document.
    """
    if not INDEX.exists():
        return {"source": SOURCE, "updated": "", "reports": [], "alerts": []}
    data: dict[str, Any] = json.loads(gzip.decompress(INDEX.read_bytes()))
    return data


def write(index: dict[str, Any]) -> None:
    """
    Write the index, deterministically (sorted, no timestamp in the gzip header).

    :param index: The index document.
    """
    index["reports"] = sorted(set(index["reports"]))
    unique = {row[0]: row for row in index["alerts"]}
    index["alerts"] = sorted(unique.values(), key=lambda row: (row[2], row[0]))
    body = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode()
    INDEX.write_bytes(gzip.compress(body, compresslevel=9, mtime=0))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="only compare; exit 1 when reports are missing")
    parser.add_argument("--cache", type=Path, default=None, help="keep the downloaded XML in this directory")
    args = parser.parse_args()
    if args.cache:
        args.cache.mkdir(parents=True, exist_ok=True)

    index = load()
    with httpx.Client(timeout=60, headers=_HEADERS, follow_redirects=True) as client:
        listed = reports(client.get(LIST_URL).raise_for_status().content)
        if not listed:
            sys.exit("The Safety Gate list named no weekly reports")
        have = set(index["reports"])
        missing = [(ref, url) for ref, url in listed if ref not in have]
        if args.check:
            if missing:
                print(
                    f"src/unfret/safety_gate_index.json.gz lacks {len(missing)} Safety Gate weekly report(s) "
                    f"(newest {missing[0][0]}): run `make safety-gate` and commit the result"
                )
                sys.exit(1)
            print(f"Safety Gate index up to date ({len(have)} weekly reports)")
            return
        for i, (ref, url) in enumerate(missing, start=1):
            index["alerts"] += alerts(_get(client, url, args.cache, ref))
            index["reports"].append(ref)
            if i % 50 == 0:
                print(f"{i}/{len(missing)} weekly reports", file=sys.stderr)
    index["source"] = SOURCE
    index["updated"] = datetime.now(UTC).date().isoformat() if missing else index.get("updated", "")
    write(index)
    print(f"{len(missing)} new weekly report(s); {len(index['alerts'])} alerts in the index")


if __name__ == "__main__":
    main()
