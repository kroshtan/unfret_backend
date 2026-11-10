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
