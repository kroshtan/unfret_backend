"""
Brand names as a page writes them, made comparable: "Nike™", "NIKE Official Store" and "nike" are one brand.

The image index keeps the brand a listing names next to its photo (:mod:`unfret.image_store`), so the same photo
sold under different brand names can be told from the same brand's photo on its resellers' shops. Two rules hold
everywhere a brand is compared:

- **No brand is "unknown", never "different".** A listing without a brand, or with a word that isn't one
  ("Generic", "Unbranded", "N/A"), compares as nothing at all (:func:`same_brand` returns ``None``).
- **A brand is compared by its key**: accents folded, case, marks (™ ® ©) and punctuation dropped, spaces ignored,
  and words that say where it is sold rather than what it is ("Official", "Store", "Shop", a company form like
  "GmbH" or "Ltd") taken off the end.
"""

import re
import unicodedata

MAX_LABEL = 60
# Words that aren't a brand.
_NOT_BRANDS = frozenset(
    {"generic", "unbranded", "nobrand", "unknown", "na", "none", "other", "diverse", "brand", "oem", "various"}
    | {"shop", "store"}
)
# Words taken off the end of a brand name ("ACME Official Store" is ACME): where it is sold, or a company form.
_SUFFIXES = frozenset(
    {
        "official",
        "store",
        "shop",
        "flagship",
        "direct",
        "online",
        "inc",
        "llc",
        "ltd",
        "limited",
        "gmbh",
        "bv",
        "sa",
        "sl",
        "srl",
        "corp",
        "co",
        "company",
    }
)
_ARTICLES = frozenset({"the", "a", "my", "de", "het", "le", "la", "les", "der", "die", "das", "el"})
_MARKS_RE = re.compile(r"[™®©℠]|\((?:tm|r|c)\)", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(_fold(_MARKS_RE.sub(" ", text)))


def _core(words: list[str]) -> list[str]:
    """The words of a brand name without the trailing "official store" and company form (never down to an article)."""
    words = list(words)
    while len(words) > 1 and words[-1] in _SUFFIXES and not (len(words) == 2 and words[0] in _ARTICLES):
        words.pop()
    return words


def brand_key(brand: str | None) -> str | None:
    """
    A brand's comparable key.

    :param brand: The brand as the page (or the catalogue) writes it.
    :return: Folded letters and digits without spaces, or ``None`` when it isn't a brand.
    """
    if not brand:
        return None
    key = "".join(_core(_words(brand)))
    if len(key) < 2 or len(key) > MAX_LABEL or key in _NOT_BRANDS:
        return None
    return key


def label(brand: str | None) -> str | None:
    """
    The brand to keep with a listing's photo: the page's own spelling without marks and the trailing "Official Store".

    :param brand: The brand as the page writes it.
    :return: At most :data:`MAX_LABEL` characters, or ``None`` when it isn't a brand.
    """
    if brand is None or brand_key(brand) is None:
        return None
    tokens = _SPACES_RE.sub(" ", _MARKS_RE.sub(" ", brand)).strip(" -–—|:·,.").split(" ")
    keep = len(_core(_words(" ".join(tokens))))  # the key's words: drop whole trailing tokens beyond them
    while len(tokens) > 1 and len(_words(" ".join(tokens[:-1]))) >= keep:
        tokens.pop()
    return " ".join(tokens).strip(" -–—|:·,.")[:MAX_LABEL] or None


def same_brand(a: str | None, b: str | None) -> bool | None:
    """
    Whether two brand names are the same brand.

    :param a: One brand.
    :param b: The other.
    :return: ``True`` or ``False``, or ``None`` when either is missing or isn't a brand (unknown, never different).
    """
    ka, kb = brand_key(a), brand_key(b)
    if ka is None or kb is None:
        return None
    return ka == kb


def named_in(brand: str | None, text: str) -> bool:
    """
    Whether a text names the brand: a reseller's title saying "Philips" sells Philips, whatever its brand field says.

    :param brand: The brand.
    :param text: The text (a product title).
    :return: ``True`` when the brand's words appear in the text as words (or, for a longer name, run together).
    """
    key = brand_key(brand)
    if key is None or not text:
        return False
    words = _core(_words(brand or ""))
    haystack = _words(text)
    joined = " ".join(haystack)
    if f" {' '.join(words)} " in f" {joined} ":
        return True
    return len(key) >= 5 and key in "".join(haystack)


def is_shop_name(brand: str | None, domain: str) -> bool:
    """
    Whether a brand is just the shop's own name ("Gadgetshop" on gadgetshop.nl): many shops fill the brand field so.

    :param brand: The brand.
    :param domain: The shop's registrable domain (or a marketplace's family name).
    :return: ``True`` when the brand's key and the domain's name are the same, or one starts with the other.
    """
    key = brand_key(brand)
    stem = "".join(_words(domain.split("/", maxsplit=1)[0].rsplit(".", 1)[0])) if domain else ""
    if key is None or len(stem) < 3:
        return False
    return key == stem or (min(len(key), len(stem)) >= 4 and (key.startswith(stem) or stem.startswith(key)))
