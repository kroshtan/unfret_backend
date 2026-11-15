"""
The EU Safety Gate index: dangerous non-food products that EU authorities have alerted on, matched to a product page.

Safety Gate (formerly RAPEX) is the EU's rapid alert system; the Commission publishes its alerts as open data (CC0,
the "Weekly Reports - XML format" distribution of the data.europa.eu dataset ``rapex-rapid-alert-system-non-food``).
``scripts/update_safety_gate.py`` (``make safety-gate``) turns them into the compact index vendored next to this
module; a monthly CI job says when it is behind.

Matching favours precision over recall:

- **Barcode**: a valid GTIN on the page (its product IDs or Product JSON-LD) equal to one in an alert.
- **Brand and model**: the page's brand equal to the alert's (accents, case and punctuation folded; "no brand" and
  "unknown" never match), and one of the alert's model numbers (letters and digits, at least five characters, or six
  digits) written as such in the page's title, model, MPN or SKU. Model numbers are compared whole, never as part of a
  longer one.

No lookups: the index is local.
"""

import gzip
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from unfret.facts import PageFacts
from unfret.market_prices import barcode
from unfret.price_store import gtins, valid_gtin

INDEX_PATH = Path(__file__).with_name("safety_gate_index.json.gz")
ALERT_URL = "https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/{id}"
_NOT_BRANDS = frozenset(
    {"", "unknown", "no brand", "nobrand", "none", "n a", "na", "not known", "without brand", "generic", "unbranded"}
)
_SPLIT_RE = re.compile(r"[,;/|]+|\s+(?:and|en|und|et|or)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Alert:
    """One Safety Gate alert, as the index keeps it."""

    id: int
    case: str
    date: str
    brand: str
    name: str
    model: str
    gtins: tuple[str, ...]
    product: str
    risk: str
    level: str

    @property
    def url(self) -> str:
        """
        The alert's public page.

        :return: The URL.
        """
        return ALERT_URL.format(id=self.id)


@dataclass(frozen=True)
class Match:
    """A page matched to an alert, and on what."""

    alert: Alert
    on: str  # "barcode" or "model"
    value: str  # the barcode or model number


def fold(text: str) -> str:
    """
    A name's comparable form: accents folded, lower case, letters and digits separated by single spaces.

    :param text: The name.
    :return: The folded name.
    """
    folded = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", fold(text).upper())


def model_codes(model: str) -> set[str]:
    """
    The distinctive model numbers in an alert's model field.

    :param model: E.g. ``"C52-683-145-000161"`` or ``"10.2.17.25.271, 2175334.5906.ONE SIZE"``.
    :return: Compact codes (letters and digits only, upper case) of at least five characters with a letter and a
        digit, or of six digits or more.
    """
    codes = set()
    for part in _SPLIT_RE.split(model):
        code = _compact(part)
        has_digit = any(c.isdigit() for c in code)
        if (len(code) >= 5 and has_digit and any(c.isalpha() for c in code)) or (code.isdigit() and len(code) >= 6):
            codes.add(code[:40])
    return codes


def page_codes(facts: PageFacts) -> set[str]:
    """
    Every way the page writes a model number.

    Whole title words, two or three of them run together, and the model, MPN and SKU fields.

    :param facts: The page facts.
    :return: Compact candidate codes.
    """
    words = [w for w in re.split(r"[\s,;()\[\]|]+", facts.title) if w]
    found = {_compact(w) for w in words}
    for n in (2, 3):
        found |= {_compact("".join(words[i : i + n])) for i in range(len(words) - n + 1)}
    product = facts.json_ld[0] if facts.json_ld else {}
    for value in (facts.product_ids.get("mpn"), product.get("model"), product.get("mpn"), product.get("sku")):
        if isinstance(value, str | int) and not isinstance(value, bool):
            found.add(_compact(str(value)))
    return {c for c in found if c}


@dataclass(frozen=True)
class Index:
    """The alerts by barcode and by brand."""

    by_gtin: dict[str, list[Alert]]
    by_brand: dict[str, list[tuple[frozenset[str], Alert]]]
    updated: str
    alerts: int


def build_index(data: dict[str, Any]) -> Index:
    """
    Index the vendored alerts.

    :param data: The parsed index file.
    :return: The lookup tables.
    """
    by_gtin: dict[str, list[Alert]] = {}
    by_brand: dict[str, list[tuple[frozenset[str], Alert]]] = {}
    rows = data.get("alerts", [])
    for row in rows:
        alert = Alert(int(row[0]), row[1], row[2], row[3], row[4], row[5], tuple(row[6]), row[7], row[8], row[9])
        for gtin in alert.gtins:
            by_gtin.setdefault(gtin, []).append(alert)
        brand = fold(alert.brand)
        codes = model_codes(alert.model)
        if brand not in _NOT_BRANDS and codes:
            by_brand.setdefault(brand, []).append((frozenset(codes), alert))
    return Index(by_gtin, by_brand, str(data.get("updated", "")), len(rows))


@cache
def index(path: Path = INDEX_PATH) -> Index:
    """
    The vendored index, loaded once per process.

    :param path: The index file.
    :return: The index (empty when the file is missing).
    """
    if not path.exists():
        return Index({}, {}, "", 0)
    return build_index(json.loads(gzip.decompress(path.read_bytes())))


def _page_gtins(facts: PageFacts) -> list[str]:
    found = [g for g in [barcode(facts)] if g]
    found += gtins(facts.json_ld)
    found += [v.zfill(14) for k, v in facts.product_ids.items() if k.startswith("gtin") and valid_gtin(v)]
    return list(dict.fromkeys(found))


def match(facts: PageFacts, idx: Index | None = None) -> Match | None:
    """
    The Safety Gate alert for the page's product, if there is one: by barcode first, then by brand and model.

    :param facts: The page facts.
    :param idx: The index; the vendored one by default.
    :return: The newest matching alert, or ``None``.
    """
    idx = idx or index()
    for gtin in _page_gtins(facts):
        if hits := idx.by_gtin.get(gtin):
            return Match(max(hits, key=lambda a: (a.date, a.id)), "barcode", gtin.lstrip("0") or gtin)
    brand = fold(facts.brand)
    if brand in _NOT_BRANDS or brand not in idx.by_brand:
        return None
    codes = page_codes(facts)
    found = [(alert, min(c & codes)) for c, alert in idx.by_brand[brand] if c & codes]
    if not found:
        return None
    alert, code = max(found, key=lambda h: (h[0].date, h[0].id))
    return Match(alert, "model", code)
