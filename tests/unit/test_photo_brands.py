"""
The same photo under other brand names (D6): brand names made comparable, kept with each photo, compared in the photo
check, and the reasons they give. Every brand here is made up.
"""

from typing import Any

import pytest

from tests.unit.test_image_index import (
    TODAY,
    StubEmbedder,
    encode,
    fp,
    listing_at,
    page,
    product_photo,
    serve,
    serve_test_shop,
)
from unfret import brands, card, image_index, image_store, observations, shop_store, shopper, test_shop_photos
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.graph import SKILLS
from unfret.graph.skills import reverse_image
from unfret.graph.skills.reverse_image import ReverseImageSkill
from unfret.graph.state import Finding
from unfret.image_store import MemoryImageStore
from unfret.observations import CLIENT, DEEP, FETCHED, QUICK

PHOTO_URL = "https://cdn.glowlamp.shop/lamp.jpg"


@pytest.fixture(autouse=True)
def stub(monkeypatch: pytest.MonkeyPatch) -> MemoryImageStore:
    """The image index's test set-up (as in test_image_index): a stand-in model and an empty index."""
    monkeypatch.setattr(image_index, "_embedder", StubEmbedder())
    monkeypatch.setattr(image_index, "embedder", lambda: image_index._embedder)
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(shop_store, "today", lambda: TODAY)
    store = MemoryImageStore()
    reverse_image.use_store(store)
    return store


@pytest.fixture(autouse=True)
def no_wikidata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wikidata can't say (a test that needs it answers for it)."""

    async def unknown(_: str) -> None:
        return None

    monkeypatch.setattr(reverse_image, "on_wikidata", unknown)


# ── Brand names ──────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "key", "label"),
    [
        ("Brightora", "brightora", "Brightora"),
        ("BRIGHTORA™", "brightora", "BRIGHTORA"),
        ("Brightora® Official Store", "brightora", "Brightora"),
        ("  Brightora   Online Shop ", "brightora", "Brightora"),
        ("Velu-Nox GmbH", "velunox", "Velu-Nox"),
        ("Café Zorvi (R)", "cafezorvi", "Café Zorvi"),
        ("The Lampery Co", "thelampery", "The Lampery"),
        ("The Store", "thestore", "The Store"),  # never cut down to an article
    ],
)
def test_brand_names_are_compared_by_their_key(written: str, key: str, label: str) -> None:
    assert (brands.brand_key(written), brands.label(written)) == (key, label)


@pytest.mark.parametrize("written", ["", "  ", "Generic", "unbranded", "N/A", "No brand", "Store", "x", None])
def test_words_that_arent_a_brand_are_no_brand(written: str | None) -> None:
    assert brands.brand_key(written) is None and brands.label(written) is None


def test_a_missing_brand_is_unknown_never_different() -> None:
    assert brands.same_brand("Brightora", "BRIGHTORA™ Official Store") is True
    assert brands.same_brand("Brightora", "Lumiqzen") is False
    assert brands.same_brand(None, "Lumiqzen") is None
    assert brands.same_brand("Generic", "Lumiqzen") is None


def test_a_title_naming_the_brand_and_a_shop_named_brand() -> None:
    assert brands.named_in("Brightora", "Brightora Galaxy Lamp, 16 colours")
    assert brands.named_in("Bright Ora", "BRIGHTORA galaxy lamp")  # run together, for a longer name
    assert not brands.named_in("On", "Lemon squeezer")  # a short name only as a word
    assert brands.is_shop_name("Lampery", "lampery.nl") and brands.is_shop_name("Lampery NL", "lampery.nl")
    assert not brands.is_shop_name("Brightora", "lampery.nl")
    assert not brands.is_shop_name("VELUNOX", "unfret.net/test-shop/sister-shop")


# ── Kept with each photo ─────────────────────────────────────────────────────────────────────────────────────────


async def test_the_brand_follows_the_photo_and_reports_cant_change_a_fetched_one(stub: MemoryImageStore) -> None:
    photo, other = fp(product_photo(41)), fp(product_photo(42))
    where = listing_at("lampery.nl", 1)

    def brand() -> str | None:
        return stub.rows[(where.domain, where.listing_key)][0].label

    await stub.upsert(where, photo, image_url="u", day=TODAY, source="seen", label="Brightora")
    assert brand() == "Brightora"
    await stub.upsert(where, photo, image_url="u", day=TODAY, source="seen", label=None)
    assert brand() == "Brightora"  # a page naming no brand says nothing about it
    await stub.upsert(where, photo, image_url="u", day=TODAY, source="seen", label="Zorvi", bits=CLIENT, vouch=1)
    assert brand() == "Brightora"  # a report that doesn't count can't relabel what a fetched page showed
    await stub.upsert(where, photo, image_url="u", day=TODAY, source="seen", label="Zorvi", bits=FETCHED | DEEP)
    assert brand() == "Zorvi"  # a fetched page can
    await stub.upsert(where, other, image_url="u2", day=TODAY, source="seen", label=None)
    assert brand() is None  # a new photo comes with its own page's brand (none)

    reported = listing_at("forged.shop", 2)
    await stub.upsert(reported, photo, image_url="u", day=TODAY, source="seen", label=None, bits=CLIENT, vouch=1)
    await stub.upsert(reported, photo, image_url="u", day=TODAY, source="seen", label="Zorvi", bits=CLIENT, vouch=1)
    assert stub.rows[("forged.shop", reported.listing_key)][0].label == "Zorvi"  # fills in a missing brand
    await stub.upsert(reported, photo, image_url="u", day=TODAY, source="seen", label="Qelto", bits=QUICK, vouch=1)
    assert stub.rows[("forged.shop", reported.listing_key)][0].label is None  # reports that disagree: unknown


@pytest.mark.parametrize(
    ("action", "old", "new", "old_bits", "bits", "kept"),
    [
        ("insert", None, "A", 0, CLIENT, "A"),
        ("replace", "A", None, FETCHED, FETCHED, None),
        ("vouch", "A", "B", FETCHED, FETCHED, "B"),
        ("vouch", "A", None, FETCHED, FETCHED, "A"),
        ("vouch", "A", "B", FETCHED, CLIENT, "A"),
        ("vouch", None, "B", FETCHED, CLIENT, None),  # a fetched page named none: a report doesn't add one
        ("vouch", None, "B", CLIENT, CLIENT, "B"),
        ("vouch", "A", "B", CLIENT, CLIENT, None),
        ("vouch", "Acme", "ACME™", CLIENT, CLIENT, "Acme"),
        ("keep", "A", "B", FETCHED, CLIENT, "A"),
    ],
)
def test_which_brand_a_row_keeps(  # noqa: PLR0917 - the parameters
    action: observations.Action, old: str | None, new: str | None, old_bits: int, bits: int, kept: str | None
) -> None:
    assert image_store.label_after(action, old, new, old_bits=old_bits, bits=bits) == kept


async def test_the_photo_check_records_the_pages_brand_normalised(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, {PHOTO_URL: encode(product_photo(43), "JPEG", quality=70)})
    await ReverseImageSkill().run(branded("Brightora® Official Store"))
    await ReverseImageSkill().run(branded("Generic", "https://other.shop/products/lamp"))
    await reverse_image.drain()
    assert {d: row.label for (d, _), (row, *_) in stub.rows.items()} == {"glowlamp.shop": "Brightora",
                                                                       "other.shop": None}  # fmt: skip


# ── The photo check compares brands ──────────────────────────────────────────────────────────────────────────────


def branded(brand: str, url: str = "https://glowlamp.shop/products/lamp", title: str = "Galaxy lamp") -> Any:
    state = page(url, PHOTO_URL)
    state["facts"] = PageFacts(image_urls=[PHOTO_URL], brand=brand, title=title)
    return state


async def seen(store: MemoryImageStore, photo: Any, *rows: tuple[str, str | None]) -> None:
    for n, (domain, brand) in enumerate(rows, start=1):
        await store.upsert(listing_at(domain, n), fp(photo), image_url=f"https://{domain}/{n}.jpg", day=TODAY,
                           source="seen", label=brand)  # fmt: skip


@pytest.fixture
def photo(monkeypatch: pytest.MonkeyPatch) -> Any:
    image = product_photo(46)
    serve(monkeypatch, {PHOTO_URL: encode(image, "JPEG", quality=70)})
    return image


async def check(state: Any) -> Any:
    out = await ReverseImageSkill().run(state)
    await reverse_image.drain()
    return out


async def test_the_same_photo_under_another_brand_name(stub: MemoryImageStore, photo: Any) -> None:
    await seen(stub, photo, ("cheap-lamps.shop", "Zorvi"), ("budget.shop", "ZORVI™"), ("third.shop", None))

    out = await check(branded("Brightora"))

    assert out["features"]["reverse_image.other_brands"] == 1.0
    assert out["features"]["reverse_image.brand_photo_reused"] == 0.0
    (other,) = [f for f in out["findings"] if f.type == "photo_other_brands"]
    assert other.evidence == "The same product photo is sold under another brand name: Zorvi."
    assert out["same_product"]["brands"] == ["Zorvi"]
    assert out["same_product"]["detail"] == (
        "The main photo is the same photo as on cheap-lamps.shop and 2 other shops, under another brand name (Zorvi)."
    )


@pytest.mark.parametrize(
    ("brand", "rows", "title", "count"),
    [
        ("Brightora", [("a.shop", "Brightora™"), ("b.shop", "BRIGHTORA Official Store")], "Lamp", 0),  # same brand
        ("Brightora", [("a.shop", None), ("b.shop", "Generic")], "Lamp", 0),  # unknown, never different
        ("", [("a.shop", "Zorvi")], "Lamp", 0),  # the page names none: one name is no second brand
        ("", [("a.shop", "Zorvi"), ("b.shop", "Qelto")], "Lamp", 2),  # two other names: two brands
        ("Brightora", [("a.shop", "Zorvi")], "Zorvi galaxy lamp", 0),  # a reseller whose title names the brand
        ("Brightora", [("lampery.nl", "Lampery")], "Lamp", 0),  # a shop's own name in the brand field
        ("Glowlamp", [("a.shop", "Zorvi")], "Lamp", 0),  # the brand's own shop: the others are its resellers
    ],
)
async def test_only_names_that_are_really_another_brand_count(  # noqa: PLR0917 - the parameters
    stub: MemoryImageStore, photo: Any, brand: str, rows: list[tuple[str, str | None]], title: str, count: int
) -> None:
    await seen(stub, photo, *rows)
    out = await check(branded(brand, title=title))
    assert out["features"]["reverse_image.other_brands"] == count
    assert bool([f for f in out["findings"] if f.type == "photo_other_brands"]) is bool(count)


async def test_a_known_brands_catalogue_photo_under_another_brand(
    stub: MemoryImageStore, photo: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    async def on_wikidata(brand: str) -> bool:
        asked.append(brand)
        return brand == "Lumico"

    monkeypatch.setattr(reverse_image, "on_wikidata", on_wikidata)
    await stub.upsert(listing_at("amazon", 9), fp(photo), image_url="https://abo/9.jpg", day=TODAY, source="abo",
                      label="Lumico")  # fmt: skip

    out = await check(branded("Brightora"))
    assert out["features"]["reverse_image.brand_photo_reused"] == 1.0
    copied = next(f for f in out["findings"] if f.type == "brand_photo_reused")
    assert copied.evidence == (
        'The same photo is on an Amazon catalogue listing of the brand "Lumico", a known brand; this page names the '
        'brand "Brightora".'
    )
    assert asked == ["Lumico"]
    reseller = await check(branded("Brightora", title="Lumico galaxy lamp, sold by Brightora"))
    assert reseller["features"]["reverse_image.brand_photo_reused"] == 0.0  # names the real brand: no finding
    unbranded = await check(branded(""))
    assert unbranded["features"]["reverse_image.brand_photo_reused"] == 0.0  # names no other brand


async def test_an_unknown_catalogue_brand_is_only_another_brand_name(stub: MemoryImageStore, photo: Any) -> None:
    # Wikidata can't say (lookups off, or no answer): the catalogue brand isn't "known", only another name.
    await stub.upsert(listing_at("amazon", 9), fp(photo), image_url="https://abo/9.jpg", day=TODAY, source="abo",
                      label="Lumico")  # fmt: skip
    out = await check(branded("Brightora"))
    assert (out["features"]["reverse_image.other_brands"], out["features"]["reverse_image.brand_photo_reused"]) == (
        1.0, 0.0,
    )  # fmt: skip


async def test_on_a_marketplace_its_other_listings_are_compared_too(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = product_photo(45)
    url = "https://m.media-amazon.example/lamp.jpg"
    serve(monkeypatch, {url: encode(image, "JPEG", quality=70)})
    amazon = PageFacts(platform="amazon", image_urls=[url], brand="Brightora", title="Galaxy lamp")
    for n, brand in enumerate(("Zorvi", "Qelto", "Brightora"), start=1):
        await stub.upsert(listing_at("amazon", n), fp(image), image_url="u", day=TODAY, source="seen", label=brand)
    state = page("https://www.amazon.de/dp/B0TESTLAMP", url) | {"facts": amazon, "page_type": "aggregate_listing"}

    out = await check(state)

    assert out["features"]["reverse_image.other_shops"] == 0.0  # Amazon's own listings are no other shop
    assert out["features"]["reverse_image.other_brands"] == 2.0
    assert "same_product" not in out
    assert next(f for f in out["findings"] if f.type == "photo_other_brands").evidence == (
        "The same product photo is sold under 2 other brand names: Zorvi, Qelto."
    )


async def test_a_stock_photo_compares_no_brands(stub: MemoryImageStore, photo: Any) -> None:
    await seen(stub, photo, *((f"s{i}.shop", f"Brand{i}x") for i in range(image_store.STOP_DOMAINS + 1)))
    out = await check(branded("Brightora"))
    assert "reverse_image.other_brands" not in out["features"]


async def test_the_test_shop_shows_its_two_made_up_brands(
    stub: MemoryImageStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve_test_shop(monkeypatch)
    hero, sister = (f"https://unfret.net/static/test-shop/{p.photo}" for p in test_shop_photos.PAGES)
    shop = page("https://unfret.net/test-shop", hero) | {"facts": PageFacts(image_urls=[hero], brand="LUMIQZEN")}
    other = page("https://unfret.net/test-shop/sister-shop", sister) | {
        "facts": PageFacts(image_urls=[sister], brand="VELUNOX")
    }

    one, two = await check(shop), await check(other)

    assert [f.evidence for f in one["findings"] if f.type == "photo_other_brands"] == [
        "The same product photo is sold under another brand name: VELUNOX."
    ]
    assert [f.evidence for f in two["findings"] if f.type == "photo_other_brands"] == [
        "The same product photo is sold under another brand name: LUMIQZEN."
    ]
    assert one["same_product"]["detail"] == (
        "The main photo is the same photo as on unfret.net/test-shop/sister-shop, under another brand name (VELUNOX)."
        " Unfret saw it there for $17.99 on November 9, 2026."
    )
    assert stub.rows == {}  # nothing of the test shop is kept


# ── The reasons ──────────────────────────────────────────────────────────────────────────────────────────────────

RELABELLED = Finding(
    skill="reverse_image",
    type="photo_other_brands",
    evidence="The same product photo is sold under 2 other brand names: Zorvi, Qelto.",
    thumbnails=["https://unfret.net/v1/images/1/thumbnail.jpg"],
)
COPIED = Finding(
    skill="reverse_image",
    type="brand_photo_reused",
    evidence='The same photo is on an Amazon catalogue listing of the brand "Lumico", a known brand; this page names '
    'the brand "Brightora".',
    thumbnails=["https://unfret.net/v1/images/2/thumbnail.jpg"],
)


def verdict(score: int, features: dict[str, float], *findings: Finding, confidence: float = 0.9) -> shopper.Verdict:
    return shopper.build(
        score,
        features=features,
        findings=list(findings),
        facts=PageFacts(brand="Brightora"),
        page_type="storefront",
        confidence=confidence,
    )


def test_relabelled_goods_are_a_reason_without_other_signs_and_worth_a_look_at_best() -> None:
    out = verdict(90, {"reverse_image.other_brands": 2.0}, RELABELLED)
    assert out.band == "concerns"  # not "Looks fine", however good the score
    (reason,) = out.reasons
    assert (reason.type, reason.title) == ("rebranded_photo", "Same photo under other brand names")
    assert reason.detail == (
        "The main product photo is the same photo as on other listings that sell it under 2 other brand names: Zorvi, "
        "Qelto. The same product under several brand names is usually generic goods, relabelled."
    )
    assert verdict(60, {"reverse_image.other_brands": 2.0}, RELABELLED).band == "concerns"
    assert verdict(30, {"reverse_image.other_brands": 2.0}, RELABELLED).band == "careful"  # the scorer's own say


def test_a_known_brands_photo_is_think_twice_at_best_and_never_serious_by_itself() -> None:
    features = {"reverse_image.brand_photo_reused": 1.0, "reverse_image.other_brands": 1.0}
    out = verdict(95, features, COPIED, RELABELLED)
    assert out.band == "careful"
    assert [r.type for r in out.reasons] == ["brand_photo_reused"]  # one story: the stronger telling
    assert out.reasons[0].detail.startswith("The main product photo is the “Lumico” brand's own photo")
    assert verdict(60, features, COPIED).band == "careful"
    assert verdict(10, features, COPIED, confidence=0.1).band == "careful"  # "Serious concerns" stays the scorer's


@pytest.mark.parametrize(
    "known",
    [
        {"content.brand_recognized": 1.0},
        {"brand_presence.wikidata": 1.0},
        {"brand_presence.own_domain_age_years": 12.0},
        {"brand_presence.shop_is_brand": 1.0},
    ],
)
def test_never_for_a_brand_that_exists_beyond_the_page(known: dict[str, float]) -> None:
    features = {"reverse_image.brand_photo_reused": 1.0, "reverse_image.other_brands": 2.0, **known}
    out = verdict(90, features, COPIED, RELABELLED)
    assert out.band == "reliable" and not {r.type for r in out.reasons} & {"rebranded_photo", "brand_photo_reused"}


def test_a_reused_photo_is_no_warning_for_a_brand_on_wikidata() -> None:
    evidence = "The same product photo is on 2 other shops: a.shop, b.shop."
    photo = Finding(skill="reverse_image", type="photo_on_other_shops", evidence=evidence)
    features = {"reverse_image.other_shops": 2.0, "content.generic_product": 0.9}
    assert "photo_reused" in [r.type for r in verdict(30, features, photo).reasons]
    assert "photo_reused" not in [
        r.type for r in verdict(30, features | {"brand_presence.wikidata": 1.0}, photo).reasons
    ]


def test_the_card_names_the_index_and_the_product_area() -> None:
    features = {"reverse_image.brand_photo_reused": 1.0, "reverse_image.searched": 1.0}
    out = verdict(95, features, COPIED).model_dump()
    state: Any = {
        "page_type": "storefront",
        "features": features,
        "findings": [COPIED],
        "facts": PageFacts(brand="Brightora"),
        "same_product": {
            "title": "t",
            "detail": "d",
            "where": "a.shop",
            "image": "https://unfret.net/v1/images/2/t.jpg",
        },
    }
    done = card.complete(out, state, available=SKILLS, applicable=SKILLS)
    (reason,) = done["reasons"]
    assert reason["source"] == "Unfret's image index"
    assert reason["thumbnails"] == []  # the same-product block shows the photos
    product = next(i for i in done["checklist"] if i["key"] == "product")
    assert product["status"] == "concern"
    assert (
        done["next_step"]["text"]
        == "If you want the brand's product, buy it from the brand itself or a shop you know."
    )


# ── The corpus replay's report (evals.photo_brands) ──────────────────────────────────────────────────────────────


def test_the_photo_replay_reports_precision_and_every_band_it_changes(tmp_path: Any) -> None:
    from evals import photo_brands  # noqa: PLC0415 - the evaluation package

    def page_(label: str, before: str, after: str, reasons: list[str]) -> dict[str, Any]:
        relabelled = float("rebranded_photo" in reasons)
        bands = {"before": before, "after": after, "recorded_band": before}
        return {"label": label, "reasons": reasons, "other_brands": relabelled, "brand_photo_reused": 0.0,
                "other_shops": 1.0, **bands}  # fmt: skip

    per_page = {
        "a": page_("junk", "reliable", "concerns", ["rebranded_photo"]),
        "b": page_("legit", "reliable", "reliable", []),
        "c": page_("scam", "careful", "careful", ["rebranded_photo"]),
    }
    cache = photo_brands.PrintCache(tmp_path / "prints.jsonl")
    cache.add({"key": "https://x/1.jpg", "error": "TimeoutError"})
    report = photo_brands.summarise([], states={}, cache=cache, store=MemoryImageStore(), per_page=per_page)
    assert report["reasons"]["rebranded_photo"] == {"pages": 2, "labels": {"junk": 1, "scam": 1}, "precision": 1.0}
    assert report["band_changes"] == {"reliable -> concerns": 1}
    assert (report["new_warnings_on_legit"], report["new_serious"]) == (0, 0)
    assert report["photo_errors"] == {"TimeoutError": 1}
    assert photo_brands.PrintCache(tmp_path / "prints.jsonl").rows.keys() == {"https://x/1.jpg"}  # kept on disk
