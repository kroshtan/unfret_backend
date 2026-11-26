"""
``PageFacts``: the structured facts about a page that the analysis runs on.

The server extracts them from raw HTML (:mod:`unfret.facts.extract`); a browser extension can send them
directly, extracted from the rendered page, which also sees things raw HTML can't (computed styles, timers that
reset). Every list is capped, so a client-supplied object stays small.

Product pages came first; the business fields (contacts, identity statements, payment methods) serve shops and
service companies alike, and :class:`ListingDetails` and :class:`SellerProfile` describe peer-to-peer ads and
platform profiles. Nothing about the person viewing the page belongs here: the extractors skip forms, account menus
and checkout or delivery areas. A private seller's name is never kept, and their contact details only for the
check itself.
"""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Platform = Literal[
    # Product marketplaces and shop systems
    "amazon",
    "bol.com",
    "walmart",
    "shopify",
    "woocommerce",
    # Peer-to-peer marketplaces (eBay also has business sellers)
    "ebay",
    "marktplaats",
    "2dehands",
    "vinted",
    "craigslist",
    "facebook",
    # Platforms where service providers have a profile
    "werkspot",
    "thumbtack",
    "angi",
    "homeadvisor",
    "checkatrade",
    "taskrabbit",
    "bark",
    "houzz",
    "myhammer",
    "homeguide",
    "generic",
]
# Platforms where the seller is usually a private person: their name is never kept.
P2P_PLATFORMS: frozenset[str] = frozenset({"marktplaats", "2dehands", "vinted", "craigslist", "facebook"})
# Platforms where the seller may be a private person: only product IDs and payment methods are read as business
# facts there (the rest, contact areas and links, would describe the platform or name the seller).
PRIVATE_SELLER_PLATFORMS: frozenset[str] = P2P_PLATFORMS | {"ebay"}
PhoneKind = Literal["mobile", "local", "national", "toll_free", "premium", "unknown"]
EmailKind = Literal["own_domain", "webmail", "other"]
SellerType = Literal["", "business", "private"]
Delivery = Literal["shipping", "pickup"]
# What a variant picker's dimension varies, from its name in any language (unfret.facts.variants).
VariantKind = Literal["size", "colour", "count", "pattern", "material", "flavour", "style", "other"]
# Where the options were read: a platform's own variant data, a shop system's product JSON, schema.org's
# ProductGroup, or the page's own picker (select or radio buttons).
VariantSource = Literal["amazon", "walmart", "shopify", "woocommerce", "schema_org", "picker"]
# How the option the headline price belongs to is known: the page has it selected, the page shows a "from" price or
# a price range (the cheapest option), or the headline price is that option's price.
HeadlineBasis = Literal["", "selected", "from", "price"]

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


class Phone(BaseModel):
    """A phone number the business publishes (``tel:`` links, contact areas, structured data)."""

    number: str = Field(default="", max_length=20, description="+ and digits when the country is known, else digits.")
    kind: PhoneKind = Field(
        default="unknown",
        description="mobile, local (geographic), national (non-geographic), toll_free, premium (NL and US numbers).",
    )
    country: str = Field(default="", max_length=2, description="ISO country code from the number, when known.")


class Email(BaseModel):
    """An e-mail address the business publishes."""

    address: str = Field(default="", max_length=254)
    kind: EmailKind = Field(
        default="other", description="own_domain (same domain as the page), webmail (Gmail, Hotmail, …) or other."
    )


class Licence(BaseModel):
    """A (US) licence number the page states, with its state when given ("CA Lic. #1234567")."""

    number: str = Field(default="", max_length=40)
    state: str = Field(default="", max_length=2)


class Link(BaseModel):
    """A link to another page on the same site."""

    text: str = Field(default="", max_length=_SHORT)
    url: str = Field(default="", max_length=500)


class Testimonial(BaseModel):
    """
    A testimonial a service company publishes on its own site: its own content, read as it is shown.

    The name is only what the site prints next to it ("Jan, Utrecht"); used during the check only.
    """

    text: str = Field(default="", max_length=_LONG)
    name: str = Field(default="", max_length=100)
    rating: float | None = Field(default=None, ge=0, le=5)
    posted: date | None = None


class SellerProfile(BaseModel):
    """
    The seller (peer-to-peer) or provider (service platform) as the platform shows them: never their name.

    Used during the check only.
    """

    seller_type: SellerType = Field(default="", description="business or private, as the platform labels them.")
    member_since: date | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    rating_count: int | None = Field(default=None, ge=0)
    badges: list[str] = Field(default_factory=list, max_length=20, description="Verified ID, Top seller, …")
    other_listings: int | None = Field(default=None, ge=0)
    response_time: str = Field(default="", max_length=100)
    response_rate: float | None = Field(default=None, ge=0, le=100, description="In percent.")
    jobs_completed: int | None = Field(default=None, ge=0)
    years_in_business: int | None = Field(default=None, ge=0, le=200)


class ListingDetails(BaseModel):
    """A peer-to-peer ad: the item and the deal as the platform shows them (price: ``PageFacts.price``)."""

    negotiable: bool | None = None
    bidding: bool | None = None
    condition: str = Field(default="", max_length=100)
    category: str = Field(default="", max_length=_SHORT)
    listed: date | None = None
    location: str = Field(default="", max_length=_SHORT)
    delivery: list[Delivery] = Field(default_factory=list, max_length=2)
    payment_protection: bool | None = Field(
        default=None, description="The platform's own payment or buyer protection."
    )
    contact_methods: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Ways to reach the seller the ad text mentions: whatsapp, telegram, phone, email.",
    )
    payment_requests: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Payment the ad text asks for: bank_transfer, payment_link, deposit, gift_card, crypto, "
        "money_transfer, payment_app.",
    )
    description: str = Field(
        default="",
        max_length=5_000,
        description="The ad's own text as the seller wrote it (not the platform's), for the check only.",
    )


class VariantDimension(BaseModel):
    """One dimension of a variant picker ("Size", "Farbe", "Stijlnaam") and its values in the page's order."""

    name: str = Field(default="", max_length=100, description="As the page labels it; '' when it doesn't.")
    kind: VariantKind = Field(default="other", description="What it varies, from its name (size, colour, …).")
    values: list[Annotated[str, Field(max_length=200)]] = Field(default_factory=list, max_length=100)


class VariantOption(BaseModel):
    """One option a shopper can pick: one value per dimension, and its price when the page gives it."""

    label: str = Field(default="", max_length=300, description="The option as the page names it ('Wit / 40').")
    values: list[Annotated[str, Field(max_length=200)]] = Field(
        default_factory=list, max_length=5, description="Its value in each dimension, in the dimensions' order."
    )
    price: float | None = Field(default=None, ge=0, description="In the page's currency (PageFacts.currency).")
    available: bool | None = None
    selected: bool = False


class Variants(BaseModel):
    """
    The options a listing groups under one page (sizes, colours, styles, amounts), on any platform.

    One rating and one review pool cover every option, and a "from" price is the cheapest option's: a listing that
    groups different products as "sizes" shows them as one product.
    """

    source: VariantSource = "picker"
    dimensions: list[VariantDimension] = Field(default_factory=list, max_length=5)
    options: list[VariantOption] = Field(default_factory=list, max_length=100)
    headline: int | None = Field(
        default=None, ge=0, le=99, description="Index in options of the option the page's price belongs to."
    )
    headline_basis: HeadlineBasis = ""
    from_price: bool = Field(default=False, description="The page shows its price as a 'from' price or a range.")


class PageFacts(BaseModel):
    """Structured facts about one page. Absent facts are ``None`` or empty, never guessed."""

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
    variants: Variants | None = Field(
        default=None, description="The variant picker's options, prices and the headline price's option."
    )

    json_ld: list[dict[str, Any]] = Field(default_factory=list, max_length=20, description="Product JSON-LD.")
    script_srcs: list[str] = Field(default_factory=list, max_length=300)
    meta: dict[str, str] = Field(default_factory=dict)
    markup_names: list[str] = Field(
        default_factory=list,
        max_length=500,
        description="Class names, ids and data-attribute names in the page's markup (names only, never values or "
        "text): the traces of a shop's apps when there is no raw HTML (unfret.facts.markup).",
    )
    external_refs: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="The other sites the page's elements refer to: host and path for a link, the host alone for "
        "anything else; never a query string (unfret.facts.markup).",
    )
    hidden_text: list[str] = Field(default_factory=list, max_length=100)
    checkboxes: list[Checkbox] = Field(default_factory=list, max_length=100)
    timers: list[str] = Field(default_factory=list, max_length=50, description="Countdown elements or scripts.")
    fee_mentions: list[str] = Field(default_factory=list, max_length=50)
    stock_left: int | None = Field(
        default=None,
        ge=1,
        le=999,
        description='The count an "only N left" stock claim on the page names (the first one), if any.',
    )
    vat_ids: list[str] = Field(default_factory=list, max_length=10)
    legal_mentions: list[str] = Field(
        default_factory=list, max_length=50, description="Imprint, company register and withdrawal-right text."
    )
    copyright_lines: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Copyright lines ('© 2026 Acme Outdoor LLC. All rights reserved.'): the keyword and what follows.",
    )
    policy_links: dict[str, str] = Field(
        default_factory=dict, description="Kind (refund, terms, privacy, contact, about, imprint, shipping) to URL."
    )
    image_urls: list[str] = Field(default_factory=list, max_length=50)

    # ── Business facts: what a shop or service company publishes about itself ────────────────────────────────
    product_ids: dict[str, str] = Field(
        default_factory=dict, description="Public product IDs by kind: gtin, gtin8/12/13/14, mpn, asin."
    )
    payment_methods: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="Payment methods the page mentions (paypal, ideal, crypto, …).",
    )
    phones: list[Phone] = Field(default_factory=list, max_length=20)
    emails: list[Email] = Field(default_factory=list, max_length=20)
    addresses: list[str] = Field(
        default_factory=list, max_length=20, description="Postal addresses from structured data and contact areas."
    )
    founded_claims: list[str] = Field(
        default_factory=list, max_length=10, description='"Since 1998", "opgericht in 2004", a foundingDate.'
    )
    business_name: str = Field(default="", max_length=_SHORT, description="From Organization/LocalBusiness data.")
    legal_form: str = Field(default="", max_length=20, description="BV, VOF, LLC, Inc, GmbH, … as stated.")
    kvk_numbers: list[str] = Field(default_factory=list, max_length=10, description="Dutch Chamber of Commerce.")
    licences: list[Licence] = Field(default_factory=list, max_length=10)
    certifications: list[str] = Field(
        default_factory=list, max_length=20, description="Licence, insurance and certification claims (snippets)."
    )
    cities_served: list[str] = Field(default_factory=list, max_length=100)
    internal_links: list[Link] = Field(default_factory=list, max_length=100)
    pricing_terms: list[str] = Field(
        default_factory=list, max_length=20, description="Call-out fees, quotes, deposits, hourly rates (snippets)."
    )
    promises: list[str] = Field(
        default_factory=list, max_length=20, description='"24/7", "within 30 minutes", same-day (snippets).'
    )
    testimonials: list[Testimonial] = Field(
        default_factory=list, max_length=50, description="Testimonials on a service company's own site."
    )
    organization_ld: list[dict[str, Any]] = Field(
        default_factory=list, max_length=10, description="Organization/LocalBusiness JSON-LD, trimmed."
    )

    # ── Peer-to-peer ads and platform profiles (filled by the platform extractors) ──────────────────────────
    seller_profile: SellerProfile | None = None
    listing: ListingDetails | None = None
