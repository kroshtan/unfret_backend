"""
Amazon Reviews 2023 (McAuley Lab, UCSD): marketplace listings with weak rule labels, rendered as product pages.

The dataset has product metadata and timestamped reviews but no fraud labels. Metadata and reviews are streamed
(never stored whole) for a few categories rich in both generic junk and established brands; per product we keep
only aggregates and a small review sample. Strict rules label the confident extremes junk or legit, everything
else stays unlabelled, and each selected product is rendered into Amazon-like HTML (``source="synthetic"``).

Usage: ``uv run python -m evals.sources.amazon2023 [--categories ...] [--per-label N]``.
"""

import argparse
import contextlib
import html
import json
import random
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from evals.corpus import Corpus, LabelRecord, page_id
from evals.features import (
    duplicate_share,
    early_review_burst,
    generated_brand_score,
    keyword_stuffed,
    rating_shape,
)
from evals.sources._http import USER_AGENT

logger = structlog.get_logger("veridex.evals")

DATASET = "amazon-reviews-2023"
BASE = "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/resolve/main/raw"
DEFAULT_CATEGORIES = ("All_Beauty", "Amazon_Fashion", "Appliances")
_MIN_RATINGS = 30
_REVIEW_SAMPLE = 14
_DICTIONARY_PATH = Path("/usr/share/dict/words")

# Established brands seen in these categories. A legit label also needs clean review signals, so a known brand
# alone is not enough.
KNOWN_BRANDS: frozenset[str] = frozenset(
    b.lower()
    for b in (
        "L'Oreal Paris",
        "Neutrogena",
        "Maybelline",
        "Maybelline New York",
        "Revlon",
        "Olay",
        "Dove",
        "Nivea",
        "CeraVe",
        "Cetaphil",
        "Aveeno",
        "Garnier",
        "Burt's Bees",
        "Clinique",
        "Estee Lauder",
        "Philips",
        "Philips Norelco",
        "Braun",
        "Remington",
        "Conair",
        "Panasonic",
        "Wahl",
        "Gillette",
        "Schick",
        "Crest",
        "Oral-B",
        "Colgate",
        "Pantene",
        "Head & Shoulders",
        "TRESemme",
        "John Frieda",
        "Paul Mitchell",
        "Redken",
        "Hanes",
        "Levi's",
        "Carhartt",
        "Columbia",
        "Calvin Klein",
        "Tommy Hilfiger",
        "Fossil",
        "Timex",
        "Casio",
        "Ray-Ban",
        "Under Armour",
        "adidas",
        "Nike",
        "PUMA",
        "Champion",
        "Fruit of the Loom",
        "Gildan",
        "Wrangler",
        "Lee",
        "Dickies",
        "Skechers",
        "Crocs",
        "New Balance",
        "Reebok",
        "Converse",
        "Vans",
        "Clarks",
        "Merrell",
        "Speedo",
        "Jockey",
        "Bali",
        "Maidenform",
        "Playtex",
        "Vanity Fair",
        "Haggar",
        "Dockers",
        "Russell",
        "GE",
        "Whirlpool",
        "Frigidaire",
        "Samsung",
        "LG",
        "Bosch",
        "KitchenAid",
        "Cuisinart",
        "Hamilton Beach",
        "BLACK+DECKER",
        "Keurig",
        "Ninja",
        "Instant Pot",
        "Breville",
        "Oster",
        "Honeywell",
        "Dyson",
        "BISSELL",
        "Hoover",
        "Shark",
        "iRobot",
        "Maytag",
        "Kenmore",
        "Electrolux",
        "Amana",
        "Everydrop",
        "EveryDrop by Whirlpool",
        "Brita",
        "PUR",
        "De'Longhi",
        "Hamilton Beach Brands",
        "Presto",
        "Proctor Silex",
        "Crock-Pot",
        "Mr. Coffee",
    )
)


@dataclass
class Product:
    """Metadata and review aggregates for one product."""

    asin: str
    category: str
    title: str
    store: str
    price: float | None
    average_rating: float
    rating_number: int
    features: list[str]
    description: list[str]
    details: dict[str, str]
    image: str
    histogram: list[int] = field(default_factory=lambda: [0, 0, 0, 0, 0])
    timestamps: list[int] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    sample: list[dict[str, object]] = field(default_factory=list)
    seen: int = 0


def stream_jsonl(url: str) -> Iterator[dict[str, object]]:
    """
    Stream a remote JSON-lines file record by record without storing it.

    :param url: The file URL.
    :yield: Each parsed record; malformed lines are skipped.
    """
    with httpx.stream("GET", url, follow_redirects=True, timeout=120, headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                with contextlib.suppress(ValueError):
                    yield json.loads(line)


def _price(value: object) -> float | None:
    try:
        return float(str(value).replace("$", "").replace(",", "")) if value not in (None, "", "None") else None
    except ValueError:
        return None


def _strings(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def load_products(category: str, store_counts: Counter[str]) -> dict[str, Product]:
    """
    Stream a category's metadata, keeping products with enough ratings to judge.

    :param category: Dataset category name, e.g. ``"All_Beauty"``.
    :param store_counts: Updated with the number of listings per store, across all products.
    :return: Kept products keyed by parent ASIN.
    """
    products: dict[str, Product] = {}
    for row in stream_jsonl(f"{BASE}/meta_categories/meta_{category}.jsonl"):
        store = str(row.get("store") or "").strip()
        store_counts[store.lower()] += 1
        rating_number = int(row.get("rating_number") or 0)  # type: ignore[call-overload]
        title = str(row.get("title") or "")
        if rating_number < _MIN_RATINGS or not store or not title:
            continue
        images = row.get("images") or []
        image = ""
        if isinstance(images, list) and images and isinstance(images[0], dict):
            image = str(images[0].get("large") or images[0].get("hi_res") or "")
        raw_details = row.get("details")
        details: dict[object, object] = raw_details if isinstance(raw_details, dict) else {}
        asin = str(row["parent_asin"])
        products[asin] = Product(
            asin=asin,
            category=category,
            title=title,
            store=store,
            price=_price(row.get("price")),
            average_rating=float(row.get("average_rating") or 0),  # type: ignore[arg-type]
            rating_number=rating_number,
            features=_strings(row.get("features"))[:6],
            description=_strings(row.get("description"))[:3],
            details={str(k): str(v) for k, v in details.items()},
            image=image,
        )
    return products


def attach_reviews(category: str, products: dict[str, Product], rng: random.Random) -> None:
    """
    Stream a category's reviews into per-product aggregates and a small reservoir sample.

    :param category: Dataset category name.
    :param products: Products from :func:`load_products`, updated in place.
    :param rng: Random source for the reservoir sample.
    """
    for row in stream_jsonl(f"{BASE}/review_categories/{category}.jsonl"):
        product = products.get(str(row.get("parent_asin")))
        if product is None:
            continue
        rating = int(float(row.get("rating") or 0))  # type: ignore[arg-type]
        if 1 <= rating <= 5:
            product.histogram[rating - 1] += 1
        product.timestamps.append(int(row.get("timestamp") or 0))  # type: ignore[call-overload]
        text = str(row.get("text") or "")
        if len(product.texts) < 400:
            product.texts.append(text)
        review = {
            "rating": rating,
            "title": str(row.get("title") or "")[:120],
            "text": text[:500],
            "timestamp": row.get("timestamp"),
            "verified": bool(row.get("verified_purchase")),
        }
        product.seen += 1
        if len(product.sample) < _REVIEW_SAMPLE:
            product.sample.append(review)
        else:
            slot = rng.randrange(product.seen)
            if slot < _REVIEW_SAMPLE:
                product.sample[slot] = review


@dataclass(frozen=True)
class Assessment:
    """Rule features and the resulting weak label for one product."""

    label: str | None
    confidence: float
    features: dict[str, object]


def assess(product: Product, store_counts: Counter[str], dictionary: frozenset[str]) -> Assessment:
    """
    Apply the strict weak-labelling rules.

    Junk needs a generated-looking brand, a low price and at least two independent manipulation signals. Legit needs
    an established brand and clean review signals. Everything in between stays unlabelled.

    :param product: The product with its review aggregates.
    :param store_counts: Listings per store across the streamed categories.
    :param dictionary: English words, for the brand check.
    :return: The assessment.
    """
    shape = rating_shape(product.histogram)
    dup = duplicate_share(product.texts)
    burst = early_review_burst(product.timestamps)
    brand_score = generated_brand_score(product.store, dictionary, KNOWN_BRANDS)
    stuffed = keyword_stuffed(product.title)
    signals = {
        "keyword_stuffed": stuffed,
        "duplicate_reviews": dup >= 0.08,
        "j_shaped": shape.j_shaped,
        "early_burst": burst >= 150,
    }
    features: dict[str, object] = {
        "generated_brand_score": round(brand_score, 2),
        "store_listings": store_counts.get(product.store.lower(), 0),
        "duplicate_share": round(dup, 3),
        "rating_one": round(shape.one, 3),
        "rating_middle": round(shape.middle, 3),
        "rating_five": round(shape.five, 3),
        "early_burst_180d": burst,
        "reviews_streamed": product.seen,
        **signals,
    }
    cheap = product.price is not None and product.price < 30
    junk_hits = sum(signals.values())
    if brand_score >= 0.6 and cheap and junk_hits >= 2:
        return Assessment("junk", min(0.6 + 0.1 * junk_hits, 0.9), features)
    known = product.store.lower() in KNOWN_BRANDS
    if known and product.rating_number >= 50 and dup < 0.04 and not shape.j_shaped and not stuffed:
        return Assessment("legit", 0.75, features)
    return Assessment(None, 0.0, features)


def _date(timestamp_ms: object) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp_ms) / 1000, UTC).strftime("%B %-d, %Y")  # type: ignore[call-overload]
    except TypeError, ValueError, OverflowError:
        return ""


def render(product: Product) -> str:
    """
    Render a product as an Amazon-like product detail page.

    It mirrors what a shopper (and a browser extension) sees: title, store link, rating summary and histogram,
    price, bullet points, a product-information table and dated reviews. Like real Amazon pages, the reviews are
    plain markup without structured review data.

    :param product: The product.
    :return: The HTML page.
    """
    e = html.escape
    total = sum(product.histogram) or 1
    shares = {stars: round(100 * product.histogram[stars - 1] / total) for stars in range(1, 6)}
    histogram = "".join(
        f'<tr><td>{stars} star</td><td class="a-text-right">{shares[stars]}%</td></tr>' for stars in range(5, 0, -1)
    )
    bullets = "".join(f"<li><span>{e(f)}</span></li>" for f in product.features)
    description = "".join(f"<p>{e(d)}</p>" for d in product.description)
    details = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in product.details.items())
    reviews = "".join(
        '<div class="review" data-hook="review">'
        f'<span class="a-icon-alt">{r["rating"]}.0 out of 5 stars</span> '
        f'<a data-hook="review-title"><span>{e(str(r["title"]))}</span></a>'
        f'<span data-hook="review-date">Reviewed in the United States on {_date(r["timestamp"])}</span>'
        + ('<span data-hook="avp-badge">Verified Purchase</span>' if r["verified"] else "")
        + f'<span data-hook="review-body"><span>{e(str(r["text"]))}</span></span></div>'
        for r in sorted(product.sample, key=lambda r: -int(r["timestamp"] or 0))  # type: ignore[call-overload]
    )
    price = f"${product.price:.2f}" if product.price is not None else ""
    offers = (
        f', "offers": {{"@type": "Offer", "price": "{product.price:.2f}", "priceCurrency": "USD"}}' if price else ""
    )
    json_ld = (
        '{"@context": "https://schema.org", "@type": "Product", '
        f'"name": {json.dumps(product.title)}, "brand": {{"@type": "Brand", "name": {json.dumps(product.store)}}}, '
        f'"aggregateRating": {{"@type": "AggregateRating", "ratingValue": "{product.average_rating}", '
        f'"ratingCount": "{product.rating_number}"}}{offers}}}'
    )
    return f"""<!doctype html>
<html lang="en-us"><head><meta charset="utf-8">
<title>Amazon.com: {e(product.title)}</title>
<link rel="canonical" href="https://www.amazon.com/dp/{e(product.asin)}">
<script type="application/ld+json">{json_ld}</script>
</head><body>
<div id="nav-belt"><a href="/">amazon</a></div>
<div id="dp" class="{e(product.category)}">
<div id="imgTagWrapperId"><img id="landingImage" src="{e(product.image)}" alt="{e(product.title)}"></div>
<div id="centerCol">
<h1 id="title"><span id="productTitle">{e(product.title)}</span></h1>
<a id="bylineInfo" href="/stores/{e(product.store)}">Visit the {e(product.store)} Store</a>
<div id="averageCustomerReviews"><span class="a-icon-alt">{product.average_rating} out of 5 stars</span>
<span id="acrCustomerReviewText">{product.rating_number:,} ratings</span></div>
<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">{price}</span></span></div>
<div id="feature-bullets"><h2>About this item</h2><ul>{bullets}</ul></div>
</div>
<div id="productDescription"><h2>Product Description</h2>{description}</div>
<div id="prodDetails"><h2>Product information</h2><table>{details}
<tr><th>ASIN</th><td>{e(product.asin)}</td></tr></table></div>
<div id="reviewsMedley"><h2>Customer reviews</h2>
<span class="a-icon-alt">{product.average_rating} out of 5</span> <span>{product.rating_number:,} global ratings</span>
<table id="histogramTable">{histogram}</table>
<h3>Top reviews from the United States</h3>{reviews}</div>
</div></body></html>
"""


def build(corpus: Corpus, categories: tuple[str, ...], per_label: int, unlabelled: int, seed: int = 7) -> None:
    """
    Stream the categories, label products with the rules, and add a balanced sample to the corpus.

    :param corpus: The corpus to add to.
    :param categories: Dataset categories to stream.
    :param per_label: How many junk and how many legit products to add.
    :param unlabelled: How many unlabelled products to add as candidates for the second opinion.
    :param seed: Random seed, for a reproducible sample.
    """
    rng = random.Random(seed)
    dictionary = (
        frozenset(w.strip().lower() for w in _DICTIONARY_PATH.read_text(errors="ignore").splitlines())
        if _DICTIONARY_PATH.exists()
        else frozenset()
    )
    store_counts: Counter[str] = Counter()
    products: dict[str, Product] = {}
    for category in categories:
        found = load_products(category, store_counts)
        logger.info("amazon_meta_loaded", category=category, kept=len(found))
        attach_reviews(category, found, rng)
        logger.info("amazon_reviews_attached", category=category)
        products.update(found)

    buckets: dict[str | None, list[tuple[Product, Assessment]]] = {"junk": [], "legit": [], None: []}
    for product in products.values():
        if product.seen < 20:
            continue
        assessment = assess(product, store_counts, dictionary)
        buckets[assessment.label].append((product, assessment))
    logger.info("amazon_rule_labels", **{str(k): len(v) for k, v in buckets.items()})

    stats = {str(k): len(v) for k, v in buckets.items()}
    (corpus.root / "raw").mkdir(exist_ok=True)
    (corpus.root / "raw" / "amazon2023_rule_stats.json").write_text(json.dumps(stats, indent=2) + "\n")

    picked: list[tuple[LabelRecord, str]] = []
    for label, count in (("junk", per_label), ("legit", per_label), (None, unlabelled)):
        pool = buckets[label]
        rng.shuffle(pool)
        # Spread the sample over categories and stores.
        per_store: Counter[str] = Counter()
        taken = 0
        for product, assessment in pool:
            if taken >= count:
                break
            if per_store[product.store.lower()] >= 3:
                continue
            per_store[product.store.lower()] += 1
            taken += 1
            picked.append(
                (
                    LabelRecord(
                        id=page_id("amz", product.asin),
                        label=label,  # type: ignore[arg-type]
                        label_source="rule" if label else None,
                        prior_label=label,  # type: ignore[arg-type]
                        confidence=assessment.confidence,
                        platform="Amazon",
                        page_type="aggregate_listing",
                        source="synthetic",
                        origin_dataset=DATASET,
                        origin_url=f"https://www.amazon.com/dp/{product.asin}",
                        origin_id=product.asin,
                        collected_at="2023",
                        notes=json.dumps({"category": product.category, **assessment.features}),
                    ),
                    render(product),
                )
            )
    corpus.add_many(picked)
    logger.info("amazon_done", added=len(picked))


def main() -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--categories", nargs="+", default=list(DEFAULT_CATEGORIES))
    parser.add_argument("--per-label", type=int, default=150)
    parser.add_argument("--unlabelled", type=int, default=60)
    args = parser.parse_args()
    build(Corpus(), tuple(args.categories), args.per_label, args.unlabelled)


if __name__ == "__main__":
    main()
