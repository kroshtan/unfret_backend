"""
The test shop's own photo index: a check of a test shop page finds its photo on the other test shop page.

The test shop (:mod:`unfret.test_shop`) has two product pages: Nebulora Home's LUMIQZEN projector at ``/test-shop``
and the sister shop Quenvale Deals' VELUNOX projector at ``/test-shop/sister-shop``, cheaper, with a re-uploaded copy
of the same made-up photo (drawn by ``scripts/test_shop_images.py``). Two brand names on one photo is deliberate: it is
the case of the same generic product sold under different made-up brands, and the test shop demos it.

This index is separate from Unfret's real one (:mod:`unfret.image_store`) and is scoped by address both ways:

- a test shop page is looked up only here, never in the real index, and is never added to the real index (the skill
  already kept it out); a real page is never looked up here (:func:`unfret.test_shop.is_test_shop` decides, by the
  address alone, so a real shop can't get in by copying the test shop's markup or photos);
- the rows are made from the static files on disk at first use, in memory, and are never written anywhere. Their IDs
  are negative, so they can't be taken for a real row; their thumbnails come from the static files through
  ``/v1/images/test-shop/<photo>`` (:func:`thumbnail`), and their "seen" price is the sister page's own.
"""

import asyncio
import hashlib
import io
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from PIL import Image

from unfret import image_index
from unfret.config import settings
from unfret.image_store import Listing, StoredPrint
from unfret.net import hostname
from unfret.price_store import Observation
from unfret.test_shop import PATH, is_test_shop

logger = structlog.get_logger("unfret")

SISTER_PATH = f"{PATH}/sister-shop"
PHOTO_DIR = Path(__file__).resolve().parent / "static" / "test-shop"
THUMBNAIL_SIDE = 160


@dataclass(frozen=True)
class Page:
    """A test shop product page and its main photo."""

    path: str
    photo: str  # in static/test-shop
    brand: str
    price: float  # its price on the page, USD


PAGES = (
    Page(PATH, "projector-hero.jpg", "LUMIQZEN", 39.99),
    Page(SISTER_PATH, "sister-hero.jpg", "VELUNOX", 17.99),
)
_PHOTOS = {page.photo for page in PAGES}
_rows: list[tuple[Page, StoredPrint]] | None = None
_building = asyncio.Lock()


def page_of(url: str) -> Page | None:
    """
    The test shop product page a URL is, by its address.

    :param url: The checked URL.
    :return: The page, or ``None`` for anything else (a real page, the checkout).
    """
    if not is_test_shop(url):
        return None
    path = urlsplit(url if "://" in url else f"https://{url}").path.rstrip("/") or "/"
    return next((page for page in PAGES if page.path == path), None)


def _domain(page: Page) -> str:
    # The page's address on this service, as the findings and the card name it ("dev.unfret.net/test-shop/…").
    return f"{hostname(settings.public_base_url) or 'unfret.net'}{page.path}"


def listing(page: Page) -> Listing:
    """
    A test shop page as a listing of this index: each page is a shop of its own here.

    :param page: The page.
    :return: The listing.
    """
    return Listing(_domain(page), hashlib.sha256(f"test-shop|{page.path}".encode()).digest())


def _fingerprints(model: image_index.Embedder) -> list[tuple[Page, StoredPrint]]:
    rows = []
    for i, page in enumerate(PAGES, start=1):
        fp = image_index.fingerprint((PHOTO_DIR / page.photo).read_bytes(), model)
        where = listing(page)
        rows.append((page, StoredPrint(-i, "seen", where.domain, where.listing_key, fp, page.brand)))
    return rows


async def rows(model: image_index.Embedder) -> list[tuple[Page, StoredPrint]]:
    """
    The index: each page's main photo, fingerprinted with the skill's model once per process.

    :param model: The embedder.
    :return: The pages and their rows (empty when the photos can't be read).
    """
    global _rows  # noqa: PLW0603 - built once per process
    async with _building:
        if _rows is None:
            try:
                _rows = await asyncio.to_thread(_fingerprints, model)
            except Exception as exc:  # noqa: BLE001 - the test shop only loses its match
                logger.warning("test_shop_photos_failed", error=type(exc).__name__)
                return []
    return _rows


class TestShopImages:
    """The index as an :class:`~unfret.image_store.ImageStore` for lookups (read only)."""

    def __init__(self, found: list[tuple[Page, StoredPrint]]) -> None:
        """
        Over the rows of :func:`rows`.

        :param found: The rows.
        """
        self._rows = [row for _, row in found]

    async def candidates(self, phash: int, max_distance: int) -> list[StoredPrint]:
        """
        The rows within ``max_distance`` bits.

        :param phash: The query hash.
        :param max_distance: The largest distance.
        :return: The rows.
        """
        return [row for row in self._rows if image_index.hamming(row.print.phash, phash) <= max_distance]


def is_test_row(match: StoredPrint) -> bool:
    """
    Whether a match is one of this index's rows (negative IDs, never a real row's).

    :param match: The match.
    :return: ``True`` for a test shop row.
    """
    return match.id < 0


def _page_of_row(match: StoredPrint) -> Page | None:
    return PAGES[-match.id - 1] if is_test_row(match) and -match.id <= len(PAGES) else None


def thumbnail_url(match: StoredPrint) -> str | None:
    """
    Where a client fetches a test shop row's thumbnail (under ``/v1/images/``, as the clients expect).

    :param match: The match.
    :return: The URL, or ``None`` for a real row.
    """
    page = _page_of_row(match)
    return photo_url(page) if page else None


def photo_url(page: Page) -> str:
    """
    A test shop page's photo thumbnail.

    :param page: The page.
    :return: The URL.
    """
    return f"{settings.public_base_url.rstrip('/')}/v1/images/test-shop/{page.photo}"


def price(match: StoredPrint, day: date) -> Observation | None:
    """
    A test shop row's price, as the card names a price Unfret saw: the page's own, today.

    :param match: The match.
    :param day: Today.
    :return: The price, or ``None`` for a real row.
    """
    page = _page_of_row(match)
    return Observation(day, page.price, "USD", None) if page else None


@cache
def thumbnail(photo: str) -> bytes | None:
    """
    A small JPEG of a test shop photo, from the static file.

    :param photo: The photo's file name.
    :return: The JPEG, or ``None`` for anything but the test shop's product photos.
    """
    if photo not in _PHOTOS:
        return None
    with Image.open(PHOTO_DIR / photo) as image:
        small = image.convert("RGB")
        small.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        small.save(out, format="JPEG", quality=80)
    return out.getvalue()
