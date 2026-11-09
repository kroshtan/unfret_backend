#!/usr/bin/env python3
"""
Refresh the vendored EU VAT rates (``src/veridex/credits/vat_rates.json``) from the EU's TEDB.

TEDB (Taxes in Europe Database) publishes each member state's VAT rates as a SOAP service. Electronically supplied
services (what Unfret sells) are taxed at the standard rate everywhere, so only the standard rate is kept.

Usage:
    uv run scripts/update_vat_rates.py           # rewrite the table (review the diff, then commit)
    uv run scripts/update_vat_rates.py --check   # exit 1 if TEDB differs from the table (for a monthly job)
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

TABLE = Path(__file__).resolve().parent.parent / "src" / "veridex" / "credits" / "vat_rates.json"
ENDPOINT = "https://ec.europa.eu/taxation_customs/tedb/ws/"
ACTION = "urn:ec.europa.eu:taxud:tedb:services:v1:VatRetrievalService/RetrieveVatRates"
# TEDB's member state codes (Greece is EL there; ISO 3166 calls it GR).
MEMBER_STATES = [
    *("AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR", "HR", "HU"),
    *("IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"),
]
_ENVELOPE = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
 xmlns:urn="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService"
 xmlns:t="urn:ec.europa.eu:taxud:tedb:services:v1:IVatRetrievalService:types">
<soapenv:Header/><soapenv:Body><urn:retrieveVatRatesReqMsg>
<t:memberStates>{states}</t:memberStates><t:situationOn>{day}</t:situationOn>
</urn:retrieveVatRatesReqMsg></soapenv:Body></soapenv:Envelope>"""


def _text(block: str, tag: str) -> str | None:
    match = re.search(rf"<(?:\w+:)?{tag}>\s*([^<]*?)\s*</(?:\w+:)?{tag}>", block)
    return match.group(1) if match else None


def fetch(day: str) -> dict[str, str]:
    """
    Each member state's standard VAT rate on ``day``, from TEDB.

    :param day: ``YYYY-MM-DD``.
    :return: ISO 3166 country code → rate in percent (as text, e.g. ``"21"``).
    :raises SystemExit: If TEDB doesn't answer with a rate for every member state.
    """
    states = "".join(f"<t:isoCode>{code}</t:isoCode>" for code in MEMBER_STATES)
    response = httpx.post(
        ENDPOINT,
        content=_ENVELOPE.format(states=states, day=day).encode(),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ACTION},
        timeout=60,
    )
    response.raise_for_status()
    rates: dict[str, str] = {}
    for block in re.findall(r"<(?:\w+:)?vatRateResults>(.*?)</(?:\w+:)?vatRateResults>", response.text, re.DOTALL):
        state, kind, value = _text(block, "memberState"), _text(block, "type"), _text(block, "value")
        if kind != "STANDARD" or not state or value is None:
            continue
        rates["GR" if state == "EL" else state] = format(Decimal(value).normalize(), "f")
    missing = sorted({"GR" if s == "EL" else s for s in MEMBER_STATES} - rates.keys())
    if missing:
        sys.exit(f"TEDB returned no standard rate for {', '.join(missing)}")
    return dict(sorted(rates.items()))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="only compare; exit 1 on a difference")
    args = parser.parse_args()
    today = datetime.now(UTC).date().isoformat()
    rates = fetch(today)
    current = json.loads(TABLE.read_text()) if TABLE.exists() else {"standard_rates": {}}
    if args.check:
        if rates != current["standard_rates"]:
            changed = {c: (current["standard_rates"].get(c), r) for c, r in rates.items()}
            print("EU VAT rates changed:", {c: v for c, v in changed.items() if v[0] != v[1]})
            sys.exit(1)
        print(f"EU VAT rates unchanged ({len(rates)} member states, TEDB {today})")
        return
    document = {"source": "EU TEDB (standard rates)", "checked_on": today, "standard_rates": rates}
    TABLE.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {len(rates)} rates to {TABLE}")


if __name__ == "__main__":
    main()
