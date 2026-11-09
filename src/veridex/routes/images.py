"""
Thumbnails of matched photos, served by this server so a shopper's browser never contacts the other shops.

Only photos in the index can be fetched (by row ID, never by URL), they are re-encoded as small JPEGs, and kept in
a small in-memory cache.
"""

import asyncio
import io
from collections import OrderedDict

import structlog
from fastapi import APIRouter, HTTPException, Path, Response
from PIL import Image

from veridex.graph.skills import reverse_image
from veridex.image_index import load
from veridex.net import fetch_public

logger = structlog.get_logger("veridex")

router = APIRouter(prefix="/v1/images", tags=["Images"])

THUMBNAIL_SIDE = 160
_CACHE_ENTRIES = 512
_cache: OrderedDict[int, bytes] = OrderedDict()


def _thumbnail(data: bytes) -> bytes:
    image = load(data, min_side=1)
    image.thumbnail((THUMBNAIL_SIDE, THUMBNAIL_SIDE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80)
    return out.getvalue()


@router.get("/{print_id}/thumbnail.jpg", response_class=Response, responses={200: {"content": {"image/jpeg": {}}}})
async def thumbnail(print_id: int = Path(ge=1)) -> Response:
    """
    A small JPEG of a photo in the image index (the thumbnails in photo findings point here).

    :param print_id: The index row, from a finding's thumbnail URL.
    :return: The JPEG, cacheable for a day.
    :raises HTTPException: 404 for an unknown row or a photo that is gone.
    """
    data = _cache.get(print_id)
    if data is None:
        url = await reverse_image.store().image_url(print_id)
        if url is None:
            raise HTTPException(404, "No such photo.")
        try:
            data = await asyncio.to_thread(_thumbnail, await fetch_public(url, timeout=6.0, max_bytes=8 * 1024 * 1024))
        except Exception as exc:
            logger.info("thumbnail_unavailable", error=type(exc).__name__)
            raise HTTPException(404, "The photo is no longer available.") from exc
        _cache[print_id] = data
        while len(_cache) > _CACHE_ENTRIES:
            _cache.popitem(last=False)
    _cache.move_to_end(print_id)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
