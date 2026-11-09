"""The image index: filters, the match rule, lookups (own shop, same listing, stock images), the skill and reasons."""

import io
from datetime import date, timedelta
from typing import Any

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from veridex import image_index, image_store, shop_store, shopper
from veridex.config import settings
from veridex.facts import PageFacts
from veridex.graph.skills import reverse_image
from veridex.graph.skills.reverse_image import ReverseImageSkill
from veridex.graph.state import Finding
from veridex.image_index import ImagePrint, NotDiscriminativeError, Thresholds
from veridex.image_store import Listing, MemoryImageStore
from veridex.main import app
from veridex.routes import images as images_route

TODAY = date(2026, 11, 9)


class StubEmbedder(image_index.Embedder):
    """A deterministic stand-in for the model: the normalised 16x24 grayscale thumbnail."""

    def __init__(self) -> None:
        super().__init__(image_index.Path("stub"))

    def available(self) -> bool:
        return True

    def embed(self, image: Image.Image) -> bytes:
        x = np.asarray(image_index._square(image).convert("L").resize((24, 16)), dtype=np.float32).ravel()
        x = x - x.mean()
        x = x / (np.linalg.norm(x) or 1.0)
        return np.clip(np.round(x * 127), -127, 127).astype(np.int8).tobytes()


def product_photo(seed: int, size: tuple[int, int] = (400, 400)) -> Image.Image:
    rng = np.random.default_rng(seed)
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x0, y0 = rng.integers(0, size[0] - 60), rng.integers(0, size[1] - 60)
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        draw.ellipse((x0, y0, x0 + rng.integers(30, 160), y0 + rng.integers(30, 160)), fill=colour)
    return image


def encode(image: Image.Image, fmt: str = "PNG", **kw: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, **kw)
    return out.getvalue()


@pytest.fixture(autouse=True)
def stub(monkeypatch: pytest.MonkeyPatch) -> MemoryImageStore:
    monkeypatch.setattr(image_index, "_embedder", StubEmbedder())
    monkeypatch.setattr(image_index, "embedder", lambda: image_index._embedder)
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(shop_store, "today", lambda: TODAY)
    store = MemoryImageStore()
    reverse_image.use_store(store)
    return store


def fp(image: Image.Image) -> ImagePrint:
    return image_index.fingerprint(encode(image))


@pytest.mark.parametrize(
    ("image", "reason"),
    [
        (Image.new("RGB", (120, 120), (10, 200, 10)), "too small"),
        (product_photo(1, (900, 200)), "a banner"),
        (Image.new("RGB", (500, 500), (255, 255, 255)), "blank"),
    ],
)
def test_images_that_cant_identify_a_product_are_left_out(image: Image.Image, reason: str) -> None:
    with pytest.raises(NotDiscriminativeError, match=reason):
        image_index.load(encode(image))


def test_nearly_blank_images_are_left_out() -> None:
    image = Image.new("RGB", (500, 500), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((230, 230, 270, 270), fill=(0, 0, 0))
    with pytest.raises(NotDiscriminativeError, match="nearly blank"):
        image_index.load(encode(image))


def test_untrusted_bytes_are_rejected() -> None:
    with pytest.raises(ValueError, match="not a supported image"):
        image_index.load(b"<svg onload=alert(1)>")


def watermarked(image: Image.Image) -> Image.Image:
    marked = image.copy()
    ImageDraw.Draw(marked).text((250, 370), "BESTDEALS", fill=(200, 30, 30))
    return marked


def test_edited_copies_match_and_other_photos_dont() -> None:
    original = product_photo(7)
    a = fp(original)
    for edited in (
        original.resize((800, 800)),
        Image.open(io.BytesIO(encode(original, "JPEG", quality=50))),
        watermarked(original),
    ):
        assert image_index.same_photo(a, fp(edited))
    others = [fp(product_photo(s)) for s in range(20, 40)]
    assert not any(image_index.same_photo(a, o) for o in others)


def test_a_recoloured_product_is_another_photo() -> None:
    red = product_photo(13)
    blue = Image.merge("RGB", red.split()[::-1])  # the same shot, colours swapped
    assert not image_index.same_photo(fp(red), fp(blue))
    darker = red.point(lambda v: int(v * 0.97))
    assert image_index.same_photo(fp(red), fp(darker))


def test_every_fingerprint_must_agree() -> None:
    a = fp(product_photo(3))
    unrelated = fp(product_photo(4))
    # Any one fingerprint disagreeing is enough: hashes, embedding or colours.
    assert not image_index.same_photo(a, ImagePrint(a.phash, a.dhash, unrelated.embedding, a.colour))
    assert not image_index.same_photo(a, ImagePrint(unrelated.phash, a.dhash, a.embedding, a.colour))
    assert not image_index.same_photo(a, ImagePrint(a.phash, unrelated.dhash, a.embedding, a.colour))
    assert not image_index.same_photo(a, ImagePrint(a.phash, a.dhash, a.embedding, unrelated.colour))
    assert image_index.same_photo(a, a, Thresholds(0, 0, 0.99))


def listing_at(domain: str, key: int) -> Listing:
    return Listing(domain, bytes([key]) * 32)


async def test_lookup_ignores_the_own_shop_and_the_same_listing(stub: MemoryImageStore) -> None:
    photo = fp(product_photo(5))
    await stub.upsert(listing_at("glowlamp.shop", 1), photo, image_url="https://glowlamp.shop/1.jpg", day=TODAY,
                      source="seen")  # fmt: skip
    await stub.upsert(
        listing_at("other.shop", 2), photo, image_url="https://other.shop/2.jpg", day=TODAY, source="seen"
    )
    found = await image_store.lookup(stub, photo, listing_at("glowlamp.shop", 3))
    assert [m.domain for m in found.matches] == ["other.shop"]


async def test_a_listing_that_changes_is_an_update_not_a_duplicate(stub: MemoryImageStore) -> None:
    where = listing_at("glowlamp.shop", 1)
    await stub.upsert(where, fp(product_photo(5)), image_url="u1", day=TODAY - timedelta(days=3), source="seen")
    await stub.upsert(where, fp(product_photo(6)), image_url="u2", day=TODAY, source="seen")
    assert len(stub.rows) == 1
    (row, url, first, last) = next(iter(stub.rows.values()))
    assert (url, first, last) == ("u2", TODAY - timedelta(days=3), TODAY)


async def test_a_photo_on_too_many_shops_is_a_stock_image(stub: MemoryImageStore) -> None:
    photo = fp(product_photo(8))
    for i in range(image_store.STOP_DOMAINS + 1):
        await stub.upsert(listing_at(f"s{i}.shop", i), photo, image_url="u", day=TODAY, source="seen")
    found = await image_store.lookup(stub, photo, listing_at("new.shop", 200))
    assert found.stop and found.matches == []


async def test_retention_keeps_the_catalogue_seed(stub: MemoryImageStore) -> None:
    old = TODAY - timedelta(days=image_store.RETENTION_DAYS + 1)
    await stub.upsert(listing_at("old.shop", 1), fp(product_photo(9)), image_url="u", day=old, source="seen")
    await stub.upsert(listing_at("amazon", 2), fp(product_photo(10)), image_url="u", day=old, source="abo")
    await stub.purge(TODAY)
    assert [row.source for row, *_ in stub.rows.values()] == ["abo"]


def test_listing_keys_follow_the_product_not_the_address() -> None:
    amazon = PageFacts(platform="amazon")
    nl = image_store.listing("https://www.amazon.nl/dp/B06X9STHNG?tag=x", amazon)
    de = image_store.listing("https://www.amazon.de/Schuh/dp/B06X9STHNG/ref=sr_1", amazon)
    assert nl == de and nl is not None and nl.domain == "amazon"
    shop = image_store.listing("https://shop.glowlamp.shop/products/lamp?variant=2", PageFacts())
    same = image_store.listing("https://glowlamp.shop/products/lamp/", PageFacts())
    assert shop == same and shop is not None and shop.domain == "glowlamp.shop"


def page(url: str = "https://glowlamp.shop/products/lamp", image: str = "https://cdn.glowlamp.shop/lamp.jpg") -> Any:
    return {
        "page_content": "",
        "url": url,
        "flags": [],
        "facts": PageFacts(image_urls=[image]),
        "cleaned_content": "",
        "page_type": "storefront",
    }


def serve(monkeypatch: pytest.MonkeyPatch, photos: dict[str, bytes]) -> list[str]:
    fetched: list[str] = []

    async def fetch(url: str, **_: Any) -> bytes:
        fetched.append(url)
        return photos[url]

    monkeypatch.setattr(reverse_image, "fetch_public", fetch)
    monkeypatch.setattr(images_route, "fetch_public", fetch)
    return fetched


async def test_skill_finds_the_photo_on_other_shops_and_records_it(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = product_photo(11)
    serve(monkeypatch, {"https://cdn.glowlamp.shop/lamp.jpg": encode(photo, "JPEG", quality=70)})
    await stub.upsert(listing_at("cheap-lamps.shop", 1), fp(photo), image_url="https://cheap/1.jpg", day=TODAY,
                      source="seen")  # fmt: skip
    await stub.upsert(listing_at("amazon", 2), fp(photo.resize((600, 600))), image_url="https://abo/2.jpg", day=TODAY,
                      source="abo", label="LumiCo")  # fmt: skip

    out = await ReverseImageSkill().run(page())
    await reverse_image.drain()

    f = out["features"]
    assert (f["reverse_image.searched"], f["reverse_image.other_shops"], f["reverse_image.catalogue_matches"]) == (
        1.0, 1.0, 1.0,
    )  # fmt: skip
    shops, catalogue = out["findings"]
    assert shops.evidence == "The same product photo is on 1 other shop: cheap-lamps.shop."
    assert shops.thumbnails == ["https://unfret.kroshtan.com/v1/images/1/thumbnail.jpg"]
    assert catalogue.evidence == "The same photo is used on an Amazon catalogue listing (brand: LumiCo)."
    assert ("glowlamp.shop" in {d for d, _ in stub.rows}) is True  # recorded for the next shop


async def test_skill_reports_nothing_without_the_model_or_with_fingerprints_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "fingerprints", False)
    assert (await ReverseImageSkill().run(page()))["features"] == {"reverse_image.searched": 0.0}
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(StubEmbedder, "available", lambda self: False)
    assert (await ReverseImageSkill().run(page()))["features"] == {"reverse_image.searched": 0.0}


async def test_skill_skips_a_logo_as_main_image(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, {"https://cdn.glowlamp.shop/lamp.jpg": encode(Image.new("RGB", (100, 100), (0, 0, 0)))})
    out = await ReverseImageSkill().run(page())
    assert out["features"] == {"reverse_image.searched": 0.0}
    assert "can't identify a product" in out["skill_results"][0]


async def test_thumbnails_are_proxied_and_only_for_indexed_photos(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetched = serve(monkeypatch, {"https://cheap/1.jpg": encode(product_photo(12))})
    await stub.upsert(listing_at("cheap.shop", 1), fp(product_photo(12)), image_url="https://cheap/1.jpg", day=TODAY,
                      source="seen")  # fmt: skip
    images_route._cache.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get("/v1/images/1/thumbnail.jpg")
        again = await client.get("/v1/images/1/thumbnail.jpg")
        missing = await client.get("/v1/images/99/thumbnail.jpg")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(ok.content)).size) <= images_route.THUMBNAIL_SIDE
    assert again.content == ok.content and fetched == ["https://cheap/1.jpg"]  # cached
    assert missing.status_code == 404


def verdict(features: dict[str, float], brand_known: bool = False) -> shopper.Verdict:
    finding = Finding(
        skill="reverse_image",
        type="photo_on_other_shops",
        evidence="The same product photo is on 2 other shops: a.shop, b.shop.",
        thumbnails=["https://unfret.kroshtan.com/v1/images/1/thumbnail.jpg"],
    )
    return shopper.build(
        30,
        features={"reverse_image.other_shops": 2.0, "content.brand_recognized": float(brand_known), **features},
        findings=[finding],
        facts=PageFacts(),
        page_type="storefront",
    )


def test_photo_reuse_is_a_warning_only_in_a_suspicious_context() -> None:
    reasons = verdict({"content.generic_product": 0.9}).reasons
    (reused,) = [r for r in reasons if r.type == "photo_reused"]
    assert reused.detail.startswith("The main product photo is the same photo as on 2 other shops (a.shop, b.shop).")
    assert reused.thumbnails == ["https://unfret.kroshtan.com/v1/images/1/thumbnail.jpg"]
    assert not [r for r in verdict({}).reasons if r.type == "photo_reused"]  # no context: no warning
    known = verdict({"content.generic_product": 0.9}, brand_known=True)
    assert not [r for r in known.reasons if r.type == "photo_reused"]  # never for a known brand


def test_young_linked_shops_are_a_reason_on_storefronts() -> None:
    finding = Finding(
        skill="shop_links",
        type="linked_young_shops",
        evidence="2 of the linked shops were registered in the last six months: a.shop, b.shop.",
    )
    reasons = shopper.build(
        30,
        features={"shop_links.linked_young_shops": 2.0},
        findings=[finding],
        facts=PageFacts(),
        page_type="storefront",
        language="nl",
    ).reasons
    (linked,) = [r for r in reasons if r.type == "linked_new_shops"]
    assert linked.detail.endswith("met 2 winkels die in het afgelopen halfjaar zijn geregistreerd: a.shop, b.shop.")
