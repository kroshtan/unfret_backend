"""
Is the product photo the same photo as on other shops? Looked up in Unfret's own image index, nothing external.

The main photo only: it is fetched from the shop by us, fingerprinted on our server (:mod:`unfret.image_index`)
and looked up in the index (:mod:`unfret.image_store`). Its own shop and the same listing don't count. A photo
on more than a handful of shops can be honest (a brand's press photo) or a supplier photo: the skill only reports
what it found, and the shopper verdict calls it a warning only in a suspicious context, never for a known brand.

After the lookup the photo is added to the index, in the background (off with ``fingerprints``).
"""

import asyncio
from datetime import date

import structlog

from unfret import image_index, image_store, shop_store
from unfret.config import settings
from unfret.graph.skills import Observation, Skill, SkillOutput
from unfret.graph.state import AnalysisState
from unfret.image_index import ImagePrint
from unfret.image_store import ImageStore, Listing, MemoryImageStore, StoredPrint
from unfret.net import fetch_public

logger = structlog.get_logger("unfret")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_FETCH_TIMEOUT_S = 6.0
_SHOWN = 3
_CATALOGUES = {"abo": "an Amazon catalogue listing"}

_store: ImageStore = MemoryImageStore()
_pending: set[asyncio.Task[None]] = set()
_purged_on: date | None = None


def use_store(store: ImageStore) -> None:
    """
    Use a different index (the app swaps in :class:`~unfret.image_store.PostgresImageStore` at startup).

    :param store: The index.
    """
    global _store, _purged_on
    _store, _purged_on = store, None


def store() -> ImageStore:
    """
    The index in use.

    :return: The index.
    """
    return _store


def thumbnail_url(print_id: int) -> str:
    """
    Where a client fetches a matched photo's thumbnail.

    Through this server, so the shopper never contacts the other shop.

    :param print_id: The index row.
    :return: The URL.
    """
    return f"{settings.public_base_url.rstrip('/')}/v1/images/{print_id}/thumbnail.jpg"


async def _record(where: Listing, fp: ImagePrint, image_url: str, day: date) -> None:
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    try:
        await _store.upsert(where, fp, image_url=image_url, day=day, source="seen")
        if _purged_on != day:
            _purged_on = day
            await _store.purge(day)
    except Exception as exc:  # noqa: BLE001 - optional, after the lookup
        logger.warning("image_store_failed", error=type(exc).__name__)


def _record_later(where: Listing, fp: ImagePrint, image_url: str, day: date) -> None:
    task = asyncio.create_task(_record(where, fp, image_url, day))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for background writes (tests and shutdown)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def _evidence(matches: list[StoredPrint]) -> list[Observation]:
    shops = [m for m in matches if m.source == "seen"]
    catalogue = [m for m in matches if m.source != "seen"]
    observations: list[Observation] = []
    if shops:
        names = ", ".join(m.domain for m in shops[:_SHOWN]) + (" and others" if len(shops) > _SHOWN else "")
        observations.append(
            Observation(
                type="photo_on_other_shops",
                evidence=f"The same product photo is on {len(shops)} other shop{'s' if len(shops) != 1 else ''}: "
                f"{names}."[:500],
                thumbnails=[thumbnail_url(m.id) for m in shops[:_SHOWN]],
            )
        )
    if catalogue:
        first = catalogue[0]
        brand = f" (brand: {first.label})" if first.label else ""
        observations.append(
            Observation(
                type="catalogue_photo",
                evidence=f"The same photo is used on {_CATALOGUES.get(first.source, 'a catalogue listing')}{brand}.",
                thumbnails=[thumbnail_url(m.id) for m in catalogue[:_SHOWN]],
            )
        )
    return observations


class ReverseImageSkill(Skill):
    """The main product photo, looked up in Unfret's own image index."""

    name = "reverse_image"
    description = "Looks the main product photo up in Unfret's own index of shop and catalogue photos."
    features = ("searched", "other_shops", "catalogue_matches")

    async def run(self, state: AnalysisState) -> SkillOutput:
        """
        Fetch and fingerprint the main photo, look it up, and add it to the index.

        :param state: Current graph state; uses ``facts`` and ``url``.
        :return: The skill's features and findings.
        """
        facts = state["facts"]
        model = image_index.embedder()
        if not settings.fingerprints or not model.available():
            return self.output({"searched": False}, note="The photo index is not available.")
        if not facts.image_urls:
            return self.output({"searched": False}, note="No product image found.")
        image_url = facts.image_urls[0]
        try:
            data = await fetch_public(image_url, timeout=_FETCH_TIMEOUT_S, max_bytes=MAX_IMAGE_BYTES)
            fp = await asyncio.to_thread(image_index.fingerprint, data, model)
        except image_index.NotDiscriminativeError as exc:
            return self.output({"searched": False}, note=f"The main image can't identify a product ({exc}).")
        except Exception as exc:  # noqa: BLE001 - one optional signal
            logger.info("reverse_image_unavailable", error=type(exc).__name__)
            return self.output({"searched": False}, note="The main image couldn't be read.")

        where = image_store.listing(state.get("url", ""), facts)
        found = await image_store.lookup(_store, fp, where)
        if where is not None:
            _record_later(where, fp, image_url, shop_store.today())
        if found.stop:
            return self.output({"searched": True}, note="The photo is a stock image found on many shops.")
        return self.output(
            {
                "searched": True,
                "other_shops": sum(1 for m in found.matches if m.source == "seen"),
                "catalogue_matches": sum(1 for m in found.matches if m.source != "seen"),
            },
            _evidence(found.matches),
        )
