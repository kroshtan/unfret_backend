"""Locale-tolerant parsing of the numbers, prices and dates found on shop pages (en, de, fr, nl, es, it)."""

import re
from datetime import date

_MONTHS: dict[str, int] = {}
for _number, _names in enumerate(
    [
        "january jan januar janvier janv januari enero ene gennaio gen",
        "february feb februar février fév fevrier fev februari febrero febbraio",
        "march mar märz maerz mars maart marzo",
        "april apr avril avr abril aprile",
        "may mai mei mayo maggio mag",
        "june jun juni juin junio giugno giu",
        "july jul juli juillet juil julio luglio lug",
        "august aug august août aout augustus agosto ago",
        "september sep sept septembre septiembre settembre set",
        "october oct oktober okt octobre octubre ottobre ott",
        "november nov novembre noviembre",
        "december dec dezember dez décembre déc decembre diciembre dic dicembre",
    ],
    start=1,
):
    for _name in _names.split():
        _MONTHS[_name] = _number

_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DAY_MONTH_YEAR_RE = re.compile(r"\b(\d{1,2})\.?\s+([^\W\d_]{3,10})\.?\s+(\d{4})\b")
_MONTH_DAY_YEAR_RE = re.compile(r"\b([^\W\d_]{3,10})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_NUMBER_RE = re.compile(r"\d[\d.,  ]*")
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "zł": "PLN", "kr": "SEK"}
_CURRENCY_CODE_RE = re.compile(r"\b(USD|EUR|GBP|CAD|AUD|JPY|INR|PLN|SEK|CHF|DKK|NOK|TRY|MXN|BRL)\b")


def parse_date(text: str) -> date | None:
    """
    Find the first date in ``text``, in ISO form or with a month name in one of the supported languages.

    :param text: Text that may contain a date, e.g. "Date First Available: August 14, 2026".
    :return: The date, or ``None`` when there is none.
    """
    candidates: list[tuple[int, int, int, int]] = []  # (position, year, month, day)
    if m := _ISO_RE.search(text):
        candidates.append((m.start(), int(m[1]), int(m[2]), int(m[3])))
    if (m := _DAY_MONTH_YEAR_RE.search(text)) and (month := _MONTHS.get(m[2].lower())):
        candidates.append((m.start(), int(m[3]), month, int(m[1])))
    if (m := _MONTH_DAY_YEAR_RE.search(text)) and (month := _MONTHS.get(m[1].lower())):
        candidates.append((m.start(), int(m[3]), month, int(m[2])))
    for _, year, month, day in sorted(candidates):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def parse_number(text: str) -> float | None:
    """
    Parse the first number in ``text``, with either ``,`` or ``.`` as the decimal or thousands separator.

    The last separator followed by exactly one or two digits is the decimal point ("1.299,00", "4,5", "9.99");
    one followed by three digits groups thousands ("2,285", "2.285").

    :param text: Text containing a number.
    :return: The number, or ``None`` when there is none.
    """
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"[  ]", "", m.group()).rstrip(".,")
    last = max(raw.rfind(","), raw.rfind("."))
    if last == -1:
        return float(raw)
    decimals = raw[last + 1 :]
    if len(decimals) in {1, 2}:
        integer = re.sub(r"[.,]", "", raw[:last])
        return float(f"{integer}.{decimals}")
    return float(re.sub(r"[.,]", "", raw))


def parse_count(text: str) -> int | None:
    """
    Parse a count such as "2,285 ratings" or "1.204 Sternebewertungen".

    :param text: Text containing the count.
    :return: The count, or ``None`` when there is none.
    """
    m = _NUMBER_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group())
    return int(digits) if digits else None


def parse_price(text: str) -> tuple[float | None, str]:
    """
    Parse a price and its currency from text such as "$9.99", "12,99 €" or "EUR 1.299,00".

    :param text: The price text.
    :return: The amount (``None`` if absent) and an ISO currency code ("" if unknown).
    """
    currency = ""
    if m := _CURRENCY_CODE_RE.search(text):
        currency = m.group(1)
    else:
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in text:
                currency = code
                break
    return parse_number(text), currency


def parse_rating(text: str) -> float | None:
    """
    Parse a star rating such as "4.5 out of 5 stars" or "4,5 von 5 Sternen".

    :param text: The rating text.
    :return: The rating (0–5), or ``None`` when absent or out of range.
    """
    value = parse_number(text)
    return value if value is not None and 0 <= value <= 5 else None
