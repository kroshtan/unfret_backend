"""
The brand name as a page writes it, made usable: marketplace bylines unwrapped, sentences and store links dropped.

Amazon's byline is a link to the brand's store, and its text wraps the brand in a phrase in the page's language
("Visit the Airmax Store", "De Airmax Store openen", "Besuche den Airmax-Store", "Marque : Airmax"). Taken whole,
that phrase was looked up as a brand and judged twice over ("no presence of its own" and "known brand").
:func:`clean_brand` takes the brand out of the known phrases, and gives up (no brand) on a name that still reads
like a sentence or a link to a store: a quiet result instead of a wrong claim. The extension's
``src/extract/brand.ts`` is a line-for-line port; the parity tests keep the two in step.
"""

import re
import unicodedata

# The byline phrases of Amazon's storefronts, group 1 the brand. Matched against the whole squashed text.
_BYLINE_RES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^visit the\s+(.+?)\s+(?:store|shop|storefront)$",  # en
        r"^(?:besuche|besuchen sie)\s+den\s+(.+?)(?:-|\s+)(?:store|shop|markenshop)$",  # de
        r"^bezoek\s+de\s+(.+?)(?:-|\s+)(?:winkel|store|shop)$",  # nl, older
        r"^de\s+(.+?)(?:-|\s+)(?:store|winkel|shop)\s+openen$",  # nl
        r"^ga\s+naar\s+de\s+(.+?)(?:-|\s+)(?:store|winkel|shop)$",  # nl, Belgium
        r"^visite[rz]?\s+la\s+boutique(?:\s+de)?\s+(.+)$",  # fr
        r"^visita\s+lo\s+store\s+di\s+(.+)$",  # it
        r"^visita\s+la\s+tienda\s+de\s+(.+)$",  # es
        r"^visite\s+a\s+loja(?:\s+d[aeo])?\s+(.+)$",  # pt
        r"^odwiedź\s+sklep(?:\s+marki)?\s+(.+)$",  # pl
        r"^besök\s+(.+?)(?:-|\s+)(?:butiken|butik|store)$",  # se
        r"^(.+?)\s+mağazasını\s+ziyaret\s+edin$",  # tr
        r"^(.+?)\s*のストアを表示$",  # ja
        r"^(?:brand|marke|marque|merk|marca|marka|märke|varumärke|marchio|ブランド)\s*[:：]\s*(.+)$",
    )
)
# A store word joined to the brand by a hyphen: "Pokémon-Store", "Philips-winkel".
_HYPHEN_STORE_RE = re.compile(
    r"^(.+?)\s*[-‐–]\s*(?:store|shop|winkel|butiken|butik|boutique|markenshop)$", re.IGNORECASE
)
# Words that make a name a link to a store rather than a brand: "visit" and "open" in the marketplaces' languages
# (accents folded), and store words, which a brand ends with at most ("The Body Shop").
_VISIT_WORDS = frozenset(
    {
        "visit",
        "visiter",
        "visitez",
        "visita",
        "visite",
        "visitar",
        "besuche",
        "besuchen",
        "bezoek",
        "bezoeken",
        "openen",
        "offnen",
        "ouvrir",
        "abrir",
        "aprire",
        "odwiedz",
        "besok",
        "oppna",
        "ziyaret",
    }
)
_STORE_WORDS = frozenset(
    {"store", "shop", "winkel", "boutique", "tienda", "negozio", "sklep", "butik", "butiken", "loja", "storefront"}
)
# A brand is a name, not a sentence.
MAX_WORDS = 5
_WORD_RE = re.compile(r"[^\W_]+")
_SPACES_RE = re.compile(r"\s+")
_TRIM = " -–—|:·,"
# Company forms after a store word keep it a name ("Funky Flair Boutique LLC").
_COMPANY_FORMS = frozenset({"inc", "llc", "ltd", "limited", "gmbh", "bv", "nv", "sa", "sl", "srl", "corp", "co", "ab"})


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def unwrap_byline(text: str) -> str:
    """
    The brand inside a marketplace byline ("Visit the Airmax Store" is Airmax), or the text as it is.

    :param text: The byline's text, or any brand as a page writes it.
    :return: The brand, without the phrase around it or a store word hyphened on.
    """
    name = _SPACES_RE.sub(" ", text).strip()
    for pattern in _BYLINE_RES:
        if m := pattern.match(name):
            name = m.group(1).strip(_TRIM)
            break
    if m := _HYPHEN_STORE_RE.match(name):
        name = m.group(1)
    return name.strip(_TRIM)


def plausible_brand(name: str) -> bool:
    """
    Whether a name can be a brand: no more than :data:`MAX_WORDS` words, no "visit" or "open", no early store word.

    A store word may only end the name, before a company form ("The Body Shop", "Funky Flair Boutique LLC").

    :param name: The name, unwrapped (:func:`unwrap_byline`).
    :return: ``False`` for a sentence or a link to a store ("De Airmax Store openen", "Visita la tienda de X").
    """
    words = _WORD_RE.findall(_fold(name))
    if not words or len(words) > MAX_WORDS:
        return False
    if any(w in _VISIT_WORDS for w in words):
        return False
    while len(words) > 1 and words[-1] in _COMPANY_FORMS:
        words.pop()
    return not any(w in _STORE_WORDS for w in words[:-1])


def clean_brand(text: str) -> str:
    """
    The brand a page names, ready to look up and to compare: unwrapped, or empty when it isn't a brand.

    :param text: The brand as the page writes it (a byline, a brand field).
    :return: The brand, at most 300 characters, or ``""``.
    """
    name = unwrap_byline(text)
    return name[:300] if plausible_brand(name) else ""
