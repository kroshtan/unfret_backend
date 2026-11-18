"""
Is the product photo the same photo as on other shops? Looked up in Unfret's own image index, nothing external.

The main photo only: it is fetched from the shop by us, fingerprinted on our server (:mod:`unfret.image_index`)
and looked up in the index (:mod:`unfret.image_store`). Its own shop and the same listing don't count. A photo
on more than a handful of shops can be honest (a brand's press photo) or a supplier photo: the skill only reports
what it found, and the shopper verdict calls it a warning only in a suspicious context, never for a known brand.

On a service company's site or profile the photo is the first one that isn't a logo or an icon (its work, its
van, its team): the same photo on other sites means it was copied from elsewhere.

On a peer-to-peer ad the photo is also compared with the other ads (:func:`unfret.image_store.lookup_ad`): the
same photo in another ad is a copied ad, or someone else's photo of the item.

After the lookup the photo is added to the index, in the background (off with ``fingerprints``), with the
analysis's provenance (:mod:`unfret.observations`): the index counts it in other pages' lookups once it came from a
page Unfret fetched itself, or enough independent sources reported it. A quick check records the photo the same way
after its answer, without the lookup (:func:`record_photo`).

The test shop's pages are looked up only in the test shop's own photo index (:mod:`unfret.test_shop_photos`), where
each finds the other's photo, and never in the real one or added to it.
"""

import asyncio
import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import structlog

from unfret import (
    background,
    image_index,
    image_store,
    observations,
    price_store,
    shop_store,
    shopper,
    test_shop_photos,
)
from unfret.config import settings
from unfret.facts.schema import PageFacts
from unfret.graph.platforms import PLATFORMS
from unfret.graph.skills import ANALYSED_PAGES, P2P_PAGES, PRODUCT_PAGES, Observation, Skill, SkillOutput
from unfret.graph.state import SERVICE_TYPES, AnalysisState
from unfret.image_index import ImagePrint
from unfret.image_store import ImageStore, Listing, MemoryImageStore, StoredPrint
from unfret.net import fetch_public
from unfret.test_shop import is_test_shop

logger = structlog.get_logger("unfret")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_FETCH_TIMEOUT_S = 6.0
_SHOWN = 3
_CATALOGUES = {"abo": "an Amazon catalogue listing"}
_NOT_A_PHOTO_RE = re.compile(r"logo|icon|favicon|sprite|badge|avatar|/pay(?:ment)?s?/|flag", re.IGNORECASE)

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


def _thumbnail(match: StoredPrint) -> str:
    return test_shop_photos.thumbnail_url(match) or thumbnail_url(match.id)


async def _record(where: Listing, fp: ImagePrint, image_url: str, day: date) -> int | None:
    global _purged_on  # noqa: PLW0603 - a per-process "purged today" marker
    provenance = observations.current()
    if not provenance.record:
        return None
    vouch = observations.vouch(b"photo|" + where.domain.encode() + b"|" + where.listing_key)
    try:
        row_id = await _store.upsert(
            where, fp, image_url=image_url, day=day, source="seen", bits=provenance.bits, vouch=vouch
        )
        if _purged_on != day:
            _purged_on = day
            await _store.purge(day)
    except Exception as exc:  # noqa: BLE001 - optional, after the lookup
        logger.warning("image_store_failed", error=type(exc).__name__)
        return None
    return row_id


async def _record_quietly(where: Listing, fp: ImagePrint, image_url: str, day: date) -> None:
    await _record(where, fp, image_url, day)


def _record_later(where: Listing, fp: ImagePrint, image_url: str, day: date) -> None:
    task = asyncio.create_task(_record_quietly(where, fp, image_url, day))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for background writes (tests and shutdown)."""
    await background.drain(_pending)


async def _fingerprint(image_url: str, model: image_index.Embedder) -> ImagePrint:
    """
    Fetch a photo (at most :data:`MAX_IMAGE_BYTES`, within the fetch timeout) and fingerprint it on this server.

    :param image_url: The photo.
    :param model: The embedder.
    :return: Its fingerprints.
    """
    data = await fetch_public(image_url, timeout=_FETCH_TIMEOUT_S, max_bytes=MAX_IMAGE_BYTES)
    return await asyncio.to_thread(image_index.fingerprint, data, model)


async def record_photo(url: str, facts: PageFacts, page_type: str) -> bool:
    """
    Add a product page's main photo to the index without looking it up: what a quick check records after its answer.

    The same row and the same limits as the photo check (:class:`ReverseImageSkill`), with the running analysis's
    provenance. Never the test shop, never ads (only product pages). Never raises.

    :param url: The page URL.
    :param facts: The page facts.
    :param page_type: The classified page type.
    :return: Whether a photo was recorded.
    """
    model = image_index.embedder()
    if page_type not in PRODUCT_PAGES or is_test_shop(url) or not model.available():
        return False
    image_url, where = main_photo(facts, page_type), image_store.listing(url, facts)
    if image_url is None or where is None:
        return False
    image_url = urljoin(url, image_url)  # a photo named by its path is on the page's site, as in the photo check
    try:
        fp = await _fingerprint(image_url, model)
    except Exception as exc:  # noqa: BLE001 - optional, after the answer (unreadable or not a product photo)
        logger.info("photo_record_skipped", error=type(exc).__name__)
        return False
    return await _record(where, fp, image_url, shop_store.today()) is not None


def main_photo(facts: PageFacts, page_type: str) -> str | None:
    """
    The photo to look up: a product page's first image; on a service provider's page the first that isn't a logo.

    :param facts: The page facts.
    :param page_type: The classified page type.
    :return: The image URL, or ``None`` when there is none.
    """
    if page_type in SERVICE_TYPES:
        return next((u for u in facts.image_urls if not _NOT_A_PHOTO_RE.search(u)), None)
    return facts.image_urls[0] if facts.image_urls else None


def _evidence(matches: list[StoredPrint], *, service: bool = False) -> list[Observation]:
    shops = [m for m in matches if m.source == "seen"]
    catalogue = [m for m in matches if m.source != "seen"]
    observations: list[Observation] = []
    if shops:
        names = ", ".join(m.domain for m in shops[:_SHOWN]) + (" and others" if len(shops) > _SHOWN else "")
        what, where = ("photo", "site") if service else ("product photo", "shop")
        observations.append(
            Observation(
                type="photo_on_other_shops",
                evidence=f"The same {what} is on {len(shops)} other {where}{'s' if len(shops) != 1 else ''}: {names}."[
                    :500
                ],
                thumbnails=[_thumbnail(m) for m in shops[:_SHOWN]],
            )
        )
    if catalogue:
        first = catalogue[0]
        brand = f" (brand: {first.label})" if first.label else ""
        observations.append(
            Observation(
                type="catalogue_photo",
                evidence=f"The same photo is used on {_CATALOGUES.get(first.source, 'a catalogue listing')}{brand}.",
                thumbnails=[_thumbnail(m) for m in catalogue[:_SHOWN]],
            )
        )
    return observations


def _money(price: float, currency: str) -> str:
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), "")
    return f"{symbol}{price:,.2f}" if symbol else f"{price:,.2f} {currency.upper()}".strip()


def _day(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"  # US English, as the site: "November 18, 2026"


async def same_product(matches: list[StoredPrint], own_image: str | None) -> dict[str, Any] | None:
    """
    The likely same product on another shop, for the result card (shown first, with both photos).

    Only what the index allows to be shown: the other shop's domain (or "Amazon", or "an Amazon catalogue listing"
    for a photo from the public catalogue seed, with the brand it names), its photo through this server's thumbnail
    proxy, and its price when Unfret's price history has seen that listing (a shop's listing with a public product
    ID, in the last 90 days). Nothing else of the seed or of the other listing. A test shop page's match is the other
    test shop page, with that page's own price (:mod:`unfret.test_shop_photos`).

    :param matches: The lookup's matches on shops and the catalogue (shops first).
    :param own_image: This page's own photo's thumbnail (``None`` when it isn't kept).
    :return: The fields of :class:`unfret.shopper.SameProduct`, or ``None`` without a match.
    """
    if not matches:
        return None
    copy = shopper.catalogue()["same_product"]
    first = matches[0]
    domain: str | None = None
    if first.source != "seen":
        where = copy["catalogue_brand"].format(brand=first.label) if first.label else copy["catalogue"]
    else:
        where = copy["families"].get(first.domain) or first.domain
        domain = None if first.domain in copy["families"] else first.domain
    rest = len(matches) - 1
    others = copy["others"].format(count=rest, shops=shopper.catalogue()["shop_one" if rest == 1 else "shop_many"])
    if test_shop_photos.is_test_row(first):
        seen = test_shop_photos.price(first, shop_store.today())
    else:
        seen = await price_store.seen_price(first.listing_key) if first.source == "seen" else None
    fields: dict[str, Any] = {
        "where": where,
        "domain": domain,
        "others": rest,
        "image": _thumbnail(first),
        "own_image": own_image,
        "title": copy["title"],
        "detail": copy["detail"].format(where=where, others=others if rest else ""),
    }
    if seen is not None and seen.currency:
        text = _money(seen.price, seen.currency)
        fields |= {
            "price": seen.price,
            "currency": seen.currency.upper(),
            "price_text": text,
            "price_seen_on": seen.day.isoformat(),
            "title": copy["title_price"].format(price=text),
            "detail": copy["detail_price"].format(
                where=where, others=others if rest else "", price=text, day=_day(seen.day)
            ),
        }
    return fields


def _ad_evidence(ads: list[StoredPrint]) -> list[Observation]:
    if not ads:
        return []
    names = list(dict.fromkeys(PLATFORMS[m.domain].name for m in ads if m.domain in PLATFORMS))
    where = f" ({', '.join(names)})" if names else ""
    count = f"{len(ads)} other ad{'s' if len(ads) != 1 else ''}"
    return [
        Observation(
            type="photo_in_other_ads",
            evidence=f"The same photo is in {count} checked in the last {image_store.AD_RETENTION_DAYS} days{where}.",
            thumbnails=[thumbnail_url(m.id) for m in ads[:_SHOWN]],
        )
    ]


class ReverseImageSkill(Skill):
    """The main product photo, looked up in Unfret's own image index."""

    name = "reverse_image"
    description = "Looks the main product photo up in Unfret's own index of shop and catalogue photos."
    page_types = ANALYSED_PAGES
    retry = True
    features = ("searched", "other_shops", "catalogue_matches", "other_ads")

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
        image_url = main_photo(facts, state.get("page_type", "storefront"))
        if image_url is None:
            return self.output({"searched": False}, note="No product image found.")
        # A page's structured data may name its photos by path ("/images/lamp.jpg"): fetched from the page's site.
        image_url = urljoin(state.get("url", ""), image_url)
        try:
            fp = await _fingerprint(image_url, model)
        except image_index.NotDiscriminativeError as exc:
            return self.output({"searched": False}, note=f"The main image can't identify a product ({exc}).")
        except Exception as exc:  # noqa: BLE001 - one optional signal
            logger.info("reverse_image_unavailable", error=type(exc).__name__)
            return self.output({"searched": False}, note="The main image couldn't be read.")

        url = state.get("url", "")
        page_type = state.get("page_type", "storefront")
        ad = page_type in P2P_PAGES
        test_page = test_shop_photos.page_of(url)
        if is_test_shop(url):  # only ever its own index: nothing of the real one, and nothing real matches it
            index: image_store.Candidates = test_shop_photos.TestShopImages(
                await test_shop_photos.rows(model) if test_page else []
            )
            where = test_shop_photos.listing(test_page) if test_page else None
        else:
            index, where = _store, image_store.listing(url, facts)
        found = await (image_store.lookup_ad if ad else image_store.lookup)(index, fp, where)
        ads = [m for m in found.matches if image_store.is_ad(m)]
        others = [m for m in found.matches if not image_store.is_ad(m)]
        product_match = not found.stop and page_type in PRODUCT_PAGES and bool(others)
        own_image = test_shop_photos.photo_url(test_page) if test_page and product_match else None
        if where is not None and not is_test_shop(url):  # the test shop's photos stay out
            if product_match:  # the card shows this page's photo next to the other one: its row ID is needed now
                own_id = await _record(where, fp, image_url, shop_store.today())
                own_image = thumbnail_url(own_id) if own_id is not None else None
            else:
                _record_later(where, fp, image_url, shop_store.today())
        if found.stop:
            return self.output({"searched": True}, note="The photo is a stock image found on many shops.")
        output = self.output(
            {
                "searched": True,
                "other_shops": sum(1 for m in others if m.source == "seen"),
                "catalogue_matches": sum(1 for m in others if m.source != "seen"),
                **({"other_ads": len(ads)} if ad else {}),
            },
            _ad_evidence(ads) + _evidence(others, service=page_type in SERVICE_TYPES),
        )
        if product_match and (match := await same_product(others, own_image)) is not None:
            output["same_product"] = match
        return output
