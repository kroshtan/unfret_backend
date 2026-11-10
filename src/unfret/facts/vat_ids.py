"""
Check digits of EU VAT numbers, and IBANs that only look like one.

Every member state builds a check digit (or two) into its VAT numbers, so a number that fails its own check was
never issued: it is made up or mistyped, and a shop that shows one has no registered VAT number on its page. This
is a local check, no lookup: it can only say a number is impossible, never that it is registered (that is VIES's
job, :mod:`unfret.graph.skills.eu_registry`). Countries whose check isn't implemented here answer ``None``.

The algorithms follow each tax administration's published scheme (as collected in python-stdnum).
"""

import re
from collections.abc import Callable

_IBAN_RE = re.compile(r"[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}")
# IBAN lengths per country (ISO 13616), for the countries whose IBANs share a prefix with a VAT number.
_IBAN_LENGTHS = {
    "AT": 20, "BE": 16, "BG": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24, "FI": 18, "FR": 27,
    "GR": 27, "HR": 21, "HU": 28, "IE": 22, "IT": 27, "LT": 20, "LU": 20, "LV": 21, "MT": 31, "NL": 18, "PL": 28,
    "PT": 25, "RO": 24, "SE": 24, "SI": 19, "SK": 24,
}  # fmt: skip


def _mod97(text: str) -> int:
    return int("".join(str(int(c, 36)) for c in text)) % 97


def is_iban_at(text: str, start: int) -> bool:
    """
    Whether a valid IBAN starts at ``start`` in ``text`` (its length and its mod-97 check both right).

    A Lithuanian bank account (``LT70 4010 …``) starts like a VAT number of the same country, so the VAT checks
    skip numbers that are only the start of one (:func:`unfret.graph.skills.eu_registry.in_iban`).

    :param text: Page text.
    :param start: Where the candidate begins.
    :return: ``True`` when the characters from ``start`` form an IBAN.
    """
    m = _IBAN_RE.match(text, start)
    if not m:
        return False
    compact = m.group().replace(" ", "")
    length = _IBAN_LENGTHS.get(compact[:2])
    if length is None or len(compact) < length:
        return False
    iban = compact[:length]
    return _mod97(iban[4:] + iban[:4]) == 1


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _weighted(digits: str, weights: tuple[int, ...]) -> int:
    return sum(w * int(d) for w, d in zip(weights, digits, strict=False))


def _at(n: str) -> bool | None:
    # ATU + 8 digits: the Luhn sum of the first seven, offset by 4.
    if not n.startswith("U"):
        return None
    d = n[1:]
    total = 0
    for i, c in enumerate(d[:7]):
        v = int(c) * (2 if i % 2 else 1)
        total += v // 10 + v % 10
    return (6 - total) % 10 == int(d[7])


def _be(n: str) -> bool | None:
    n = n.zfill(10)
    return 97 - int(n[:8]) % 97 == int(n[8:])


def _cz(n: str) -> bool | None:
    if len(n) != 8 or n.startswith("9"):
        return None  # 9 and 10 digits are personal numbers (birth numbers), checked differently
    check = (11 - _weighted(n, (8, 7, 6, 5, 4, 3, 2))) % 11
    return (check or 1) % 10 == int(n[7])


def _de(n: str) -> bool | None:
    product = 10
    for c in n[:8]:
        s = (int(c) + product) % 10 or 10
        product = 2 * s % 11
    return (11 - product) % 10 == int(n[8])


def _dk(n: str) -> bool | None:
    return _weighted(n, (2, 7, 6, 5, 4, 3, 2, 1)) % 11 == 0


def _fi(n: str) -> bool | None:
    return _weighted(n, (7, 9, 10, 5, 8, 4, 2, 1)) % 11 == 0


def _fr(n: str) -> bool | None:
    key, siren = n[:2], n[2:]
    if not key.isdigit():
        return None  # the newer alphanumeric keys use another scheme
    return int(key) == int(siren + "12") % 97


def _it(n: str) -> bool | None:
    return _luhn_ok(n) and int(n[:7]) != 0


def _lt(n: str) -> bool | None:
    if len(n) not in {9, 12} or n[-2] != "1":
        return False  # legal persons have 9 digits, others 12, and the second-to-last is always 1
    body = n[:-1]
    check = sum((1 + i % 9) * int(c) for i, c in enumerate(body)) % 11
    if check == 10:
        check = sum((1 + (i + 2) % 9) * int(c) for i, c in enumerate(body)) % 11
    return check % 10 == int(n[-1])


def _lu(n: str) -> bool | None:
    return int(n[:6]) % 89 == int(n[6:])


def _nl(n: str) -> bool | None:
    digits = n[:9]
    # Companies: the "elfproef" over the first nine digits. Sole traders (since 2020): mod 97 over the whole ID.
    elfproef = (_weighted(digits, (9, 8, 7, 6, 5, 4, 3, 2)) - int(digits[8])) % 11 == 0
    return elfproef or _mod97("NL" + n) == 1


def _pl(n: str) -> bool | None:
    return _weighted(n, (6, 5, 7, 2, 3, 4, 5, 6, 7)) % 11 == int(n[9])


def _pt(n: str) -> bool | None:
    check = 11 - _weighted(n, (9, 8, 7, 6, 5, 4, 3, 2)) % 11
    return (0 if check >= 10 else check) == int(n[8])


def _se(n: str) -> bool | None:
    return n.endswith("01") and _luhn_ok(n[:10])


_CHECKS: dict[str, Callable[[str], bool | None]] = {
    "AT": _at,
    "BE": _be,
    "CZ": _cz,
    "DE": _de,
    "DK": _dk,
    "FI": _fi,
    "FR": _fr,
    "IT": _it,
    "LT": _lt,
    "LU": _lu,
    "NL": _nl,
    "PL": _pl,
    "PT": _pt,
    "SE": _se,
}


def check_digits_ok(vat_id: str) -> bool | None:
    """
    Whether a VAT number passes its country's check digit.

    :param vat_id: A VAT ID in the extracted form: country prefix and number, no spaces (``NL810433941B01``).
    :return: ``True`` or ``False``, or ``None`` for a country (or number type) whose check isn't implemented.
    """
    country, number = vat_id[:2], vat_id[2:]
    check = _CHECKS.get(country)
    return check(number) if check else None
