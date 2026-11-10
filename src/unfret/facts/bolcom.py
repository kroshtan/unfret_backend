"""
bol.com product pages (the Dutch/Belgian marketplace).

bol.com ships Product JSON-LD, which the generic extractor already reads; this adds what only its own markup
shows: the seller (bol.com itself or a third-party "partner"), the seller's rating, and the review summary.
"""

import re

from bs4 import BeautifulSoup, Tag

from unfret.facts._parse import parse_count, parse_number, parse_price, parse_rating
from unfret.facts.schema import PageFacts

_SELLER_RE = re.compile(r"verkoop door\s+(.+?)(?:\s{2,}|$|\s+\d)", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def extract(soup: BeautifulSoup, facts: PageFacts) -> None:
    """
    Fill bol.com-specific facts into ``facts`` (JSON-LD has already been read).

    :param soup: The parsed page.
    :param facts: The facts to complete; updated in place.
    """
    facts.title = _text(soup.select_one('[data-test="title"], h1.page-heading')) or facts.title
    facts.brand = _text(soup.select_one('[data-role="BRAND"], [data-test="brand"]'))[:300] or facts.brand
    if facts.price is None and (price_tag := soup.select_one('[data-test="price"]')):
        price, currency = parse_price(_text(price_tag).replace(" ", ","))
        facts.price, facts.currency = price, currency or "EUR"
    seller = _text(soup.select_one('[data-test="seller-name"], .seller-name, [data-test="party-link"]'))
    if not seller and (m := _SELLER_RE.search(_text(soup.body) if soup.body else "")):
        seller = m.group(1)
    facts.seller = seller[:300] or facts.seller
    seller_rating = _text(soup.select_one('[data-test="seller-rating"], .seller-rating'))
    if seller_rating:
        value = parse_number(seller_rating)
        # bol.com shows partner ratings on a 10-point scale ("9,1"); store them as a percentage.
        facts.seller_rating = value * 10 if value is not None and value <= 10 else value
    if (rating := parse_rating(_text(soup.select_one('[data-test="rating-suffix"], .rating-stars')))) is not None:
        facts.rating = facts.rating if facts.rating is not None else rating
    if (count := parse_count(_text(soup.select_one('[data-test="rating-count"], .rating-count')))) is not None:
        facts.rating_count = facts.rating_count if facts.rating_count is not None else count


def _text(tag: object) -> str:
    return _WS_RE.sub(" ", tag.get_text(" ")).strip() if isinstance(tag, Tag) else ""
