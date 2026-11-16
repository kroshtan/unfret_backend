"""
Which consumer-law market a shop sells into, so each check applies the rules that hold there.

EU law requires an imprint, a VAT ID and a 14-day withdrawal right; US law requires none of these, so a US shop
must not be marked down for lacking them, and neither must a shop elsewhere outside Europe. The call is
deliberately conservative: a shop counts as US, or "other", only on positive evidence. US: prices in US dollars, no
EU VAT ID, not on an EU, UK or other country domain. Other: a country domain outside Europe and the US (.ca, .au,
…), or prices in a currency of neither. A shop without any evidence keeps the EU rules, as before (in the evaluation
corpus such pages are mostly scams).

The dollar evidence is the page's structured currency, or, when the page has none (a shop's home page), prices
written with "$" outnumbering those in euros and pounds in the page text.
"""

import re
from typing import Literal

from unfret.facts.schema import PageFacts
from unfret.net import hostname

Market = Literal["us", "eu", "other"]

# Country-code domains whose shops answer to EU (or UK, which kept the same consumer rules) law.
_EU_TLDS = frozenset(
    {"at", "be", "bg", "cy", "cz", "de", "dk", "ee", "es", "eu", "fi", "fr", "gr", "hr", "hu", "ie", "it", "lt", "lu"}
    | {"lv", "mt", "nl", "pl", "pt", "ro", "se", "si", "sk", "uk"}
)
# Country-code domains of large markets outside Europe and the US, whose law asks for no EU imprint. (EEA and Swiss
# domains are in neither list: their shops follow EU-like rules, so they fall through to the currency.)
_OTHER_TLDS = frozenset(
    {"au", "nz", "ca", "mx", "br", "ar", "cl", "co", "pe", "jp", "kr", "cn", "hk", "tw", "sg", "my", "id", "ph", "th"}
    | {"vn", "in", "pk", "ae", "sa", "il", "tr", "za", "ng", "ke", "eg"}
)
# Currencies of the EU, the UK and the EEA/Swiss shops that follow EU-like consumer rules.
_EU_CURRENCIES = frozenset({"EUR", "GBP", "PLN", "SEK", "DKK", "CZK", "HUF", "RON", "BGN", "HRK", "CHF", "NOK", "ISK"})
_DOLLAR_RE = re.compile(r"(?<![A-Za-z])(?:US)?\$\s?\d")
_EURO_POUND_RE = re.compile(r"[€£]\s?\d|\d\s?[€£]|\d\s?EUR\b|\bEUR\s?\d")
_MAX_TEXT = 50_000


def shop_market(facts: PageFacts, url: str = "", text: str = "") -> Market:
    """
    The market a shop sells into: ``"us"`` or ``"other"`` on positive evidence, else ``"eu"``.

    :param facts: The page facts.
    :param url: The page URL (its country domain counts against "us").
    :param text: The page text, read for "$" prices when the facts carry no currency.
    :return: The market.
    """
    host = hostname(url or facts.url)
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if facts.vat_ids or tld in _EU_TLDS:
        return "eu"
    if tld in _OTHER_TLDS:
        return "other"
    currency = facts.currency.upper()
    if currency:
        if currency == "USD":
            return "us"
        foreign = len(currency) == 3 and currency.isalpha() and currency not in _EU_CURRENCIES  # an ISO code
        return "other" if foreign else "eu"
    sample = text[:_MAX_TEXT]
    dollars = len(_DOLLAR_RE.findall(sample))
    return "us" if dollars and dollars > len(_EURO_POUND_RE.findall(sample)) else "eu"


ServiceMarket = Literal["nl", "us", "other"]
_NL_POSTCODE_RE = re.compile(r"(?<![0-9])[1-9]\d{3}\s?[A-Z]{2}(?![A-Za-z])")
_US_STATE_ZIP_RE = re.compile(r"(?<![A-Za-z])[A-Z]{2},?\s+\d{5}(?:-\d{4})?(?![0-9])|,\s*(?:US|USA|United States)$")


def service_market(facts: PageFacts, url: str = "") -> ServiceMarket:
    """
    Where a service company works, for the identity rules that hold there: the Netherlands, the US, or elsewhere.

    On positive evidence only: a ``.nl`` domain, a KvK number, Dutch phone numbers or a Dutch postcode for the
    Netherlands; a US licence number, US phone numbers, a state and ZIP code or dollar prices for the US.

    :param facts: The page facts.
    :param url: The page URL.
    :return: ``"nl"``, ``"us"`` or ``"other"``.
    """
    host = hostname(url or facts.url)
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    countries = [p.country for p in facts.phones if p.country]
    nl = (
        tld == "nl"
        or bool(facts.kvk_numbers)
        or (bool(countries) and countries.count("NL") > countries.count("US"))
        # Not a US address's ZIP and directional ("2500 SW 7th Ave Amarillo, TX 79106" has no Dutch postcode).
        or any(_NL_POSTCODE_RE.search(a) and not _US_STATE_ZIP_RE.search(a) for a in facts.addresses)
    )
    if nl:
        return "nl"
    us = (
        bool(facts.licences)
        or (bool(countries) and countries.count("US") >= len(countries) / 2)
        or any(_US_STATE_ZIP_RE.search(a) for a in facts.addresses)
        or facts.currency.upper() == "USD"
    )
    if us and tld not in _EU_TLDS | _OTHER_TLDS:
        return "us"
    return "other"
