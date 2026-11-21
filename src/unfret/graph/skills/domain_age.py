"""
How long the shop's domain has existed: RDAP registration data and the first certificate in the CT logs.

RDAP is the registries' structured successor to WHOIS (ICANN retired WHOIS for gTLDs in 2025). The first TLS
certificate ever logged for the domain (crt.sh) gives a second, registry-independent age, which still works when
registration data is redacted. Both are store-level facts, cached across callers by :mod:`unfret.fact_cache`.

Many registries (.nl, .de and other EU country domains) publish no registration date at all. For a web shop there,
the oldest sighting of the address says it has been online since at least that day. Three sources, the oldest
counting:

- **Common Crawl's** oldest copy of the home page among a few crawls years apart
  (:func:`unfret.graph.skills.common_crawl.first_capture`, cached per address);
- else (Common Crawl has none, or can't be asked) **the Internet Archive's** first copy
  (:func:`unfret.graph.skills.domain_history.first_capture`, one request within its pace and daily cap);
- **Unfret's own** first check of the shop: the day a page Unfret fetched itself first showed it, from the shop store
  (:meth:`unfret.shop_store.ShopStore.first_fetched`: the shop's domain and a day, nothing about who checked).

That is a minimum, never the registration date, so it is kept apart (``archive_age_years``, ``age_from_archive``,
and which source: ``first_seen_common_crawl``, ``first_seen_unfret``, else the Internet Archive) and only counts for
the shop once the deep check's look at the address's earlier copies didn't contradict it
(:func:`unfret.shopper.address_years`): an expired address bought again keeps its old copies.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote

import httpx

from unfret import shop_store, shopper
from unfret.config import settings
from unfret.fact_cache import cached_fact
from unfret.graph.skills import (
    SERVICE_SITES,
    STOREFRONTS,
    Observation,
    Skill,
    SkillOutput,
    common_crawl,
    domain_history,
)
from unfret.graph.skills.claims import claimed_founding
from unfret.graph.state import AnalysisState
from unfret.net import fetch_public, hostname
from unfret.price_store import registrable_domain

# The first-seen sources (unfret.shopper names them; it imports the graph, so not imported from there).
COMMON_CRAWL, WAYBACK, UNFRET = "commoncrawl", "wayback", "unfret"

_RDAP_TIMEOUT_S = 10.0
_CRTSH_TIMEOUT_S = 15.0
_REDACTED_MARKERS = ("redacted", "privacy", "withheld", "data protected", "not disclosed")
# A shop that claims at least this many years ...
FOUNDED_MIN_YEARS = 5
# ... on a domain whose registration and first certificate are both younger than this contradicts itself.
FOUNDED_DOMAIN_MAX_DAYS = 365


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


def candidates(domain: str) -> list[str]:
    """
    The names to try in RDAP, the likely registrable domain first.

    Without a public-suffix list: the last two labels (``example.com``), then the last three (``example.co.uk``),
    then the full hostname.

    :param domain: A hostname such as ``shop.example.co.uk``.
    :return: Distinct candidates, most likely first.
    """
    labels = domain.lower().split(".")
    names = [".".join(labels[-n:]) for n in (2, 3) if len(labels) >= n] + [".".join(labels)]
    return list(dict.fromkeys(n for n in names if "." in n))


def _not_found(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 404}


async def rdap_lookup(domain: str) -> dict[str, Any] | None:
    """
    Look a domain up via the RDAP bootstrap service, the registrable domain first.

    "Not found" (404) moves on to the next candidate, and ``None`` means no candidate exists. Any other failure (a
    timeout, a 5xx) raises instead, so a flaky registry isn't cached as "this domain has no record".

    :param domain: A hostname such as ``shop.example.co.uk``.
    :return: The parsed fields (see :func:`parse_rdap`), or ``None`` if no candidate is registered.
    :raises Exception: When a registry fails other than with "not found".
    """
    for candidate in candidates(domain):
        try:
            body = await fetch_public(settings.rdap_bootstrap_url + quote(candidate), timeout=_RDAP_TIMEOUT_S)
        except Exception as exc:
            if _not_found(exc):
                continue
            raise
        return parse_rdap(await asyncio.to_thread(json.loads, body))
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
    :return: ISO date of the earliest certificate, or ``None`` if crt.sh had none.
    :raises Exception: When crt.sh fails or times out (not cached; the next check asks again).
    """
    url = f"{settings.crtsh_url}?q={quote(domain)}&output=json"
    try:
        body = await fetch_public(url, timeout=_CRTSH_TIMEOUT_S, max_bytes=20 * 1024 * 1024)
    except Exception as exc:
        if _not_found(exc):
            return None
        raise  # crt.sh is often slow or overloaded: not a fact worth caching
    entries = await asyncio.to_thread(json.loads, body)  # up to 20 MB of JSON
    return earliest_certificate(entries) if isinstance(entries, list) else None


_HOW = {
    COMMON_CRAWL: "the oldest copy of {domain} found there",
    WAYBACK: "its first copy of {domain}",
    UNFRET: "the first time Unfret checked {domain}",
}


async def _archive_first_seen(host: str) -> tuple[str, str] | None:
    """
    The oldest copy of a web shop's home page in the web archives: Common Crawl, else the Internet Archive.

    Each within its own pace: paced out, refused or unreachable, it is skipped this time (not cached).

    :param host: The shop's host.
    :return: ``(ISO date, source)``, or ``None``.
    """
    try:
        found = await cached_fact("commoncrawl_first", host, lambda: common_crawl.first_capture(host))
    except Exception:  # noqa: BLE001  (paced out, refused or unreachable: the Internet Archive instead)
        found = None
    if found:
        return found, COMMON_CRAWL
    try:
        found = await cached_fact("wayback_first", host, lambda: domain_history.first_capture(host))
    except Exception:  # noqa: BLE001  (paced out, refused or unreachable: no age from the archives this time)
        return None
    return (found, WAYBACK) if found else None


async def _unfret_first_seen(host: str) -> tuple[str, str] | None:
    """
    The day a page Unfret fetched itself first showed the shop (the shop store), when the store is on.

    :param host: The shop's host.
    :return: ``(ISO date, "unfret")``, or ``None`` (a shop Unfret hasn't fetched before, or a failing store).
    """
    if not settings.fingerprints:
        return None
    try:
        day = await shop_store.store().first_fetched(registrable_domain(host))
    except Exception:  # noqa: BLE001  (an optional store: no age from it this time)
        return None
    return (day.isoformat(), UNFRET) if day else None


async def _first_seen(host: str, state: AnalysisState) -> tuple[str, str] | None:
    """
    The oldest sighting of a web shop's address: the web archives' (when the archive look is on) or Unfret's own.

    :param host: The shop's host.
    :param state: Current graph state; uses ``page_type``.
    :return: ``(ISO date, source)``, or ``None``.
    """
    if state.get("page_type") != "storefront":
        return None
    archived = await _archive_first_seen(host) if settings.domain_history else None
    own = await _unfret_first_seen(host)
    return min((s for s in (archived, own) if s), key=lambda s: s[0], default=None)


def _days_since(iso: str | None, today: date) -> int | None:
    return (today - date.fromisoformat(iso)).days if iso else None


class DomainAgeSkill(Skill):
    """Domain registration age, registration term, privacy and first-certificate age for storefronts."""

    name = "domain_age"
    description = "Checks domain registration (RDAP) and first certificate (CT logs) for pop-up store signals."
    page_types = STOREFRONTS | SERVICE_SITES
    retry = True
    features = (
        "known",
        "age_years",
        "under_6_months",
        "term_years",
        "redacted",
        "cert_age_years",
        "founded_claim_years",
        "founded_contradicted",
        "archive_age_years",
        "archive_first_year",
        "age_from_archive",
        "first_seen_common_crawl",
        "first_seen_unfret",
    )

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Look up the page's domain in RDAP, the CT logs and, if need be, its first sighting (cached per domain).

        The web archives' first copy of the home page, and Unfret's own first check, are looked up only on a web
        shop whose registry gives no date.

        :param state: Current graph state; uses ``url`` and ``page_type``.
        :return: The skill's features and findings.
        """
        domain = hostname(state.get("url", ""))
        if not domain or not settings.network_lookups:
            return self.output({"known": False}, note="No domain to look up.")
        # Both lookups at once; a failed one (raised, so not cached) only costs its own signal.
        rdap_result, cert_result = await asyncio.gather(
            cached_fact("rdap", domain, lambda: rdap_lookup(domain)),
            cached_fact("crtsh", domain, lambda: first_certificate(domain)),
            return_exceptions=True,
        )
        rdap = None if isinstance(rdap_result, BaseException) else rdap_result
        cert = None if isinstance(cert_result, BaseException) else cert_result
        registered = (rdap or {}).get("registered")
        seen = await _first_seen(domain, state) if registered is None else None
        first_seen, source = seen or (None, WAYBACK)
        today = datetime.now(tz=UTC).date()
        archive_age = _days_since(first_seen, today)
        archive: dict[str, float | bool] = {}
        if first_seen is not None and archive_age is not None:
            archive = {
                "archive_age_years": archive_age / 365,
                "archive_first_year": float(first_seen[:4]),
                "age_from_archive": archive_age > (_days_since(cert, today) or 0),
                "first_seen_common_crawl": source == COMMON_CRAWL,
                "first_seen_unfret": source == UNFRET,
            }
        archived = (
            [
                Observation(
                    type="archive_first_capture",
                    evidence=f"Online since at least {first_seen}, per {shopper.SEEN_BY[source]} "
                    f"({_HOW[source].format(domain=domain)}; a minimum, not the registration date).",
                )
            ]
            if archive
            else []
        )
        if rdap is None and cert is None:
            note = f"No registration or certificate data for {domain}."
            return self.output({"known": False} | archive, archived, note=note)

        age = _days_since(registered, today)
        cert_age = _days_since(cert, today)
        expires = (rdap or {}).get("expires")
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
        observations += archived
        if rdap and rdap.get("redacted"):
            observations.append(Observation(type="registrant", evidence="Registrant details are redacted."))
        if term is not None and term <= 1.1:
            observations.append(Observation(type="term", evidence="Registered for one year only."))

        youngest = min((d for d in (age, cert_age) if d is not None), default=None)
        features: dict[str, float | bool] = {
            "known": True,
            "age_years": (age if age is not None else cert_age or 0) / 365,
            "under_6_months": youngest is not None and youngest < 183,
            "term_years": term if term is not None else 0.0,
            "redacted": bool(rdap and rdap.get("redacted")),
            "cert_age_years": (cert_age or 0) / 365,
        } | archive
        # Claims vs facts: "since 1998" on a domain that has existed for months. The oldest evidence of the domain
        # counts (a re-registered domain keeps its old certificates), so only a domain young by every measure does,
        # the archive's first copy included (which only ever makes a domain older, never young).
        founded = claimed_founding(state["facts"], state.get("cleaned_content", ""), today.year)
        if founded is not None:
            year, quote = founded
            claimed = today.year - year
            oldest = max(d for d in (age, cert_age, archive_age) if d is not None)
            features["founded_claim_years"] = claimed
            if claimed >= FOUNDED_MIN_YEARS and oldest < FOUNDED_DOMAIN_MAX_DAYS:
                features["founded_contradicted"] = True
                since = f"registered {registered}" if age is not None else f"first certificate {cert}"
                observations.append(
                    Observation(
                        type="founded_contradicted",
                        evidence=f'Says "{quote}"; {domain} is only {oldest} days old ({since}).',
                    )
                )
        return self.output(features, observations)
