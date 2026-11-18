"""
What the page's markup names, for the app and supplier traces when there is no raw HTML.

A shop's apps leave their names in the markup rather than in the text: a review importer's widget class
(``loox-reviews-widget``), a dropshipping app's data attribute (``data-dsers-variant-sync``), a pop-up's class
(``sales-pop``), a product photo still on the supplier's image CDN (``ae01.alicdn.com``).
:mod:`unfret.graph.skills.html_source_signals` matches its patterns against the raw HTML when the server fetched the
page; the browser extension never sends raw HTML, so these facts carry the same traces for it:

- ``markup_names``: the class names, ids and data-attribute names of the page's elements. Names only, never an
  attribute's value or any text, and only identifier-like names without a run of four digits (no customer or order
  numbers).
- ``external_refs``: the other sites the page's elements refer to: for a link, the site and the path (an
  ``aliexpress.com/item/…`` link); for anything else (an image, a frame, a stylesheet), the site alone. Never a query
  string or a fragment, never on a private seller's ad.

Areas that could hold the visitor's own details (forms and their fields, account menus, greetings, checkout;
:func:`unfret.facts.business.is_private`) give only their own element's names, never anything inside them. The
browser extension reads the same facts from the live page (``src/extract/markup.ts``, kept in parity).
"""

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from unfret.facts.business import is_address_marked, is_business, is_private
from unfret.facts.schema import PRIVATE_SELLER_PLATFORMS, PageFacts

MAX_NAMES = 500
MAX_REFS = 200
_MAX_REF_CHARS = 300
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,63}")
_DIGITS_RE = re.compile(r"[0-9]{4}")
# An absolute or protocol-relative address: the host (with any user and port) and the path, up to a query or fragment.
_URL_RE = re.compile(r"(?:https?:)?//([^/?#\s]+)([^?#\s]*)", re.IGNORECASE)
_PORT_RE = re.compile(r":[0-9]*$")
# Their own names count, nothing inside them is read: scripts and styles (script URLs are in ``script_srcs``),
# templates and noscript (markup a browser doesn't build, or holds as text).
_LEAF_TAGS = frozenset({"script", "style", "template", "noscript"})
_LINK_TAGS = frozenset({"a", "area"})


def _attr(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value is not None else ""


def _is_name(token: str) -> bool:
    return _NAME_RE.fullmatch(token) is not None and _DIGITS_RE.search(token) is None


def _names(tag: Tag) -> tuple[list[str], list[str], list[str]]:
    data = [str(name) for name in tag.attrs if str(name).startswith("data-")]
    ids = _attr(tag, "id").split()[:1] if tag.has_attr("id") else []
    return data, ids, _attr(tag, "class").split()


def host_and_path(value: str) -> tuple[str, str] | None:
    """
    The host (lower case, without user or port) and path of an absolute or protocol-relative address.

    Plain string matching rather than a URL parser, so the extension (``src/extract/markup.ts``) reads the same.

    :param value: An attribute's value.
    :return: ``(host, path)``, or ``None`` when it isn't such an address.
    """
    match = _URL_RE.match(value.strip())
    if match is None:
        return None
    host = _PORT_RE.sub("", match.group(1).rsplit("@", 1)[-1]).lower()
    return (host, match.group(2)) if host else None


def _refs(tag: Tag, own_host: str) -> list[str]:
    refs: list[str] = []
    for name in tag.attrs:
        value = _attr(tag, str(name))
        candidates = (
            [part.split()[0] for part in value.split(",") if part.split()] if name.endswith("srcset") else [value]
        )
        for candidate in candidates:
            found = host_and_path(candidate)
            if found is None or found[0] == own_host:
                continue
            host, path = found
            refs.append((host + path if tag.name in _LINK_TAGS else host)[:_MAX_REF_CHARS])
    return refs


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def extract(soup: BeautifulSoup, url: str, facts: PageFacts) -> None:
    """
    Fill ``markup_names`` and ``external_refs`` from the page.

    In document order, data-attribute names first, then ids, then class names (a page with a thousand utility
    classes still sends its apps' attributes and ids).

    :param soup: The parsed page (not modified).
    :param url: The page's URL: references to its own host are left out.
    :param facts: The facts to fill.
    """
    own = host_and_path(url)
    own_host = own[0] if own else ""
    refs_allowed = facts.platform not in PRIVATE_SELLER_PLATFORMS
    data: list[str] = []
    ids: list[str] = []
    classes: list[str] = []
    refs: list[str] = []
    stack: list[tuple[Any, bool]] = [(child, False) for child in reversed(soup.contents) if isinstance(child, Tag)]
    while stack:
        tag, business = stack.pop()
        inside = business or is_business(tag)
        tag_data, tag_ids, tag_classes = _names(tag)
        data += tag_data
        ids += tag_ids
        classes += tag_classes
        if tag.name in _LEAF_TAGS or is_private(tag) or (is_address_marked(tag) and not inside):
            continue
        if refs_allowed:
            refs += _refs(tag, own_host)
        stack.extend((child, inside) for child in reversed(tag.contents) if isinstance(child, Tag))
    facts.markup_names = [n for n in _dedupe(data + ids + classes) if _is_name(n)][:MAX_NAMES]
    facts.external_refs = _dedupe(refs)[:MAX_REFS]
