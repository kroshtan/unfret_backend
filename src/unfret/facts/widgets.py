"""
Reviews and reference prices from a shop's own markup, where its structured data (JSON-LD, microdata) leaves them out.

- WooCommerce's product summary: the struck "was" price (``<del>``) next to the price on offer (``<ins>``), the
  rating and count, and the reviews under the product (``#reviews .commentlist``).
- Review apps that write their data into the page as attributes: Judge.me (the review widget's average, count,
  star histogram and reviews), Loox and Stamped (their rating badge's average and count).
- eBay's struck reference price ("List price", "Was") in the item's price section.

Every reading here only fills a fact that is still empty: the structured data and the platform extractors come first.
The browser extension's ``src/extract/widgets.ts`` is a line-for-line port; the parity tests keep them in step.
"""

import re

from bs4 import BeautifulSoup, Tag

from unfret.facts._parse import parse_count, parse_date, parse_number, parse_price, parse_rating
from unfret.facts.schema import PageFacts, Review

_WS_RE = re.compile(r"\s+")
_MAX_REVIEWS = 100

# WooCommerce: the main product's summary, not a related product's price further down.
_WOO_SUMMARY = ".summary, .entry-summary"
_WOO_WAS = ".price del .amount"
_WOO_NOW = ".price ins .amount"
_WOO_RATING = ".woocommerce-product-rating .rating"
_WOO_COUNT = ".woocommerce-product-rating .count, .woocommerce-review-link .count"
_WOO_REVIEWS = "#reviews .commentlist li.review, #reviews .commentlist li.comment"

# Judge.me's review widget (its root carries the product's average and count) and its parts.
_JUDGEME_WIDGET = ".jdgm-rev-widg[data-average-rating]"
_JUDGEME_BAR = ".jdgm-histogram__row[data-rating][data-frequency]"
_JUDGEME_REVIEWS = ".jdgm-rev"
# Rating badges: Loox's and Stamped's.
_LOOX_BADGE = ".loox-rating[data-rating][data-raters]"
_STAMPED_BADGE = ".stamped-badge[data-rating]"
_STAMPED_CAPTION = ".stamped-badge-caption[data-reviews]"

# eBay's item price section: a struck reference price ("List price", "Was") next to the price.
_EBAY_WAS = ".x-price-section .ux-textspans--STRIKETHROUGH, .x-additional-info .ux-textspans--STRIKETHROUGH"


def extract(soup: BeautifulSoup, facts: PageFacts) -> None:
    """
    Fill the reviews and the reference price from the shop's own markup where they are still empty.

    :param soup: The parsed page.
    :param facts: The facts to complete; updated in place.
    """
    if facts.platform == "woocommerce":
        _woocommerce(soup, facts)
    if facts.platform == "ebay":
        _was_price(facts, soup.select(_EBAY_WAS))
    _judgeme(soup, facts)
    _badges(soup, facts)


def _woocommerce(soup: BeautifulSoup, facts: PageFacts) -> None:
    summary = soup.select_one(_WOO_SUMMARY)
    if summary is not None:
        if facts.price is None and (now := parse_price(_text(summary.select_one(_WOO_NOW))))[0] is not None:
            facts.price, facts.currency = now[0], facts.currency or now[1]
        _was_price(facts, summary.select(_WOO_WAS))
        _rating(facts, _text(summary.select_one(_WOO_RATING)), _text(summary.select_one(_WOO_COUNT)))
    if not facts.reviews:
        facts.reviews = [_woo_review(node) for node in soup.select(_WOO_REVIEWS)[:_MAX_REVIEWS]]


def _woo_review(node: Tag) -> Review:
    posted = node.select_one("time[datetime]")
    return Review(
        rating=parse_rating(_text(node.select_one(".star-rating .rating, .star-rating strong"))),
        posted=parse_date(str(posted.get("datetime", ""))) if isinstance(posted, Tag) else None,
        body=_text(node.select_one(".description"))[:2000],
        verified=node.select_one(".woocommerce-review__verified, .verified") is not None,
    )


def _judgeme(soup: BeautifulSoup, facts: PageFacts) -> None:
    widget = soup.select_one(_JUDGEME_WIDGET)
    if widget is None:
        return
    _rating(facts, str(widget.get("data-average-rating", "")), str(widget.get("data-number-of-reviews", "")))
    counts: dict[int, int] = {}
    for bar in widget.select(_JUDGEME_BAR):
        star, count = parse_count(str(bar.get("data-rating", ""))), parse_count(str(bar.get("data-frequency", "")))
        if star is not None and 1 <= star <= 5 and count is not None:
            counts[star] = count
    if not facts.rating_histogram and len(counts) >= 3 and (total := sum(counts.values())):
        facts.rating_histogram = {star: n / total for star, n in counts.items()}
    if not facts.reviews:
        facts.reviews = [_judgeme_review(node) for node in widget.select(_JUDGEME_REVIEWS)[:_MAX_REVIEWS]]


def _judgeme_review(node: Tag) -> Review:
    score = node.select_one(".jdgm-rev__rating[data-score]")
    posted = node.select_one(".jdgm-rev__timestamp[data-content]")
    return Review(
        rating=parse_rating(str(score.get("data-score", ""))) if isinstance(score, Tag) else None,
        posted=parse_date(str(posted.get("data-content", ""))) if isinstance(posted, Tag) else None,
        title=_text(node.select_one(".jdgm-rev__title"))[:300],
        body=_text(node.select_one(".jdgm-rev__body"))[:2000],
        verified=str(node.get("data-verified-buyer", "")).lower() == "true",
    )


def _badges(soup: BeautifulSoup, facts: PageFacts) -> None:
    if loox := soup.select_one(_LOOX_BADGE):
        _rating(facts, str(loox.get("data-rating", "")), str(loox.get("data-raters", "")))
    if stamped := soup.select_one(_STAMPED_BADGE):
        caption = soup.select_one(_STAMPED_CAPTION)
        count = str(caption.get("data-reviews", "")) if isinstance(caption, Tag) else ""
        _rating(facts, str(stamped.get("data-rating", "")), count)


def _rating(facts: PageFacts, rating: str, count: str) -> None:
    # Fill the average and the count when still empty; a badge without a count says too little to count as one.
    if facts.rating is None and (value := parse_rating(rating)) is not None and parse_count(count):
        facts.rating = value
    if facts.rating_count is None and (number := parse_count(count)) is not None:
        facts.rating_count = number


def _was_price(facts: PageFacts, tags: list[Tag]) -> None:
    # The first struck price above the price on offer.
    if facts.list_price is not None or facts.price is None:
        return
    for tag in tags:
        if (was := parse_number(_text(tag))) is not None and was > facts.price:
            facts.list_price = was
            return


def _text(tag: object) -> str:
    return _WS_RE.sub(" ", tag.get_text(" ")).strip() if isinstance(tag, Tag) else ""
