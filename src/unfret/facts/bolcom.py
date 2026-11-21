"""
bol.com product pages (the Dutch/Belgian marketplace).

bol.com ships Product JSON-LD, which the generic extractor already reads (not on every page); this adds what only its
own markup shows: the price on offer and its reference price ("Adviesprijs", "Meestal"), the seller (bol.com itself or
a third-party "partner"), the seller's rating, the review summary, the share of each star and the reviews on the page.
"""

import re

from bs4 import BeautifulSoup, NavigableString, Tag

from unfret.facts._parse import parse_count, parse_date, parse_number, parse_price, parse_rating
from unfret.facts.schema import PageFacts, Review

_SELLER_RE = re.compile(r"verkoop door\s+(.+?)(?:\s{2,}|$|\s+\d)", re.IGNORECASE)
# The seller block reads "Verkoop door Light Gallery NL beoordeeld met een 8,5": the label before the name and the
# rating after it are not part of it.
_SELLER_LABEL_RE = re.compile(r"^(?:verkoop|verkocht) door\s+", re.IGNORECASE)
_SELLER_TAIL_RE = re.compile(r"\s+(?:beoordeeld\b|met een\b|\d).*$", re.IGNORECASE | re.DOTALL)
_WS_RE = re.compile(r"\s+")
# The offer's price for a screen reader, the first on the page being the main offer's: "De prijs van dit product is 21
# euro en 99 cent. De adviesprijs is De adviesprijs is 29 euro en 99 cent." ("De meest getoonde prijs is …" for the
# "Meestal" reference). Read as text, so the same sentence in the page's script data doesn't count.
_PRICE_SENTENCE_RE = re.compile(
    r"De prijs van dit product is (\d+) euro(?: en (\d+) cent)?\.\s*"
    r"(?:De (?:adviesprijs|meest getoonde prijs) is (?:De adviesprijs is )?(\d+) euro(?: en (\d+) cent)?)?",
    re.IGNORECASE,
)
# The review summary for a screen reader: "Gemiddeld 4,7 van de 5 sterren uit 20 reviews".
_SUMMARY_RE = re.compile(r"gemiddeld\s+([\d.,]+)\s+van de 5 sterren uit\s+([\d.]+)\s+review", re.IGNORECASE)
# One bar of the star distribution: "7 klanten gaven een score van 4 sterren".
_BAR_RE = re.compile(r"(\d+)\s+klanten? gaven een score van\s+([1-5])\s+ster", re.IGNORECASE)
_STARS_RE = re.compile(r"klantbeoordeling", re.IGNORECASE)
_VERIFIED_RE = re.compile(r"heeft dit artikel gekocht", re.IGNORECASE)
_MAX_SELLER_BLOCK = 100
_SELLER_SCORE_RE = re.compile(r"beoordeeld met een\s+(\d{1,2}(?:,\d)?)\s+uit 10", re.IGNORECASE)
_SELLER_BLOCK_RE = re.compile(r"^\s*(?:verkoop|verkocht) door\b", re.IGNORECASE)


def extract(soup: BeautifulSoup, facts: PageFacts) -> None:
    """
    Fill bol.com-specific facts into ``facts`` (JSON-LD has already been read).

    :param soup: The parsed page.
    :param facts: The facts to complete; updated in place.
    """
    facts.title = _text(soup.select_one('[data-test="title"], h1.page-heading')) or facts.title
    facts.brand = _text(soup.select_one('[data-role="BRAND"], [data-test="brand"]'))[:300] or facts.brand
    _prices(soup, facts)
    seller = _text(soup.select_one('[data-test="seller-name"], .seller-name, [data-test="party-link"]'))
    seller = seller or _seller_block(soup)
    # The page text is a last resort: the offer's seller in the JSON-LD (read already) is cleaner.
    if not seller and not facts.seller and (m := _SELLER_RE.search(_text(soup.body) if soup.body else "")):
        seller = m.group(1)
    facts.seller = seller_name(seller)[:300] or facts.seller
    seller_rating = _text(soup.select_one('[data-test="seller-rating"], .seller-rating'))
    if not seller_rating and (m := _SELLER_SCORE_RE.search(seller)):
        seller_rating = m.group(1)
    if seller_rating:
        value = parse_number(seller_rating)
        # bol.com shows partner ratings on a 10-point scale ("9,1"); store them as a percentage.
        facts.seller_rating = value * 10 if value is not None and value <= 10 else value
    if (rating := parse_rating(_text(soup.select_one('[data-test="rating-suffix"], .rating-stars')))) is not None:
        facts.rating = facts.rating if facts.rating is not None else rating
    if (count := parse_count(_text(soup.select_one('[data-test="rating-count"], .rating-count')))) is not None:
        facts.rating_count = facts.rating_count if facts.rating_count is not None else count
    _reviews(soup, facts)


def _prices(soup: BeautifulSoup, facts: PageFacts) -> None:
    body = soup.body
    if body is not None and (m := _PRICE_SENTENCE_RE.search(_text(body))):
        # The main offer as the page shows it, which the JSON-LD's offer may not be (another seller's, or none).
        facts.price, facts.currency = _euros(m.group(1), m.group(2)), "EUR"
        if m.group(3) and (was := _euros(m.group(3), m.group(4))) > facts.price:
            facts.list_price = was
    elif facts.price is None and (price_tag := soup.select_one('[data-test="price"]')):
        price, currency = parse_price(_text(price_tag).replace(" ", ","))
        facts.price, facts.currency = price, currency or "EUR"


def _euros(euros: str, cents: str | None) -> float:
    return float(f"{int(euros)}.{int(cents or 0):02d}")


def _seller_block(soup: BeautifulSoup) -> str:
    # The offer's seller line: the first short element that starts with the label and names someone ("Verkoop
    # door<!-- --> bol", a partner's "Verkoop door GameResource beoordeeld met een 8,5 uit 10" for a screen reader).
    body = soup.body
    for string in body.find_all(string=_SELLER_BLOCK_RE) if body is not None else []:
        if isinstance(string, NavigableString) and isinstance(string.parent, Tag):
            block = _text(string.parent)
            if (
                len(block) <= _MAX_SELLER_BLOCK
                and seller_name(block)
                and not _SELLER_BLOCK_RE.match(seller_name(block))
            ):
                return block
    return ""


def _reviews(soup: BeautifulSoup, facts: PageFacts) -> None:
    summary = _text(soup.select_one('[data-testid="reviews-summary"]'))
    if m := _SUMMARY_RE.search(summary):
        facts.rating = facts.rating if facts.rating is not None else parse_rating(m.group(1))
        facts.rating_count = facts.rating_count if facts.rating_count is not None else parse_count(m.group(2))
    counts: dict[int, int] = {}
    for bar in soup.select('[data-testid="stars-distribution"] [aria-label]'):
        if m := _BAR_RE.search(str(bar.get("aria-label", ""))):
            counts[int(m.group(2))] = int(m.group(1))
    if len(counts) >= 3 and (total := sum(counts.values())):
        facts.rating_histogram = facts.rating_histogram or {star: n / total for star, n in counts.items()}
    reviews = [_review(node) for node in soup.select('[data-testid="review-details"]')[:100]]
    facts.reviews = facts.reviews or reviews


def _review(node: Tag) -> Review:
    # A review: its stars (an aria-label), its title and date as the first texts (no reviewer's name is kept), a
    # "Heeft dit artikel gekocht" line, and the text as the first paragraph.
    stars = next((t for t in node.select("[aria-label]") if _STARS_RE.match(str(t.get("aria-label", "")))), None)
    leaves = [t for span in node.find_all("span") if not span.find(True) and (t := _text(span))]
    posted = next((d for t in leaves if len(t) <= 30 and (d := parse_date(t))), None)
    title = next((t for t in leaves if parse_date(t) is None), "") if leaves and parse_date(leaves[0]) is None else ""
    return Review(
        rating=parse_rating(str(stars.get("aria-label", "")).partition(":")[2]) if isinstance(stars, Tag) else None,
        posted=posted,
        title=title[:300],
        body=_text(node.find("p"))[:2000],
        verified=any(_VERIFIED_RE.search(t) for t in leaves),
    )


def seller_name(text: str) -> str:
    """
    The seller's name from bol.com's seller block, without its label or rating.

    :param text: The block's text ("Verkoop door Light Gallery NL beoordeeld met een 8,5").
    :return: The name ("Light Gallery NL").
    """
    return _SELLER_TAIL_RE.sub("", _SELLER_LABEL_RE.sub("", text.strip())).strip()


def _text(tag: object) -> str:
    return _WS_RE.sub(" ", tag.get_text(" ")).strip() if isinstance(tag, Tag) else ""
