"""
Which consumer-law market a shop sells into, so each check applies the rules that hold there.

EU law requires an imprint, a VAT ID and a 14-day withdrawal right; US law requires none of these, so a US shop
must not be marked down for lacking them. The call is deliberately conservative: a shop counts as US only on
positive evidence (prices in US dollars, no EU VAT ID, not on an EU or UK country domain). Everything else keeps
the EU rules, as before.

The dollar evidence is the page's structured currency, or, when the page has none (a shop's home page), prices
written with "$" outnumbering those in euros and pounds in the page text.
"""

import re
from typing import Literal

from veridex.facts.schema import PageFacts
from veridex.net import hostname

Market = Literal["us", "eu"]

# Country-code domains whose shops answer to EU (or UK, which kept the same consumer rules) law.
_EU_TLDS = frozenset(
    {"at", "be", "bg", "cy", "cz", "de", "dk", "ee", "es", "eu", "fi", "fr", "gr", "hr", "hu", "ie", "it", "lt", "lu"}
    | {"lv", "mt", "nl", "pl", "pt", "ro", "se", "si", "sk", "uk"}
)
_DOLLAR_RE = re.compile(r"(?<![A-Za-z])(?:US)?\$\s?\d")
_EURO_POUND_RE = re.compile(r"[€£]\s?\d|\d\s?[€£]|\d\s?EUR\b|\bEUR\s?\d")
_MAX_TEXT = 50_000


def shop_market(facts: PageFacts, url: str = "", text: str = "") -> Market:
    """
    The market a shop sells into: ``"us"`` on positive evidence, else ``"eu"``.

    :param facts: The page facts.
    :param url: The page URL (its country domain counts against "us").
    :param text: The page text, read for "$" prices when the facts carry no currency.
    :return: The market.
    """
    host = hostname(url or facts.url)
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if facts.vat_ids or tld in _EU_TLDS:
        return "eu"
    currency = facts.currency.upper()
    if currency:
        return "us" if currency == "USD" else "eu"
    sample = text[:_MAX_TEXT]
    dollars = len(_DOLLAR_RE.findall(sample))
    return "us" if dollars and dollars > len(_EURO_POUND_RE.findall(sample)) else "eu"
