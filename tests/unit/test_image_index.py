"""The image index: filters, the match rule, lookups (own shop, same listing, stock images), the skill and reasons."""

import io
from datetime import date, timedelta
from typing import Any

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from unfret import image_index, image_store, price_store, shop_store, shopper, test_shop_photos
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph.skills import reverse_image
from unfret.graph.skills.reverse_image import ReverseImageSkill
from unfret.graph.state import Finding
from unfret.image_index import ImagePrint, NotDiscriminativeError, Thresholds
from unfret.image_store import Listing, MemoryImageStore
from unfret.main import app
from unfret.routes import images as images_route

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
        x0, y0 = int(rng.integers(0, size[0] - 60)), int(rng.integers(0, size[1] - 60))
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        draw.ellipse((x0, y0, x0 + int(rng.integers(30, 160)), y0 + int(rng.integers(30, 160))), fill=colour)
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
    assert shops.thumbnails == ["https://unfret.net/v1/images/1/thumbnail.jpg"]
    assert catalogue.evidence == "The same photo is used on an Amazon catalogue listing (brand: LumiCo)."
    assert ("glowlamp.shop" in {d for d, _ in stub.rows}) is True  # recorded for the next shop


async def test_the_test_shops_photo_is_looked_up_but_never_recorded(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = "https://unfret.net/static/test-shop/projector-hero.jpg"
    serve(monkeypatch, {photo: encode(product_photo(14), "JPEG", quality=70)})

    out = await ReverseImageSkill().run(page("https://unfret.net/test-shop", photo))
    await reverse_image.drain()

    assert out["features"]["reverse_image.searched"] == 1.0
    assert stub.rows == {}  # its tricks are made up: nothing for the next shop to match


async def test_a_photo_named_by_its_path_is_fetched_from_the_pages_site(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Structured data may give the photo as a path (the test shop's does): fetching it as it stands fails.
    fetched = serve(monkeypatch, {"https://glowlamp.shop/images/lamp.jpg": encode(product_photo(15), "JPEG")})

    out = await ReverseImageSkill().run(page(image="/images/lamp.jpg"))
    await reverse_image.drain()

    assert fetched == ["https://glowlamp.shop/images/lamp.jpg"]
    assert out["features"]["reverse_image.searched"] == 1.0
    assert [url for _, url, *_ in stub.rows.values()] == ["https://glowlamp.shop/images/lamp.jpg"]  # for thumbnails


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


async def test_a_thumbnail_that_failed_is_not_fetched_again_at_once(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Anyone may ask for any thumbnail: a photo that can't be read must not make every request fetch it again.
    fetched = serve(monkeypatch, {})  # the shop's photo is gone: every fetch fails
    await stub.upsert(listing_at("gone.shop", 1), fp(product_photo(13)), image_url="https://gone/1.jpg", day=TODAY,
                      source="seen")  # fmt: skip
    images_route._cache.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/v1/images/1/thumbnail.jpg")
        second = await client.get("/v1/images/1/thumbnail.jpg")
    assert (first.status_code, second.status_code) == (404, 404)
    assert fetched == ["https://gone/1.jpg"]


async def test_a_huge_photo_is_not_decoded_for_a_thumbnail(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = encode(Image.new("RGB", (6000, 5000), (200, 30, 30)), "PNG")  # 30 MP, compresses to almost nothing
    serve(monkeypatch, {"https://big/1.png": huge})
    await stub.upsert(listing_at("big.shop", 1), fp(product_photo(14)), image_url="https://big/1.png", day=TODAY,
                      source="seen")  # fmt: skip
    images_route._cache.clear()
    decoded: list[object] = []

    def load(*args: object, **_: object) -> Image.Image:
        decoded.append(args)
        return Image.new("RGB", (10, 10))

    monkeypatch.setattr(images_route, "load", load)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        refused = await client.get("/v1/images/1/thumbnail.jpg")
    assert refused.status_code == 404 and decoded == []


async def test_thumbnails_are_limited_per_visitor_network(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, {})
    monkeypatch.setattr(images_route, "_requests", images_route.RateLimit(3))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        answers = [(await client.get(f"/v1/images/{n}/thumbnail.jpg")).status_code for n in range(1, 5)]
    assert answers == [404, 404, 404, 429]


def verdict(features: dict[str, float], brand_known: bool = False) -> shopper.Verdict:
    finding = Finding(
        skill="reverse_image",
        type="photo_on_other_shops",
        evidence="The same product photo is on 2 other shops: a.shop, b.shop.",
        thumbnails=["https://unfret.net/v1/images/1/thumbnail.jpg"],
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
    assert reused.thumbnails == ["https://unfret.net/v1/images/1/thumbnail.jpg"]
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
    ).reasons
    (linked,) = [r for r in reasons if r.type == "linked_new_shops"]
    assert linked.detail.endswith("with 2 shops registered in the last six months: a.shop, b.shop.")


# ── The same product sold elsewhere (the result card's first finding) ─────────────────────────────────────────────


@pytest.fixture
def prices(monkeypatch: pytest.MonkeyPatch) -> price_store.MemoryPriceStore:
    store = price_store.MemoryPriceStore()
    monkeypatch.setattr(price_store, "_store", store)
    return store


async def test_the_same_product_on_another_shop_with_its_price_and_both_photos(
    stub: MemoryImageStore, prices: price_store.MemoryPriceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = product_photo(21)
    serve(monkeypatch, {"https://cdn.glowlamp.shop/lamp.jpg": encode(photo, "JPEG", quality=70)})
    other = listing_at("cheap-lamps.shop", 1)
    await stub.upsert(other, fp(photo), image_url="https://cheap/1.jpg", day=TODAY, source="seen")
    await stub.upsert(listing_at("budget.shop", 3), fp(photo), image_url="https://budget/3.jpg", day=TODAY,
                      source="seen")  # fmt: skip
    # The price history keys a shop's listing as the index does (domain and product ID): its price can be named.
    await prices.record(other.listing_key, price_store.Observation(TODAY - timedelta(days=2), 12.99, "USD", None))
    await prices.record(other.listing_key, price_store.Observation(TODAY - timedelta(days=9), 14.5, "USD", None))

    out = await ReverseImageSkill().run(page())

    match = out["same_product"]
    own = next(row.id for row, *_ in stub.rows.values() if row.domain == "glowlamp.shop")
    assert match == {
        "where": "cheap-lamps.shop",
        "domain": "cheap-lamps.shop",
        "others": 1,
        "image": "https://unfret.net/v1/images/1/thumbnail.jpg",
        "own_image": f"https://unfret.net/v1/images/{own}/thumbnail.jpg",  # recorded at once, for its thumbnail
        "title": "Looks like the same product, sold elsewhere for $12.99",
        "detail": "The main photo is the same photo as on cheap-lamps.shop and 1 other shop. Unfret saw it there for "
        "$12.99 on 7 November 2026.",
        "price": 12.99,
        "currency": "USD",
        "price_text": "$12.99",
        "price_seen_on": "2026-11-07",
    }
    assert shopper.SameProduct.model_validate(match).price_text == "$12.99"


async def test_a_catalogue_match_names_no_price_and_nothing_of_the_seed_but_the_brand(
    stub: MemoryImageStore, prices: price_store.MemoryPriceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = product_photo(22)
    serve(monkeypatch, {"https://cdn.glowlamp.shop/lamp.jpg": encode(photo, "JPEG", quality=70)})
    await stub.upsert(listing_at("amazon", 2), fp(photo), image_url="https://abo/2.jpg", day=TODAY, source="abo",
                      label="LumiCo")  # fmt: skip

    match = (await ReverseImageSkill().run(page()))["same_product"]

    assert match["where"] == "an Amazon catalogue listing (brand: LumiCo)"
    assert match["domain"] is None and "price" not in match
    assert match["title"] == "Looks like the same product, sold elsewhere"
    assert "abo/2.jpg" not in str(match)  # only the thumbnail proxy's address, never the seed's own


async def test_no_same_product_without_a_match_or_for_ads(
    stub: MemoryImageStore, prices: price_store.MemoryPriceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = product_photo(23)
    ad_photo = "https://img.marktplaats.nl/a.jpg"
    serve(monkeypatch, {ad_photo: encode(photo, "JPEG", quality=70), "https://cdn.glowlamp.shop/lamp.jpg":
                        encode(product_photo(24), "JPEG", quality=70)})  # fmt: skip
    assert "same_product" not in await ReverseImageSkill().run(page())  # nothing in the index yet
    await stub.upsert(listing_at("cheap.shop", 9), fp(photo), image_url="u", day=TODAY, source="seen")

    ad = page("https://www.marktplaats.nl/v/a/m1", ad_photo) | {"page_type": "p2p_listing"}
    assert "same_product" not in await ReverseImageSkill().run(ad)
    await reverse_image.drain()


# ── The test shop's own photo index (unfret.test_shop_photos) ────────────────────────────────────────────────────

TEST_SHOP = "https://unfret.net/test-shop"
SISTER = "https://unfret.net/test-shop/sister-shop"


def serve_test_shop(monkeypatch: pytest.MonkeyPatch, extra: dict[str, bytes] | None = None) -> None:
    files = test_shop_photos.PHOTO_DIR
    serve(monkeypatch, {
        "https://unfret.net/static/test-shop/projector-hero.jpg": (files / "projector-hero.jpg").read_bytes(),
        "https://unfret.net/static/test-shop/sister-hero.jpg": (files / "sister-hero.jpg").read_bytes(),
        **(extra or {}),
    })  # fmt: skip
    monkeypatch.setattr(test_shop_photos, "_rows", None)  # fingerprinted with this test's model


async def test_a_test_shop_page_finds_its_photo_on_the_sister_shop(
    stub: MemoryImageStore, prices: price_store.MemoryPriceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_test_shop(monkeypatch)

    out = await ReverseImageSkill().run(page(TEST_SHOP, "https://unfret.net/static/test-shop/projector-hero.jpg"))
    back = await ReverseImageSkill().run(page(SISTER, "https://unfret.net/static/test-shop/sister-hero.jpg"))
    await reverse_image.drain()

    assert out["features"]["reverse_image.other_shops"] == 1.0
    [found] = out["findings"]
    assert found.evidence == "The same product photo is on 1 other shop: unfret.net/test-shop/sister-shop."
    assert found.thumbnails == ["https://unfret.net/v1/images/test-shop/sister-hero.jpg"]
    assert out["same_product"] == {
        "where": "unfret.net/test-shop/sister-shop",
        "domain": "unfret.net/test-shop/sister-shop",
        "others": 0,
        "image": "https://unfret.net/v1/images/test-shop/sister-hero.jpg",
        "own_image": "https://unfret.net/v1/images/test-shop/projector-hero.jpg",
        "title": "Looks like the same product, sold elsewhere for $17.99",
        "detail": "The main photo is the same photo as on unfret.net/test-shop/sister-shop. Unfret saw it there for "
        "$17.99 on 9 November 2026.",
        "price": 17.99,
        "currency": "USD",
        "price_text": "$17.99",
        "price_seen_on": "2026-11-09",
    }
    assert shopper.SameProduct.model_validate(out["same_product"]).where == "unfret.net/test-shop/sister-shop"
    assert back["same_product"]["image"] == "https://unfret.net/v1/images/test-shop/projector-hero.jpg"
    assert back["same_product"]["price_text"] == "$39.99"
    # Two made-up brands on one photo, ready for a finding about it; nothing of it is written anywhere.
    assert [row.label for _, row in await test_shop_photos.rows(image_index.embedder())] == ["LUMIQZEN", "VELUNOX"]
    assert stub.rows == {} and prices.rows == {}


async def test_the_test_shop_and_the_real_index_never_match_each_other(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    hero = (test_shop_photos.PHOTO_DIR / "projector-hero.jpg").read_bytes()
    copied = "https://cdn.glowlamp.shop/lamp.jpg"
    serve_test_shop(monkeypatch, {copied: hero})
    await stub.upsert(listing_at("cheap.shop", 9), fp(Image.open(io.BytesIO(hero))), image_url="u", day=TODAY,
                      source="seen")  # fmt: skip

    test_page = await ReverseImageSkill().run(
        page(TEST_SHOP, "https://unfret.net/static/test-shop/projector-hero.jpg")
    )
    real_page = await ReverseImageSkill().run(page(image=copied))  # a real shop with the test shop's photo
    await reverse_image.drain()

    assert test_page["same_product"]["where"] == "unfret.net/test-shop/sister-shop"  # not cheap.shop
    assert real_page["same_product"]["where"] == "cheap.shop"  # not the test shop's sister
    assert "test-shop" not in str(real_page)
    assert {d for d, _ in stub.rows} == {"cheap.shop", "glowlamp.shop"}  # the test shop's page isn't kept


async def test_a_test_shop_page_without_a_product_photo_matches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    serve_test_shop(monkeypatch)
    checkout = page(f"{TEST_SHOP}/checkout", "https://unfret.net/static/test-shop/projector-hero.jpg")
    out = await ReverseImageSkill().run(checkout)
    assert "same_product" not in out and out["features"]["reverse_image.other_shops"] == 0.0


async def test_test_shop_thumbnails_come_from_its_static_photos_only() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get("/v1/images/test-shop/sister-hero.jpg")
        other = await client.get("/v1/images/test-shop/shop.js")
        up = await client.get("/v1/images/test-shop/..%2Fsite.css")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(ok.content)).size) <= test_shop_photos.THUMBNAIL_SIDE
    assert (other.status_code, up.status_code) == (404, 404)
    assert "/v1/images/test-shop/{photo}" not in app.openapi()["paths"]  # not part of the public API
