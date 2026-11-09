"""
Google Cloud Vision web detection: where else a product photo appears, to spot supplier photos (Temu, AliExpress…).

Request format (verified against Google's documentation, 8 November 2026):
``POST https://vision.googleapis.com/v1/images:annotate`` with
``{"requests": [{"image": {"content": <base64>}, "features": [{"type": "WEB_DETECTION", "maxResults": N}]}]}``
(https://cloud.google.com/vision/docs/detecting-web), the API key in the ``x-goog-api-key`` header, which Google
recommends over the ``?key=`` parameter because URLs get logged (https://cloud.google.com/docs/authentication/api-keys-use).
Requests are capped at 10 MB of JSON (https://cloud.google.com/vision/docs/supported-files).

Only the bytes of the public product image are sent: never the page, its address, the caller or the listing. The
image is fetched by us (so Google never contacts the shop) and results are cached by perceptual hash
(:mod:`veridex.image_hash`), so a supplier photo reused across many listings costs one request. A monthly budget,
counted in images, caps spending; once it's used up, requests are refused instead of billed. Off without a key.
"""

import base64
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
import structlog

from veridex.config import settings
from veridex.price_store import registrable_domain

logger = structlog.get_logger("veridex")

# Marketplaces that supply generic goods: a product photo found there is the factory's, resold under another name.
SUPPLIER_DOMAINS: dict[str, str] = {
    "aliexpress.com": "AliExpress",
    "aliexpress.us": "AliExpress",
    "aliexpress.ru": "AliExpress",
    "temu.com": "Temu",
    "alibaba.com": "Alibaba",
    "1688.com": "1688",
    "dhgate.com": "DHgate",
    "shein.com": "Shein",
    "wish.com": "Wish",
    "banggood.com": "Banggood",
    "lightinthebox.com": "LightInTheBox",
    "miniinthebox.com": "MiniInTheBox",
    "joom.com": "Joom",
    "made-in-china.com": "Made-in-China",
    "cjdropshipping.com": "CJdropshipping",
    "tomtop.com": "Tomtop",
    "gearbest.com": "Gearbest",
}
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # base64 grows it by a third; the request must stay under 10 MB
_MAX_RESULTS = 50
_TIMEOUT_S = 20.0


class BudgetExceededError(RuntimeError):
    """The image budget for this month is used up."""


class ImageBudget(Protocol):
    """Counts images sent per calendar month."""

    async def reserve(self, month: str, limit: int) -> bool:
        """
        Count one image for ``month`` if that stays within ``limit``.

        :param month: ``YYYY-MM``.
        :param limit: The monthly maximum.
        :return: ``True`` if the request may go ahead.
        """
        ...


class MemoryBudget:
    """An in-process counter (resets on restart; the app swaps in a database-backed one)."""

    def __init__(self) -> None:
        """Start with no images counted."""
        self._counts: dict[str, int] = {}

    async def reserve(self, month: str, limit: int) -> bool:
        """
        Count one image for ``month`` if that stays within ``limit``.

        :param month: ``YYYY-MM``.
        :param limit: The monthly maximum.
        :return: ``True`` if the request may go ahead.
        """
        if self._counts.get(month, 0) >= limit:
            return False
        self._counts[month] = self._counts.get(month, 0) + 1
        return True


_budget: ImageBudget = MemoryBudget()


def use_budget(budget: ImageBudget) -> None:
    """
    Replace the process-wide budget counter (done at startup when a database is available).

    :param budget: The counter to use from now on.
    """
    global _budget  # noqa: PLW0603 - one process-wide counter, swapped once at startup
    _budget = budget


def is_enabled() -> bool:
    """
    Whether a Vision API key is configured.

    :return: ``True`` with a key.
    """
    return bool(settings.vision_api_key.get_secret_value())


def _supplier(url: str) -> str | None:
    host = urlsplit(url).hostname or ""
    return SUPPLIER_DOMAINS.get(registrable_domain(host)) if host else None


def summarise(web: dict[str, Any]) -> dict[str, Any]:
    """
    Reduce a ``webDetection`` result to what the analysis uses.

    :param web: The ``responses[0].webDetection`` object.
    :return: ``full`` and ``partial`` (matching images anywhere), ``pages`` (pages showing the image),
        ``supplier_pages`` (of those, on supplier marketplaces), ``supplier_full`` (supplier pages with an exact
        copy), ``suppliers`` (their names, most pages first) and ``examples`` (up to 3 supplier page URLs).
    """
    pages = [p for p in web.get("pagesWithMatchingImages") or [] if isinstance(p, dict) and p.get("url")]
    counts: dict[str, int] = {}
    supplier_full = 0
    examples: list[str] = []
    for page in pages:
        name = _supplier(str(page["url"]))
        if name is None:
            continue
        counts[name] = counts.get(name, 0) + 1
        supplier_full += bool(page.get("fullMatchingImages"))
        if len(examples) < 3:
            examples.append(str(page["url"])[:300])
    return {
        "full": len(web.get("fullMatchingImages") or []),
        "partial": len(web.get("partialMatchingImages") or []),
        "pages": len(pages),
        "supplier_pages": sum(counts.values()),
        "supplier_full": supplier_full,
        "suppliers": sorted(counts, key=lambda n: -counts[n]),
        "examples": examples,
    }


async def web_detection(image: bytes, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """
    Ask Vision where else an image appears, within the monthly budget.

    :param image: The image bytes (at most :data:`MAX_IMAGE_BYTES`).
    :param transport: Optional httpx transport, for tests.
    :return: The summary (see :func:`summarise`).
    :raises BudgetExceededError: If this month's budget is used up.
    :raises ValueError: If the image is too large.
    :raises httpx.HTTPStatusError: On API errors (not cached: the next analysis asks again).
    """
    if len(image) > MAX_IMAGE_BYTES:
        raise ValueError("image too large for a Vision request")
    month = datetime.now(tz=UTC).strftime("%Y-%m")
    if not await _budget.reserve(month, settings.vision_monthly_budget):
        raise BudgetExceededError(f"Vision budget of {settings.vision_monthly_budget} images used up for {month}")
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image).decode()},
                "features": [{"type": "WEB_DETECTION", "maxResults": _MAX_RESULTS}],
            }
        ]
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, transport=transport) as client:
        r = await client.post(
            settings.vision_api_url,
            json=body,
            headers={"x-goog-api-key": settings.vision_api_key.get_secret_value()},
        )
    r.raise_for_status()
    response = (r.json().get("responses") or [{}])[0]
    if "error" in response:  # a per-image error inside a 200 (e.g. unreadable image)
        raise httpx.HTTPStatusError(str(response["error"].get("message", ""))[:200], request=r.request, response=r)
    summary = summarise(response.get("webDetection") or {})
    logger.info("vision_web_detection", pages=summary["pages"], supplier_pages=summary["supplier_pages"])
    return summary
