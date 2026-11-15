"""
Testimonials a service company publishes on its own site: the text, the name as printed, the rating and the date.

From the company's structured data (a ``review`` on its Organization/LocalBusiness block) when it has any, else from
the page: elements marked as one review or testimonial (a class such as ``testimonial`` or ``review-card``, or
Review microdata), the innermost ones only. Areas that could hold the visitor's own details are skipped, as for the
other business facts. The browser extension reads the same (``src/extract/testimonials.ts``, kept in parity).
"""

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from unfret.facts._parse import parse_date, parse_number, parse_rating
from unfret.facts.business import is_private
from unfret.facts.schema import Testimonial

MAX_TESTIMONIALS = 50
_MIN_TEXT = 10
# One class token naming a single testimonial or review ("testimonial", "review-card", "c-testimonial__item");
# plural containers ("testimonials", "reviews-list") don't match.
_ITEM_CLASS_RE = re.compile(
    r"^(?:[a-z0-9]+(?:-|_|__))*(?:testimonial|review|klantervaring|ervaring|recensie|beoordeling)"
    r"(?:(?:-|_|__)(?:item|card|block|entry|box|quote|slide|wrapper))?$"
)
_NAME_MARKS = ("author", "name", "naam", "auteur", "klant", "customer", "reviewer")
_DATE_MARKS = ("date", "datum")
_STARS_LABEL_RE = re.compile(
    r"([0-5](?:[.,]\d)?)\s*(?:/|out of|of|van|uit|von)\s*5|([0-5](?:[.,]\d)?)\s*(?:stars?|sterren|ster)(?![a-z])",
    re.IGNORECASE,
)
_NAME_LEAD = " \t\n—–-~·,"


def _attr(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value is not None else ""


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _text(tag: Tag) -> str:
    return _squash(tag.get_text(" "))


def is_item(tag: Tag) -> bool:
    """
    Whether an element is one testimonial or review.

    :param tag: The element.
    :return: ``True`` for a class token like ``testimonial`` or ``review-card``, or Review microdata.
    """
    if "schema.org/review" in _attr(tag, "itemtype").lower() or _attr(tag, "itemprop").lower() == "review":
        return True
    return any(_ITEM_CLASS_RE.match(token) for token in _attr(tag, "class").lower().split())


def _excluded(tag: Tag) -> bool:
    # Inside a form, an account menu or a checkout area (the visitor's), or a script.
    node: Tag | None = tag
    while node is not None and node.name != "[document]":
        if node.name in {"script", "style", "template"} or is_private(node):
            return True
        node = node.parent
    return False


def items(soup: BeautifulSoup) -> list[Tag]:
    """
    The innermost testimonial or review elements on the page, in document order.

    :param soup: The parsed page.
    :return: The elements (at most :data:`MAX_TESTIMONIALS`).
    """
    found: list[Tag] = []
    for tag in soup.find_all(True):
        if not is_item(tag) or any(is_item(d) for d in tag.find_all(True)) or _excluded(tag):
            continue
        found.append(tag)
        if len(found) >= MAX_TESTIMONIALS:
            break
    return found


def _first(tag: Tag, test: Any) -> Tag | None:
    for d in tag.find_all(True):
        if test(d):
            return d
    return None


def _marked(tag: Tag, words: tuple[str, ...]) -> bool:
    marks = _attr(tag, "class").lower()
    return any(word in marks for word in words)


def _body(item: Tag) -> str:
    for test in (
        lambda d: _attr(d, "itemprop") == "reviewBody",
        lambda d: d.name == "blockquote",
        lambda d: d.name == "q",
    ):
        if (found := _first(item, test)) is not None:
            return _text(found)
    best = ""
    for p in item.find_all("p"):
        text = _text(p)
        if len(text) > len(best):
            best = text
    return best or _text(item)


def _name(item: Tag) -> str:
    author = _first(item, lambda d: _attr(d, "itemprop") == "author")
    if author is not None:
        inner = _first(author, lambda d: _attr(d, "itemprop") == "name")
        name = _text(inner if inner is not None else author)
    else:
        found = _first(item, lambda d: d.name == "cite") or _first(item, lambda d: _marked(d, _NAME_MARKS))
        name = _text(found) if found is not None else ""
    return name.strip(_NAME_LEAD)[:100]


def _rating(item: Tag) -> float | None:
    value = _first(item, lambda d: _attr(d, "itemprop") == "ratingValue")
    if value is not None:
        return parse_rating(_attr(value, "content") or _text(value))
    for node in [item, *item.find_all(True)]:
        if node.has_attr("data-rating"):
            return parse_rating(_attr(node, "data-rating"))
    for node in [item, *item.find_all(True)]:
        for name in ("aria-label", "title"):
            if m := _STARS_LABEL_RE.search(_attr(node, name)):
                return parse_rating(m.group(1) or m.group(2))
    stars = _text(item).count("★")
    return float(stars) if 1 <= stars <= 5 else None


def _date(item: Tag) -> Any:
    timed = _first(item, lambda d: d.name == "time" and d.has_attr("datetime"))
    if timed is not None:
        return parse_date(_attr(timed, "datetime"))
    published = _first(item, lambda d: _attr(d, "itemprop") == "datePublished")
    if published is not None:
        return parse_date(_attr(published, "content") or _text(published))
    found = _first(item, lambda d: d.name == "time") or _first(item, lambda d: _marked(d, _DATE_MARKS))
    return parse_date(_text(found)) if found is not None else None


def from_html(soup: BeautifulSoup) -> list[Testimonial]:
    """
    The testimonials marked up on the page.

    :param soup: The parsed page.
    :return: Text (at least ten characters), name as printed, rating and date.
    """
    out: list[Testimonial] = []
    for item in items(soup):
        text = _body(item)
        if len(text) < _MIN_TEXT:
            continue
        out.append(Testimonial(text=text[:2000], name=_name(item), rating=_rating(item), posted=_date(item)))
    return out


def _ld_rating(value: object) -> float | None:
    node = value.get("ratingValue") if isinstance(value, dict) else value
    if isinstance(node, bool) or not isinstance(node, int | float | str):
        return None
    number = float(node) if isinstance(node, int | float) else parse_number(node)
    return number if number is not None and 0 <= number <= 5 else None


def _ld_author(value: object) -> str:
    author = value[0] if isinstance(value, list) and value else value
    if isinstance(author, dict):
        author = author.get("name")
    return _squash(author)[:100] if isinstance(author, str) else ""


def from_structured_data(orgs: list[dict[str, Any]]) -> list[Testimonial]:
    """
    The reviews in the company's Organization/LocalBusiness structured data.

    :param orgs: The Organization nodes, untrimmed.
    :return: The testimonials.
    """
    out: list[Testimonial] = []
    for org in orgs:
        reviews = org.get("review")
        for review in reviews if isinstance(reviews, list) else [reviews]:
            if not isinstance(review, dict):
                continue
            body = review.get("reviewBody") or review.get("description")
            text = _squash(body) if isinstance(body, str) else ""
            if len(text) < _MIN_TEXT:
                continue
            published = review.get("datePublished")
            out.append(
                Testimonial(
                    text=text[:2000],
                    name=_ld_author(review.get("author")),
                    rating=_ld_rating(review.get("reviewRating")),
                    posted=parse_date(published) if isinstance(published, str) else None,
                )
            )
    return out[:MAX_TESTIMONIALS]


def extract(soup: BeautifulSoup, orgs: list[dict[str, Any]]) -> list[Testimonial]:
    """
    The site's testimonials: its structured data's reviews when it has any, else the ones marked up on the page.

    Not both: a site that has both usually shows the same testimonials twice, which would read as copies.

    :param soup: The parsed page.
    :param orgs: The Organization nodes, untrimmed.
    :return: The testimonials.
    """
    return from_structured_data(orgs) or from_html(soup)[:MAX_TESTIMONIALS]
