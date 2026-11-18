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


# What a seller adds to the brand's name and stays the brand: "AnkerDirect", "Nike-EU", "Airmax Official".
_SELLER_SUFFIXES = _SUFFIXES | {
    "eu",
    "us",
    "uk",
    "de",
    "nl",
    "fr",
    "it",
    "es",
    "europe",
    "global",
    "international",
    "retail",
    "sales",
    "trading",
    "benelux",
}
_PIECE_RE = re.compile(r"[^\W_]+|[^\w\s]+|_+")
# Characters that pass for others in a name: digits for letters, Cyrillic and Greek lookalikes for Latin.
_HOMOGLYPHS = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "l": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "х": "x",
        "у": "y",
        "і": "i",
        "ӏ": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ι": "i",
        "α": "a",
    }
)
# A lookalike name is at least this long: below it, one letter apart is just another word ("Nike" and "Mike").
LOOKALIKE_MIN = 4
# Up to this many letters, a name a letter apart from the brand by a changed letter is another word, not a lookalike.
LOOKALIKE_SHORT = 6


def _pieces(text: str) -> list[tuple[str, bool]]:
    """
    A name's words and the parts of its hyphened words, folded, each with whether it hangs on the one before.

    :param text: The name.
    :return: ``(piece, glued)`` pairs: ``glued`` when only punctuation separates it from the piece before.
    """
    pieces: list[tuple[str, bool]] = []
    for token in _fold(_MARKS_RE.sub(" ", text)).split():
        seen = glued = False
        for part in _PIECE_RE.findall(token):
            if _WORD_RE.fullmatch(part):
                pieces.append((part, glued))
                seen, glued = True, False
            else:
                glued = seen and part not in {"&", "+"}
    return pieces


def _runs(pieces: list[tuple[str, bool]], longest: int) -> list[tuple[int, int, str]]:
    return [
        (i, j, "".join(p for p, _ in pieces[i : j + 1]))
        for i in range(len(pieces))
        for j in range(i, min(i + longest, len(pieces)))
    ]


def _names_as_words(key: str, longest: int, text: str) -> bool:
    """Whether ``key`` is a run of whole words in ``text`` (or one word plus a seller's suffix: "AnkerDirect")."""
    pieces = _pieces(text)
    for i, j, run in _runs(pieces, longest + 1):
        if run != key and not (run.startswith(key) and run[len(key) :] in _SELLER_SUFFIXES and i == j):
            continue
        # A single letter hyphened on is a lookalike ("Nike-e"), not the brand with a word added.
        before = i > 0 and pieces[i][1] and len(pieces[i - 1][0]) == 1
        after = j + 1 < len(pieces) and pieces[j + 1][1] and len(pieces[j + 1][0]) == 1
        if not before and not after:
            return True
    return False


def same_company(brand: str | None, seller: str | None) -> bool | None:
    """
    Whether a seller is the brand itself: the brand's name, as whole words, in the seller's name, or the other way.

    "Airmax nasal dilator", "Airmax Official", "AIRMAX-Store BV" and "Air Max Benelux" are all the brand Airmax
    (case, spaces, hyphens and accents don't count); "Nikee", "Nlke" and "Nike-e" aren't Nike (:func:`lookalike`).

    :param brand: The brand.
    :param seller: The seller's name.
    :return: ``True`` or ``False``, or ``None`` when either is missing or isn't a name.
    """
    brand_k, seller_k = brand_key(brand), brand_key(seller)
    if brand_k is None or seller_k is None or brand is None or seller is None:
        return None
    if brand_k == seller_k:
        return True
    brand_words, seller_words = len(_core(_words(brand))), len(_core(_words(seller)))
    return _names_as_words(brand_k, brand_words, seller) or _names_as_words(seller_k, seller_words, brand)


def _edits(a: str, b: str) -> int:
    """The optimal string alignment distance: insertions, deletions, substitutions and swaps of neighbours."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            row[j] = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[-2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def _glyphs(text: str) -> str:
    return text.translate(_HOMOGLYPHS).replace("rn", "m").replace("vv", "w")


def _close(name: str, key: str) -> bool:
    """
    Whether two different names are one slip apart, compared with lookalike characters made the same.

    A short name (up to six letters) is close by one letter added, dropped or swapped with its neighbour, never by
    one letter changed: "Bose" and "Boss", "Camper" and "Casper" are other words, not lookalikes. A longer one may
    differ by two edits of any kind.
    """
    a, b = _glyphs(name), _glyphs(key)
    if a == b:
        return True
    if a[0] != b[0]:
        return False
    if len(key) > LOOKALIKE_SHORT:
        return _edits(a, b) <= 2
    return _edits(a, b) == 1 and (len(a) != len(b) or sorted(a) == sorted(b))


def lookalike(brand: str | None, seller: str | None) -> str | None:
    """
    The seller's name when it looks like the brand's without being it: "Nikee", "Nlke", "N1ke", "Nike-e".

    The seller's whole name counts (without "Official Store", a company form or a seller's suffix such as "Direct"),
    not a word somewhere in it ("Fleet Feet" is no lookalike of Feit). Close is :func:`_close`, in a name of at least
    :data:`LOOKALIKE_MIN` letters, with the same first letter. A seller that is the brand (:func:`same_company`) is no
    lookalike.

    :param brand: The brand.
    :param seller: The seller's name.
    :return: The seller's name, folded to letters and digits, or ``None``.
    """
    key = brand_key(brand)
    if key is None or seller is None or len(key) < LOOKALIKE_MIN or same_company(brand, seller) is not False:
        return None
    words = _words(seller)
    while len(words) > 1 and words[-1] in _SELLER_SUFFIXES:
        words.pop()
    name = "".join(words)
    # A seller's suffix run on to the name ("NikeeOfficial"); the short ones ("eu", "co") only as words of their own.
    for suffix in sorted((s for s in _SELLER_SUFFIXES if len(s) >= LOOKALIKE_MIN), key=len, reverse=True):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name.removesuffix(suffix)
            break
    if len(name) < LOOKALIKE_MIN or name == key:
        return None
    return name if _close(name, key) else None
