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
# The seller block reads "Verkoop door Light Gallery NL beoordeeld met een 8,5": the label before the name and the
# rating after it are not part of it.
_SELLER_LABEL_RE = re.compile(r"^(?:verkoop|verkocht) door\s+", re.IGNORECASE)
_SELLER_TAIL_RE = re.compile(r"\s+(?:beoordeeld\b|met een\b|\d).*$", re.IGNORECASE | re.DOTALL)
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
    # The page text is a last resort: the offer's seller in the JSON-LD (read already) is cleaner.
    if not seller and not facts.seller and (m := _SELLER_RE.search(_text(soup.body) if soup.body else "")):
        seller = m.group(1)
    facts.seller = seller_name(seller)[:300] or facts.seller
    seller_rating = _text(soup.select_one('[data-test="seller-rating"], .seller-rating'))
    if seller_rating:
        value = parse_number(seller_rating)
        # bol.com shows partner ratings on a 10-point scale ("9,1"); store them as a percentage.
        facts.seller_rating = value * 10 if value is not None and value <= 10 else value
    if (rating := parse_rating(_text(soup.select_one('[data-test="rating-suffix"], .rating-stars')))) is not None:
        facts.rating = facts.rating if facts.rating is not None else rating
    if (count := parse_count(_text(soup.select_one('[data-test="rating-count"], .rating-count')))) is not None:
        facts.rating_count = facts.rating_count if facts.rating_count is not None else count


def seller_name(text: str) -> str:
    """
    The seller's name from bol.com's seller block, without its label or rating.

    :param text: The block's text ("Verkoop door Light Gallery NL beoordeeld met een 8,5").
    :return: The name ("Light Gallery NL").
    """
    return _SELLER_TAIL_RE.sub("", _SELLER_LABEL_RE.sub("", text.strip())).strip()


def _text(tag: object) -> str:
    return _WS_RE.sub(" ", tag.get_text(" ")).strip() if isinstance(tag, Tag) else ""
