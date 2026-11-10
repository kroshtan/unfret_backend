"""
``PageFacts``: the structured facts about a product page that the analysis runs on.

The server extracts them from raw HTML (:mod:`unfret.facts.extract`); a browser extension can send them
directly, extracted from the rendered page, which also sees things raw HTML can't (computed styles, timers that
reset). Every list is capped, so a client-supplied object stays small.
"""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

Platform = Literal["amazon", "bol.com", "walmart", "shopify", "woocommerce", "generic"]

_SHORT = 300
_LONG = 2_000


class Review(BaseModel):
    """One review as shown on the page."""

    rating: float | None = Field(default=None, ge=0, le=5)
    posted: date | None = None
    title: str = Field(default="", max_length=_SHORT)
    body: str = Field(default="", max_length=_LONG)
    verified: bool | None = None


class Checkbox(BaseModel):
    """A checkbox on the page, e.g. an add-on in a cart or checkout form."""

    label: str = Field(default="", max_length=_SHORT)
    name: str = Field(default="", max_length=_SHORT)
    checked: bool = False


class PageFacts(BaseModel):
    """Structured facts about one product page. Absent facts are ``None`` or empty, never guessed."""

    platform: Platform = "generic"
    url: str = Field(default="", max_length=_LONG)
    title: str = Field(default="", max_length=_LONG)
    brand: str = Field(default="", max_length=_SHORT)
    seller: str = Field(default="", max_length=_SHORT)
    seller_rating: float | None = Field(default=None, ge=0, le=100, description="Positive share in percent.")
    ships_from: str = Field(default="", max_length=_SHORT)

    price: float | None = Field(default=None, ge=0)
    list_price: float | None = Field(default=None, ge=0, description="Struck-through 'was' / compare-at price.")
    currency: str = Field(default="", max_length=8)

    date_first_available: date | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    rating_count: int | None = Field(default=None, ge=0)
    rating_histogram: dict[int, float] = Field(
        default_factory=dict, description="Share of ratings (0–1) per star count 1–5."
    )
    reviews: list[Review] = Field(default_factory=list, max_length=100)
    variations: list[str] = Field(default_factory=list, max_length=100)

    json_ld: list[dict[str, Any]] = Field(default_factory=list, max_length=20, description="Product JSON-LD.")
    script_srcs: list[str] = Field(default_factory=list, max_length=300)
    meta: dict[str, str] = Field(default_factory=dict)
    hidden_text: list[str] = Field(default_factory=list, max_length=100)
    checkboxes: list[Checkbox] = Field(default_factory=list, max_length=100)
    timers: list[str] = Field(default_factory=list, max_length=50, description="Countdown elements or scripts.")
    fee_mentions: list[str] = Field(default_factory=list, max_length=50)
    vat_ids: list[str] = Field(default_factory=list, max_length=10)
    legal_mentions: list[str] = Field(
        default_factory=list, max_length=50, description="Imprint, company register and withdrawal-right text."
    )
    policy_links: dict[str, str] = Field(
        default_factory=dict, description="Kind (refund, terms, privacy, contact, imprint, shipping) to URL."
    )
    image_urls: list[str] = Field(default_factory=list, max_length=50)
