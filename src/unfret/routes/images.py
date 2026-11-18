"""
Thumbnails of matched photos, served by this server so a shopper's browser never contacts the other shops.

Only photos in the index can be fetched (by row ID, never by URL), they are re-encoded as small JPEGs, and kept in
a small in-memory cache. Anyone may ask (the IDs are in shared results), so the route is limited per visitor network,
remembers for a while which photos couldn't be read, decodes one photo at a time and refuses huge ones unread.
"""

import asyncio
import io
import time
from collections import OrderedDict

import structlog
from fastapi import APIRouter, HTTPException, Path, Request, Response
from PIL import Image

from unfret import test_shop_photos
from unfret.graph.skills import reverse_image
from unfret.image_index import load
from unfret.net import fetch_public
from unfret.schemas.errors import TooManyRequestsError
from unfret.visitors import RateLimit

logger = structlog.get_logger("unfret")

router = APIRouter(prefix="/v1/images", tags=["Images"])

THUMBNAIL_SIDE = 160
# Larger photos aren't decoded for a thumbnail: a flat 30-megapixel PNG is a few kB to send and ~100 MB to decode.
THUMBNAIL_MAX_PIXELS = 20_000_000
_CACHE_ENTRIES = 512
_cache: OrderedDict[int, bytes] = OrderedDict()
# Photos that couldn't be fetched or read, and until when they aren't tried again (monotonic seconds).
_FAILED_FOR_S = 3600.0
_failed: dict[int, float] = {}
_requests = RateLimit(120)  # thumbnails per visitor network per minute; a result shows a handful
_decoding = asyncio.Semaphore(1)


def _thumbnail(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as probe:  # reads the header only
        if probe.width * probe.height > THUMBNAIL_MAX_PIXELS:
            raise ValueError("photo too large for a thumbnail")
    image = load(data, min_side=1)
    image.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80)
    return out.getvalue()


def _limit(request: Request) -> None:
    """
    Count a thumbnail against the visitor network's limit.

    :param request: The incoming request.
    :raises TooManyRequestsError: Over the thumbnails allowed from this visitor network in a minute.
    """
    if (wait := _requests.retry_after(request.client.host if request.client else "unknown")) is not None:
        raise TooManyRequestsError(
            "Too many photos asked for; wait a moment.", metadata={"retry_after_s": int(wait) + 1}
        )


# Before the index's route, which would take "test-shop" for a row ID. Not part of the public API.
@router.get("/test-shop/{photo}", response_class=Response, include_in_schema=False)
async def test_shop_thumbnail(request: Request, photo: str) -> Response:
    """
    A small JPEG of a test shop product photo (the test shop's own photo index: :mod:`unfret.test_shop_photos`).

    :param request: The incoming request (its visitor network, for the limit).
    :param photo: The photo's file name.
    :return: The JPEG, cacheable for a day.
    :raises HTTPException: 404 for anything but the test shop's product photos.
    """
    _limit(request)
    data = test_shop_photos.thumbnail(photo)
    if data is None:
        raise HTTPException(404, "No such photo.")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{print_id}/thumbnail.jpg", response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
async def thumbnail(request: Request, print_id: int = Path(ge=1)) -> Response:
    """
    A small JPEG of a photo in the image index (the thumbnails in photo findings point here).

    :param request: The incoming request (its visitor network, for the limit).
    :param print_id: The index row, from a finding's thumbnail URL.
    :return: The JPEG, cacheable for a day.
    :raises HTTPException: 404 for an unknown row or a photo that is gone.
    :raises TooManyRequestsError: Over the thumbnails allowed from this visitor network in a minute.
    """
    if (wait := _requests.retry_after(request.client.host if request.client else "unknown")) is not None:
        raise TooManyRequestsError(
            "Too many photos asked for; wait a moment.", metadata={"retry_after_s": int(wait) + 1}
        )
    data = _cache.get(print_id)
    if data is None:
        now = time.monotonic()
        if _failed.get(print_id, 0.0) > now:
            raise HTTPException(404, "The photo is no longer available.")
        url = await reverse_image.store().image_url(print_id)
        if url is None:
            raise HTTPException(404, "No such photo.")
        try:
            raw = await fetch_public(url, timeout=6.0, max_bytes=8 * 1024 * 1024)
            async with _decoding:
                data = await asyncio.to_thread(_thumbnail, raw)
        except Exception as exc:
            logger.info("thumbnail_unavailable", error=type(exc).__name__)
            if len(_failed) > 10_000:
                _failed.clear()
            _failed[print_id] = now + _FAILED_FOR_S
            raise HTTPException(404, "The photo is no longer available.") from exc
        _cache[print_id] = data
        while len(_cache) > _CACHE_ENTRIES:
            _cache.popitem(last=False)
    _cache.move_to_end(print_id)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
