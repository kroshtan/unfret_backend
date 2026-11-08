"""
How long the shop's domain has existed: RDAP registration data and the first certificate in the CT logs.

RDAP is the registries' structured successor to WHOIS (ICANN retired WHOIS for gTLDs in 2025). The first TLS
certificate ever logged for the domain (crt.sh) gives a second, registry-independent age, which still works when
registration data is redacted. Both are store-level facts, cached across callers by :mod:`veridex.fact_cache`.
"""

import json
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

from veridex.config import settings
from veridex.fact_cache import cached_fact
from veridex.graph.skills import STOREFRONTS, Observation, Skill, SkillOutput
from veridex.graph.state import AnalysisState
from veridex.net import fetch_public, hostname

_RDAP_TIMEOUT_S = 10.0
_CRTSH_TIMEOUT_S = 15.0
_REDACTED_MARKERS = ("redacted", "privacy", "withheld", "data protected", "not disclosed")


def _parse_rdap_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_rdap(data: dict[str, Any]) -> dict[str, Any]:
    """
    Pick the useful fields out of an RDAP domain response.

    :param data: The parsed RDAP JSON.
    :return: ``registered`` and ``expires`` (ISO dates or ``None``), ``registrar`` and ``redacted`` (bool).
    """
    events = {e.get("eventAction"): _parse_rdap_date(e.get("eventDate")) for e in data.get("events", []) or []}
    registrar = ""
    for entity in data.get("entities", []) or []:
        if "registrar" in (entity.get("roles") or []):
            for field in (entity.get("vcardArray") or [None, []])[1] or []:
                if isinstance(field, list) and len(field) >= 4 and field[0] == "fn":
                    registrar = str(field[3])
    text = json.dumps(data.get("remarks", [])) + json.dumps(data.get("entities", []))
    registered, expires = events.get("registration"), events.get("expiration")
    return {
        "registered": registered.isoformat() if registered else None,
        "expires": expires.isoformat() if expires else None,
        "registrar": registrar[:200],
        "redacted": any(marker in text.lower() for marker in _REDACTED_MARKERS),
    }


async def rdap_lookup(domain: str) -> dict[str, Any] | None:
    """
    Look a domain up via the RDAP bootstrap service, trying parent domains for subdomains.

    :param domain: A hostname such as ``shop.example.co.uk``.
    :return: The parsed fields (see :func:`parse_rdap`), or ``None`` if no registry answered.
    """
    labels = domain.split(".")
    for start in range(min(3, max(len(labels) - 1, 1))):
        candidate = ".".join(labels[start:])
        try:
            body = await fetch_public(settings.rdap_bootstrap_url + quote(candidate), timeout=_RDAP_TIMEOUT_S)
            return parse_rdap(json.loads(body))
        except Exception:  # noqa: BLE001 - not found / not supported: try the parent domain
            continue
    return None


def earliest_certificate(entries: list[dict[str, Any]]) -> str | None:
    """
    The earliest ``not_before`` date among crt.sh entries.

    :param entries: crt.sh JSON entries.
    :return: ISO date, or ``None`` when there are none.
    """
    dates = [d for e in entries if (d := _parse_rdap_date(e.get("not_before")))]
    return min(dates).isoformat() if dates else None


async def first_certificate(domain: str) -> str | None:
    """
    Ask crt.sh for the first certificate logged for the domain.

    :param domain: The registrable domain.
    :return: ISO date of the earliest certificate, or ``None`` if crt.sh had none or didn't answer.
    """
    url = f"{settings.crtsh_url}?q={quote(domain)}&output=json"
    try:
        entries = json.loads(await fetch_public(url, timeout=_CRTSH_TIMEOUT_S, max_bytes=20 * 1024 * 1024))
    except Exception:  # noqa: BLE001 - crt.sh is often slow or overloaded; this signal is optional
        return None
    return earliest_certificate(entries) if isinstance(entries, list) else None


def _days_since(iso: str | None, today: date) -> int | None:
    return (today - date.fromisoformat(iso)).days if iso else None


class DomainAgeSkill(Skill):
    """Domain registration age, registration term, privacy and first-certificate age for storefronts."""

    name = "domain_age"
    description = "Checks domain registration (RDAP) and first certificate (CT logs) for pop-up store signals."
    page_types = STOREFRONTS
    features = ("known", "age_years", "under_6_months", "term_years", "redacted", "cert_age_years")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look up the page's domain in RDAP and the CT logs (cached per domain).

        :param state: Current graph state; uses ``url``.
        :return: The skill's features and findings.
        """
        domain = hostname(state.get("url", ""))
        if not domain or not settings.network_lookups:
            return self.output({"known": False}, note="No domain to look up.")
        rdap = await cached_fact("rdap", domain, lambda: rdap_lookup(domain))
        cert = await cached_fact("crtsh", domain, lambda: first_certificate(domain))
        if rdap is None and cert is None:
            return self.output({"known": False}, note=f"No registration or certificate data for {domain}.")

        today = datetime.now(tz=UTC).date()
        age = _days_since((rdap or {}).get("registered"), today)
        cert_age = _days_since(cert, today)
        expires = (rdap or {}).get("expires")
        registered = (rdap or {}).get("registered")
        term = (
            (date.fromisoformat(expires) - date.fromisoformat(registered)).days / 365
            if expires and registered
            else None
        )

        observations: list[Observation] = []
        if age is not None:
            observations.append(
                Observation(type="registered", evidence=f"{domain} registered {registered} ({age} days ago).")
            )
        if cert_age is not None:
            observations.append(
                Observation(type="first_certificate", evidence=f"First certificate for {domain}: {cert}.")
            )
        if rdap and rdap.get("redacted"):
            observations.append(Observation(type="registrant", evidence="Registrant details are redacted."))
        if term is not None and term <= 1.1:
            observations.append(Observation(type="term", evidence="Registered for one year only."))

        youngest = min((d for d in (age, cert_age) if d is not None), default=None)
        return self.output(
            {
                "known": True,
                "age_years": (age if age is not None else cert_age or 0) / 365,
                "under_6_months": youngest is not None and youngest < 183,
                "term_years": term if term is not None else 0.0,
                "redacted": bool(rdap and rdap.get("redacted")),
                "cert_age_years": (cert_age or 0) / 365,
            },
            observations,
        )
