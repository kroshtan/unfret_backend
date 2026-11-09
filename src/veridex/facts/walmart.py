"""
Walmart.com product pages.

Walmart renders with Next.js and ships the whole product in the ``__NEXT_DATA__`` script: price and "was" price,
the seller (Walmart itself or a marketplace seller), the rating histogram and a page of reviews. Its Product JSON-LD
is less reliable (the first offer can be a protection plan), so these facts override what JSON-LD gave.
"""

import json
from datetime import date
from typing import Any

from bs4 import BeautifulSoup

from veridex.facts._parse import parse_date
from veridex.facts.schema import PageFacts, Review

_STARS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def extract(soup: BeautifulSoup, facts: PageFacts) -> None:
    """
    Fill Walmart-specific facts into ``facts`` (JSON-LD has already been read).

    :param soup: The parsed page.
    :param facts: The facts to complete; updated in place.
    """
    data = _initial_data(soup)
    product = _dict(data.get("product"))
    if not product:
        return
    facts.title = str(product.get("name") or facts.title)[:2000]
    facts.brand = str(product.get("brand") or facts.brand)[:300]
    price_info = _dict(product.get("priceInfo"))
    if (price := _amount(price_info.get("currentPrice"))) is not None:
        facts.price = price
        facts.currency = str(_dict(price_info.get("currentPrice")).get("currencyUnit") or "USD")[:8]
    was = _amount(price_info.get("wasPrice")) or _amount(price_info.get("listPrice"))
    if was is not None and facts.price is not None and was > facts.price:
        facts.list_price = was
    seller = str(product.get("sellerDisplayName") or product.get("sellerName") or "")
    facts.seller = seller[:300] or facts.seller
    if product.get("fulfillmentType") in {"FC", "WFS"}:
        facts.ships_from = "Walmart"
    if isinstance(rating := product.get("averageRating"), int | float) and 0 <= rating <= 5:
        facts.rating = float(rating)
    if isinstance(count := product.get("numberOfReviews"), int) and count >= 0:
        facts.rating_count = count
    reviews = _dict(data.get("reviews"))
    facts.rating_histogram = _histogram(reviews) or facts.rating_histogram
    facts.reviews = _reviews(reviews) or facts.reviews
    images = [str(i.get("url")) for i in _dict(product.get("imageInfo")).get("allImages") or [] if _dict(i).get("url")]
    if images:
        facts.image_urls = [*images, *[u for u in facts.image_urls if u not in images]][:50]


def _initial_data(soup: BeautifulSoup) -> dict[str, Any]:
    script = soup.find("script", id="__NEXT_DATA__")
    try:
        data = json.loads(script.string or "") if script is not None else {}
    except ValueError:
        return {}
    return _dict(_dict(_dict(_dict(data.get("props")).get("pageProps")).get("initialData")).get("data"))


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _amount(node: object) -> float | None:
    value = _dict(node).get("price")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0 else None


def _histogram(reviews: dict[str, Any]) -> dict[int, float]:
    counts = {star: reviews.get(f"ratingValue{name}Count") for star, name in _STARS.items()}
    valid = {star: n for star, n in counts.items() if isinstance(n, int) and n >= 0}
    total = sum(valid.values())
    if len(valid) < 5 or total == 0:
        return {}
    return {star: round(n / total, 4) for star, n in valid.items()}


def _reviews(reviews: dict[str, Any]) -> list[Review]:
    out: list[Review] = []
    for node in reviews.get("customerReviews") or []:
        if not isinstance(node, dict):
            continue
        rating = node.get("rating")
        badges = [_dict(b).get("id") for b in node.get("badges") or []]
        out.append(
            Review(
                rating=float(rating) if isinstance(rating, int | float) and 0 <= rating <= 5 else None,
                posted=_us_date(str(node.get("reviewSubmissionTime") or "")),
                title=str(node.get("reviewTitle") or "")[:300],
                body=str(node.get("reviewText") or "")[:2000],
                verified="VerifiedPurchaser" in badges,
            )
        )
    return out[:100]


def _us_date(text: str) -> date | None:
    # Walmart writes review dates as M/D/YYYY.
    parts = text.split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        month, day, year = (int(p) for p in parts)
        return parse_date(f"{year:04d}-{month:02d}-{day:02d}")
    return parse_date(text)
