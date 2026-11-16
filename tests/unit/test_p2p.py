"""Peer-to-peer ads: the platform extractors, the four ad skills, the photo check, the ad store and the verdict."""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from bs4 import BeautifulSoup
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, SecretStr

from unfret import ad_store, image_store, market_prices, shopper, shopper_p2p
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.facts import p2p as p2p_facts
from unfret.facts._parse import parse_since
from unfret.facts.p2p import ad_signals
from unfret.facts.schema import ListingDetails, SellerProfile
from unfret.graph import build_graph, nodes
from unfret.graph.skills import ANALYSED_PAGES, Skill, SkillOutput, Tier, content, seller_profile
from unfret.graph.skills.content import ContentSkill
from unfret.graph.skills.duplicate_ads import DuplicateAdsSkill, contact_hashes, drain
from unfret.graph.skills.injection import InjectionSkill
from unfret.graph.skills.off_platform import _ABROAD_RE, OffPlatformSkill, names_a_way_off
from unfret.graph.skills.price_outlier import PriceOutlierSkill, reference
from unfret.graph.skills.seller_profile import SellerProfileSkill
from unfret.graph.state import AnalysisState, Finding
from unfret.image_index import ImagePrint
from unfret.image_store import Listing, MemoryImageStore, lookup, lookup_ad

FIXTURES = Path(__file__).parents[1] / "fixtures" / "p2p"
TODAY = date(2026, 11, 15)
URLS = {
    "marktplaats-iphone": "https://www.marktplaats.nl/v/telecommunicatie/mobiele-telefoons-apple-iphone/"
    "m2201234567-apple-iphone-15-pro-256gb-zwart",
    "2dehands-fiets": "https://www.2dehands.be/v/fietsen-en-brommers/fietsen-heren-herenfietsen/"
    "m1987654321-gazelle-orange-c7-herenfiets",
    "ebay-camera": "https://www.ebay.com/itm/256712345678",
    "vinted-jas": "https://www.vinted.nl/items/5123456789-winterjas-only-maat-m",
    "craigslist-apartment": "https://sfbay.craigslist.org/sfc/apa/d/san-francisco-sunny-2br-apartment/7801234567.html",
    "facebook-ps5": "https://www.facebook.com/marketplace/item/123456789012345/",
    "marktplaats-config": "https://www.marktplaats.nl/v/fietsen-en-brommers/fietsen-dames-damesfietsen/"
    "m2400000001-gazelle-damesfiets-opknapper",
}
# The sellers' names (and the visitor's) in the fixtures: none may end up in the facts.
NAMES = ("Kevin", "Marleen", "pdx_photo_guy", "lisa_closet", "Thomas van Dijk", "Jane", "jane.doe@example.org")


def facts_of(name: str) -> PageFacts:
    facts, _ = parse_page((FIXTURES / f"{name}.html").read_text(), URLS[name])
    return facts


@pytest.fixture(autouse=True)
def fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seller_profile, "today", lambda: TODAY)
    monkeypatch.setattr(p2p_facts, "_today", lambda: TODAY)
    monkeypatch.setattr("unfret.shop_store.today", lambda: TODAY)


@pytest.fixture(autouse=True)
def fresh_stores(monkeypatch: pytest.MonkeyPatch) -> ad_store.MemoryAdStore:
    store = ad_store.MemoryAdStore()
    monkeypatch.setattr(ad_store, "_store", store)
    monkeypatch.setattr(ad_store, "_purged_on", None)
    monkeypatch.setattr(market_prices, "_store", market_prices.MemoryMarketStore())
    return store


@pytest.fixture
def contact_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr("test-contact-key"))


def state_of(facts: PageFacts, text: str = "") -> Any:
    return {"facts": facts, "cleaned_content": text, "url": facts.url, "page_type": "p2p_listing", "flags": []}


# ── Extraction ──────────────────────────────────────────────────────────────────────────────────────────────


def test_marktplaats_ad_and_seller() -> None:
    facts = facts_of("marktplaats-iphone")

    assert (facts.platform, facts.price, facts.currency, facts.seller) == ("marktplaats", 350.0, "EUR", "")
    listing, profile = facts.listing, facts.seller_profile
    assert listing is not None and profile is not None
    assert listing.condition == "Zo goed als nieuw"
    assert listing.listed == date(2026, 11, 13)
    assert listing.location == "Amsterdam"
    assert listing.delivery == ["shipping"]
    assert listing.contact_methods == ["whatsapp", "phone"]
    assert listing.payment_requests == ["bank_transfer", "deposit"]
    assert listing.description.startswith("Te koop mijn iPhone 15 Pro")
    assert "Betaal nooit vooruit" not in listing.description  # the platform's safety tips aren't the ad
    assert "Telecommunicatie" in listing.category
    assert (profile.member_since, profile.rating_count, profile.other_listings) == (date(2026, 10, 1), 0, 23)
    assert profile.badges == []


def test_2dehands_ad_and_seller() -> None:
    facts = facts_of("2dehands-fiets")

    listing, profile = facts.listing, facts.seller_profile
    assert listing is not None and profile is not None
    assert (listing.bidding, listing.condition, listing.listed) == (True, "Gebruikt", date(2026, 11, 2))
    assert listing.delivery == ["shipping", "pickup"]
    assert listing.payment_protection is True
    assert listing.payment_requests == []  # "Gazelle" is no Zelle, "contant bij ophalen" no request
    assert (profile.member_since, profile.rating, profile.rating_count) == (date(2014, 1, 1), 4.8, 48)
    assert profile.badges == ["Verified phone"]
    assert profile.response_time == "Reageert meestal binnen een uur"


def test_ebay_private_seller() -> None:
    facts = facts_of("ebay-camera")

    listing, profile = facts.listing, facts.seller_profile
    assert listing is not None and profile is not None
    assert (facts.price, facts.currency, facts.seller_rating) == (649.0, "USD", 99.8)
    assert (listing.condition, listing.negotiable, listing.payment_protection) == ("Used", True, True)
    assert listing.location == "Portland, Oregon, United States"
    assert listing.delivery == ["shipping", "pickup"]
    assert (profile.seller_type, profile.member_since, profile.rating_count) == ("private", date(2012, 3, 1), 1234)
    assert profile.response_time == "Usually responds within 24 hours"


def test_vinted_item() -> None:
    facts = facts_of("vinted-jas")

    listing, profile = facts.listing, facts.seller_profile
    assert listing is not None and profile is not None
    assert (listing.condition, listing.location, listing.delivery) == ("Zeer goed", "Utrecht, Nederland", ["shipping"])
    assert listing.payment_protection is True
    assert listing.category == "Dames > Bovenkleding > Jassen"
    assert (profile.member_since, profile.rating, profile.rating_count) == (date(2019, 3, 4), 4.9, 131)
    assert (profile.badges, profile.other_listings) == (["Verified e-mail"], 42)


def test_craigslist_posting_has_no_profile() -> None:
    facts = facts_of("craigslist-apartment")

    listing = facts.listing
    assert listing is not None and facts.seller_profile is None
    assert facts.title == "Sunny 2BR apartment, Mission district"
    assert (facts.price, listing.listed, listing.location) == (1100.0, date(2026, 11, 12), "mission district")
    assert listing.payment_protection is False
    assert listing.contact_methods == ["email"]
    assert listing.payment_requests == ["deposit", "payment_app"]
    assert listing.description.startswith("Beautiful sunny")  # Craigslist's print label is dropped
    assert "apts/housing for rent" in listing.category


def test_facebook_marketplace_by_its_headings() -> None:
    facts = facts_of("facebook-ps5")

    listing, profile = facts.listing, facts.seller_profile
    assert listing is not None and profile is not None
    assert (facts.price, listing.condition, listing.location) == (280.0, "Used - like new", "Utrecht, Netherlands")
    assert listing.delivery == ["pickup"]
    assert listing.description.startswith("PS5 disc edition")
    assert (profile.member_since, profile.rating, profile.rating_count) == (date(2011, 1, 1), 4.9, 27)
    assert profile.badges == ["Highly rated"]


@pytest.mark.parametrize("name", list(URLS))
def test_no_seller_or_visitor_name_is_kept(name: str) -> None:
    dumped = facts_of(name).model_dump_json()
    for needle in NAMES:
        assert needle not in dumped, needle


def test_missing_areas_stay_empty() -> None:
    facts, _ = parse_page("<html><body><h1>Fiets</h1></body></html>", URLS["marktplaats-iphone"])

    assert facts.seller_profile is None
    assert facts.listing == ListingDetails()


def test_the_extension_s_facts_are_taken_as_sent() -> None:
    sent = PageFacts(
        platform="facebook",
        url=URLS["facebook-ps5"],
        title="PS5",
        price=120.0,
        currency="EUR",
        listing=ListingDetails(description="Only shipping, pay a deposit by bank transfer first."),
        seller_profile=SellerProfile(member_since=TODAY - timedelta(days=10), rating_count=0),
    )
    assert PageFacts.model_validate_json(sent.model_dump_json()) == sent


@pytest.mark.parametrize(
    ("text", "contacts", "payments"),
    [
        ("Stuur me een appje op 06-12345678", ["whatsapp", "phone"], []),
        ("Mail me: seller@example.com", ["email"], []),
        ("Betaal nooit vooruit. Geen aanbetaling nodig.", [], []),
        ("Payment by Zelle or gift cards only", [], ["gift_card", "payment_app"]),
        ("Aanbetaling van 50% via Tikkie", [], ["payment_link", "deposit"]),
        ("Send BTC or USDT to my wallet", [], ["crypto"]),
        ("IBAN NL91ABNA0417164300 voor de overboeking", [], ["bank_transfer"]),
    ],
)
def test_ad_text_signals(text: str, contacts: list[str], payments: list[str]) -> None:
    assert ad_signals(text) == (contacts, payments)


@pytest.mark.parametrize(
    ("text", "day"),
    [("12 maart 2019", date(2019, 3, 12)), ("Mar 2015", date(2015, 3, 1)), ("2015", date(2015, 1, 1)), ("x", None)],
)
def test_member_since(text: str, day: date | None) -> None:
    assert parse_since(text) == day


# ── Skills ──────────────────────────────────────────────────────────────────────────────────────────────────


async def test_seller_profile_of_a_new_account() -> None:
    out = await SellerProfileSkill().run(state_of(facts_of("marktplaats-iphone")))

    f = out["features"]
    assert f["seller_profile.new_account"] == 1 and f["seller_profile.no_ratings"] == 1
    assert f["seller_profile.listing_burst"] == 1  # 23 ads on a 45-day-old account
    types = [x.type for x in out["findings"]]
    assert types == ["new_account", "no_ratings", "listing_burst", "unverified"]
    assert "45 days old" in out["findings"][0].evidence


async def test_seller_profile_of_an_established_seller() -> None:
    out = await SellerProfileSkill().run(state_of(facts_of("vinted-jas")))

    f = out["features"]
    assert (f["seller_profile.established"], f["seller_profile.well_rated"], f["seller_profile.verified"]) == (1, 1, 1)
    assert out["findings"] == []


async def test_no_profile_no_findings() -> None:
    out = await SellerProfileSkill().run(state_of(facts_of("craigslist-apartment")))

    assert out["features"] == {"seller_profile.shown": 0.0}
    assert out["findings"] == []


async def test_seller_profile_low_and_slow() -> None:
    profile = SellerProfile(
        member_since=date(2020, 1, 1), rating=2.4, rating_count=9, response_time="Responds within a few days"
    )
    out = await SellerProfileSkill().run(state_of(PageFacts(platform="vinted", seller_profile=profile)))

    assert {x.type for x in out["findings"]} == {"low_rating", "slow_responses"}


async def test_off_platform_quotes_the_ad() -> None:
    out = await OffPlatformSkill().run(state_of(facts_of("marktplaats-iphone")))

    f = out["features"]
    fired = {k.removeprefix("off_platform.") for k, v in f.items() if v == 1 and k != "off_platform.signals"}
    assert fired == {
        "text_read",
        "contact_outside",
        "bank_transfer",
        "advance_payment",
        "shipping_only",
        "abroad",
        "protection_refused",
    }
    by_type = {x.type: x.evidence for x in out["findings"]}
    assert "Betaling vooraf via overboeking" in by_type["advance_payment"]
    assert "WhatsApp" in by_type["contact_outside"]
    assert "Geen gelijk oversteken" in by_type["protection_refused"]
    assert "buitenland" in by_type["abroad"]


@pytest.mark.parametrize("name", ["2dehands-fiets", "ebay-camera", "vinted-jas", "facebook-ps5"])
async def test_off_platform_leaves_honest_ads_alone(name: str) -> None:
    out = await OffPlatformSkill().run(state_of(facts_of(name)))

    assert out["findings"] == []  # Facebook's "Cash or Tikkie at pickup" is paying on the spot


async def test_off_platform_rental_story() -> None:
    out = await OffPlatformSkill().run(state_of(facts_of("craigslist-apartment")))

    types = {x.type for x in out["findings"]}
    assert {"advance_payment", "untraceable_payment", "abroad", "contact_outside", "shipping_only"} <= types


async def test_without_the_ad_text_the_platform_tips_dont_count() -> None:
    page = "Deel via WhatsApp. Veilig handelen: betaal nooit vooruit en maak nooit geld over via een betaalverzoek."
    out = await OffPlatformSkill().run(state_of(PageFacts(platform="marktplaats"), page))

    assert out["findings"] == []
    assert out["features"]["off_platform.text_read"] == 0


@pytest.mark.parametrize(
    ("title", "category", "expected"),
    [
        ("Apple iPhone 15 Pro 256GB", "", ("an iPhone 15", 250)),
        ("iPhone 15 hoesje", "", None),
        ("iPhone 15 defect", "", None),
        ("Gezocht: PS5", "", None),
        ("PlayStation 5 Disc Edition + 2 controllers", "", ("a PlayStation 5", 180)),
        ("PS5 controller", "", None),
        ("Sunny 2BR apartment", "apts/housing for rent", ("a home for rent", 500)),
        ("Kamer te huur Utrecht", "Huizen en Kamers", ("a room for rent", 250)),
        ("Gazelle damesfiets", "Fietsen", None),
    ],
)
def test_price_references(title: str, category: str, expected: tuple[str, float] | None) -> None:
    assert reference(title, category) == expected


async def test_price_far_below_the_usual() -> None:
    facts = PageFacts(platform="marktplaats", title="iPhone 15 128GB", price=120.0, currency="EUR")

    out = await PriceOutlierSkill().run(state_of(facts))

    assert out["features"]["price_outlier.far_below"] == 1
    assert out["findings"][0].evidence == ("Asking €120 for an iPhone 15: honest ads for one rarely go under €250.")


async def test_price_not_compared_without_a_reference() -> None:
    for facts in (
        PageFacts(platform="marktplaats", title="Gazelle fiets", price=50.0, currency="EUR"),
        PageFacts(platform="marktplaats", title="iPhone 15", price=5.0, currency="EUR"),
        PageFacts(platform="marktplaats", title="iPhone 15", price=120.0, currency="PLN"),
    ):
        out = await PriceOutlierSkill().run(state_of(facts))
        assert out["features"] == {"price_outlier.checked": 0.0}


async def test_price_against_the_same_product_new() -> None:
    product = {"@type": "Product", "gtin13": "8710103917052", "offers": {"price": "400.00"}}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(market_prices, "today", lambda: TODAY)
        for shop, price in (("a.example", 380.0), ("b.example", 420.0)):
            facts = PageFacts(platform="generic", price=price, currency="EUR", json_ld=[product])
            await market_prices.observe(f"https://{shop}/p/1", facts)
    ad = PageFacts(
        platform="ebay",
        title="Philips airfryer",
        price=90.0,
        currency="EUR",
        json_ld=[product],
        listing=ListingDetails(condition="New"),
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(market_prices, "today", lambda: TODAY)
        out = await PriceOutlierSkill().run(state_of(ad))
        rows = len(market_prices._store.rows)  # type: ignore[attr-defined]

    assert out["features"]["price_outlier.market"] == 1 and out["features"]["price_outlier.far_below"] == 1
    assert "€400" in out["findings"][0].evidence and "2 shops" in out["findings"][0].evidence
    assert rows == 2  # the ad's own price is never recorded


def _ad(url: str, text: str, platform: str = "marktplaats") -> PageFacts:
    return PageFacts(platform=platform, url=url, listing=ListingDetails(description=text))  # type: ignore[arg-type]


TEXT = (
    "Te koop mijn iPhone 15 Pro 256GB, zo goed als nieuw, altijd in een hoesje gezeten. Doos en lader erbij. "
    "Alleen verzenden. Bel of app 06-12345678."
)


async def test_duplicate_ads_are_found_across_listings(contact_key: None) -> None:
    skill = DuplicateAdsSkill()
    await skill.run(state_of(_ad(URLS["marktplaats-iphone"], TEXT)))
    await drain()

    copy = TEXT.replace("256GB", "128GB").replace("06-12345678", "+31 6 1234 5678")
    out = await skill.run(state_of(_ad("https://www.2dehands.be/v/telefoons/m1112223334-iphone", copy, "2dehands")))
    await drain()

    f = out["features"]
    assert (f["duplicate_ads.text_copies"], f["duplicate_ads.contact_ads"]) == (1, 1)
    assert [x.type for x in out["findings"]] == ["duplicate_text", "shared_contact"]
    assert "(Marktplaats)" in out["findings"][0].evidence


async def test_the_same_ad_again_is_not_a_copy(contact_key: None) -> None:
    skill = DuplicateAdsSkill()
    for _ in range(2):
        out = await skill.run(state_of(_ad(URLS["marktplaats-iphone"], TEXT)))
        await drain()

    assert out["features"]["duplicate_ads.text_copies"] == 0
    assert out["findings"] == []


async def test_contacts_are_only_kept_as_keyed_hashes(fresh_stores: ad_store.MemoryAdStore, contact_key: None) -> None:
    await DuplicateAdsSkill().run(state_of(_ad(URLS["marktplaats-iphone"], TEXT)))
    await drain()

    stored = repr(fresh_stores.ads) + repr(fresh_stores.contacts)
    for needle in ("12345678", "iPhone", "marktplaats.nl", "m2201234567"):
        assert needle not in stored
    assert len(fresh_stores.contacts) == 1
    assert all(len(c) == 32 for c, _ in fresh_stores.contacts)


async def test_without_a_key_no_contacts_are_kept(fresh_stores: ad_store.MemoryAdStore) -> None:
    assert contact_hashes(TEXT) == ()
    await DuplicateAdsSkill().run(state_of(_ad(URLS["marktplaats-iphone"], TEXT)))
    await drain()

    assert fresh_stores.contacts == {} and len(fresh_stores.ads) == 1


async def test_ads_are_kept_90_days(fresh_stores: ad_store.MemoryAdStore) -> None:
    old = ad_store.AdPrint("marktplaats", b"o" * 32, ad_store.simhash(TEXT), (b"c" * 32,))
    fresh = ad_store.AdPrint("marktplaats", b"f" * 32, ad_store.simhash(TEXT), (b"c" * 32,))
    await fresh_stores.record(old, TODAY - timedelta(days=ad_store.RETENTION_DAYS + 1))
    await fresh_stores.record(fresh, TODAY - timedelta(days=ad_store.RETENTION_DAYS))

    seen = await ad_store.seen(ad_store.AdPrint("vinted", b"n" * 32, ad_store.simhash(TEXT), (b"c" * 32,)), TODAY)
    assert seen is not None and (seen.copies, seen.contact_ads) == (1, 1)  # the expired one doesn't count

    await ad_store.record(ad_store.AdPrint("vinted", b"n" * 32, None), TODAY)  # purges once a day
    assert {k for _, k in fresh_stores.ads} == {b"f" * 32, b"n" * 32}
    assert {k for _, k in fresh_stores.contacts} == {b"f" * 32}


async def test_a_number_written_nationally_can_be_removed_on_request(
    fresh_stores: ad_store.MemoryAdStore, contact_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx  # noqa: PLC0415 - only this test talks to the app

    from unfret.main import app  # noqa: PLC0415 - keep the app out of import time for the other tests

    await DuplicateAdsSkill().run(state_of(_ad(URLS["marktplaats-iphone"], TEXT)))
    await drain()
    assert len(fresh_stores.contacts) == 1
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    auth = {"Authorization": "Bearer operator-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"kind": "phone", "value": "06 1234 5678"}
        response = await client.post("/v1/admin/contacts/forget", json=body, headers=auth)

    assert response.json() == {"removed": 1}
    assert fresh_stores.contacts == {}


def test_simhash_tolerates_small_edits_only() -> None:
    a, b = ad_store.simhash(TEXT), ad_store.simhash(TEXT.replace("verzenden.", "verzenden vanuit Rotterdam."))
    other = ad_store.simhash(
        "Gazelle damesfiets in goede staat, 7 versnellingen, nieuwe banden en een slot erbij, ophalen"
    )
    assert a is not None and b is not None and other is not None
    assert ad_store.distance(a, b) <= ad_store.SIMHASH_DISTANCE < ad_store.distance(a, other)
    assert ad_store.simhash("Zo goed als nieuw, ophalen") is None


def test_phone_numbers_compare_however_written() -> None:
    assert ad_store.comparable_phone("06-12345678") == ad_store.comparable_phone("+31 6 1234 5678") == "+31612345678"
    assert ad_store.comparable_phone("0470 12 34 56") == "+32470123456"
    assert ad_store.comparable_phone("123") is None


# ── The photo check ─────────────────────────────────────────────────────────────────────────────────────────


def _print(phash: int) -> ImagePrint:
    embedding = bytearray(384)
    embedding[0] = 127
    return ImagePrint(phash, phash, bytes(embedding), bytes(48))


async def test_an_ad_photo_matches_other_ads_and_shops_but_a_shop_photo_never_matches_ads() -> None:
    store = MemoryImageStore()
    photo = _print(0x0F0F_0F0F_0F0F_0F0F)
    ad_a = image_store.listing(URLS["marktplaats-iphone"], PageFacts(platform="marktplaats"))
    ad_b = image_store.listing("https://www.marktplaats.nl/v/x/m1234567890-iphone", PageFacts(platform="marktplaats"))
    shop = Listing("phones.example", b"s" * 32)
    assert ad_a is not None and ad_b is not None and ad_a.domain == "marktplaats"
    for where in (ad_a, shop):
        await store.upsert(where, photo, image_url="https://img/1.jpg", day=TODAY, source="seen")

    found = await lookup_ad(store, photo, ad_b)
    assert [(m.domain, image_store.is_ad(m)) for m in found.matches] == [
        ("marktplaats", True),
        ("phones.example", False),
    ]
    assert [m.domain for m in (await lookup(store, photo, Listing("other.example", b"x" * 32))).matches] == [
        "phones.example"
    ]


async def test_ad_photos_are_kept_90_days() -> None:
    store = MemoryImageStore()
    ad = image_store.listing(URLS["vinted-jas"], PageFacts(platform="vinted"))
    assert ad is not None
    old = TODAY - timedelta(days=image_store.AD_RETENTION_DAYS + 1)
    await store.upsert(ad, _print(1), image_url="u", day=old, source="seen")
    await store.upsert(Listing("shop.example", b"s" * 32), _print(2), image_url="u", day=old, source="seen")

    await store.purge(TODAY)

    assert [r.domain for r, *_ in store.rows.values()] == ["shop.example"]


# ── The verdict ─────────────────────────────────────────────────────────────────────────────────────────────


async def _verdict(facts: PageFacts, *extra: SkillOutput) -> tuple[int, shopper.Verdict]:
    outs = [
        await SellerProfileSkill().run(state_of(facts)),
        await OffPlatformSkill().run(state_of(facts)),
        await PriceOutlierSkill().run(state_of(facts)),
        *extra,
    ]
    features = {k: v for o in outs for k, v in o["features"].items()}
    findings = [x for o in outs for x in o["findings"]]
    return shopper_p2p.build(features=features, findings=findings, facts=facts)


async def test_off_platform_payment_on_a_new_account_is_many_warning_signs() -> None:
    score, verdict = await _verdict(facts_of("marktplaats-iphone"))

    assert verdict.band == "junk_or_scam" and score < settings.verdict_careful_min
    assert verdict.label == "Many warning signs"
    assert [r.severity for r in verdict.reasons] == ["high", "high", "high"]
    assert "“Betaling vooraf via overboeking" in verdict.reasons[0].detail
    words = [verdict.label, verdict.headline, *(f"{r.title} {r.detail}" for r in verdict.reasons)]
    assert "scam" not in " ".join(words).lower()
    assert verdict.positives == []


async def test_the_rental_story_is_many_warning_signs() -> None:
    _, verdict = await _verdict(facts_of("craigslist-apartment"))

    assert verdict.band == "junk_or_scam"  # deposit by Zelle + "out of the country" + won't show it


async def test_stolen_photo_and_a_bait_price_are_many_warning_signs() -> None:
    facts = PageFacts(
        platform="marktplaats", url=URLS["marktplaats-iphone"], title="iPhone 15", price=120.0, currency="EUR"
    )
    photo = Finding(skill="reverse_image", type="photo_in_other_ads", evidence="… in 2 other ads … (Marktplaats).")
    extra: SkillOutput = {"features": {"reverse_image.other_ads": 2.0}, "findings": [photo], "skill_results": []}

    _, verdict = await _verdict(facts, extra)

    assert verdict.band == "junk_or_scam"
    assert {r.type for r in verdict.reasons} >= {"p2p_photo_in_other_ads", "p2p_price_far_below"}


async def test_one_serious_sign_alone_is_some_warning_signs() -> None:
    facts = _ad(URLS["vinted-jas"], "Mooie jas. Betalen alleen via cadeaukaart.", "vinted")
    facts = facts.model_copy(update={"seller_profile": facts_of("vinted-jas").seller_profile})

    _, verdict = await _verdict(facts)

    assert verdict.band == "careful"
    assert verdict.reasons[0].type == "p2p_untraceable_payment"
    assert "gift cards" in verdict.reasons[0].detail


async def test_established_well_rated_and_protected_looks_fine() -> None:
    for name in ("vinted-jas", "ebay-camera", "2dehands-fiets"):
        _, verdict = await _verdict(facts_of(name))
        assert verdict.band == "reliable", (name, verdict)
        assert verdict.reasons == []
        assert {p.type for p in verdict.positives} >= {"p2p_established", "p2p_well_rated"}


async def test_pickup_with_payment_on_the_spot_counts_as_a_safe_way_to_pay() -> None:
    _, verdict = await _verdict(facts_of("facebook-ps5"))

    assert verdict.band == "reliable"
    assert {p.type for p in verdict.positives} == {"p2p_established", "p2p_well_rated", "p2p_pickup"}


async def test_a_transfer_before_shipping_is_a_sign() -> None:
    facts = _ad(URLS["vinted-jas"], "Mooie jas. Betalen via overboeking, daarna verstuur ik hem.", "vinted")

    _, verdict = await _verdict(facts)

    assert verdict.band == "concerns"
    assert [r.type for r in verdict.reasons] == ["p2p_bank_transfer"]


@pytest.mark.parametrize(
    "facts",
    [
        PageFacts(platform="marktplaats"),  # nothing known at all
        _ad(URLS["vinted-jas"], "Mooie jas, maat M, twee winters gedragen, geen vlekken.", "vinted"),  # no profile
        PageFacts(  # established and rated, but no platform payment, pickup or price check
            platform="craigslist",
            seller_profile=SellerProfile(member_since=date(2015, 1, 1), rating=4.9, rating_count=40),
            listing=ListingDetails(description="Bike in good condition, ships anywhere."),
        ),
        PageFacts(  # protected and rated, but a young account
            platform="vinted",
            seller_profile=SellerProfile(member_since=date(2026, 6, 1), rating=5, rating_count=12),
            listing=ListingDetails(description="Mooie jas.", payment_protection=True),
        ),
    ],
)
async def test_never_looks_fine_without_concrete_positives(facts: PageFacts) -> None:
    score, verdict = await _verdict(facts)

    assert verdict.band == "concerns" and settings.verdict_concerns_min <= score < settings.verdict_reliable_min
    assert verdict.reasons, "a band below 'Looks fine' always says why"
    assert verdict.headline


async def test_little_to_go_on_says_so() -> None:
    _, verdict = await _verdict(PageFacts(platform="marktplaats"))

    assert [r.type for r in verdict.reasons] == ["p2p_little_known"]
    assert verdict.headline == "A few things to check: little to go on."


def test_ad_copy_is_complete_and_plain() -> None:
    copy = shopper.catalogue()
    keys = [k for k in shopper.REASON_SEVERITY if k.startswith("p2p_")]
    assert keys and all(k in copy["reasons"] for k in keys)
    assert all(k in copy["positives"] for k in shopper.POSITIVE_KEYS if k.startswith("p2p_"))
    assert set(copy["class_headlines"]["p2p_listing"]) == {"reliable", "concerns", "careful", "junk_or_scam"}
    text = json.dumps({k: copy["reasons"][k] for k in keys}).lower()
    assert "scam" not in text and "fraud" not in text


# ── The graph ───────────────────────────────────────────────────────────────────────────────────────────────


class _Photo(Skill):
    """Stands in for the reverse image search."""

    name = "reverse_image"
    description = "photo"
    tier: Tier = "expensive"
    always_run = False
    page_types = ANALYSED_PAGES
    calls: ClassVar[list[str]] = []

    async def run(self, state: AnalysisState) -> SkillOutput:
        type(self).calls.append(state["page_type"])
        return self.output({"searched": True, "other_ads": 0})


@pytest.fixture(autouse=True)
def judge_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Product pages (the flag off) get a canned headline; ads are classified and judged without the LLM."""

    def structured(schema: type[BaseModel]) -> RunnableLambda:
        assert schema is nodes._JudgeOutput, schema
        return RunnableLambda(lambda _: nodes._JudgeOutput(score=70, explanation="Fine."))

    monkeypatch.setattr(nodes, "get_structured_llm", structured)


def _graph() -> Any:
    _Photo.calls = []
    skills = [SellerProfileSkill(), OffPlatformSkill(), PriceOutlierSkill(), DuplicateAdsSkill(), InjectionSkill()]
    return build_graph([*skills, _Photo()])


def _input(name: str) -> dict[str, Any]:
    return {"page_content": (FIXTURES / f"{name}.html").read_text(), "url": URLS[name], "flags": []}


async def test_the_p2p_class_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", True)

    result = await _graph().ainvoke(_input("marktplaats-iphone"))
    await drain()

    assert result["page_type"] == "p2p_listing"
    assert result["verdict"]["band"] == "junk_or_scam"
    assert result["explanation"] == result["verdict"]["headline"]
    assert _Photo.calls == ["p2p_listing"]  # the photo check runs for an ad with a photo
    assert "Kevin" not in result["facts"].model_dump_json()


async def test_no_photo_no_photo_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", True)
    facts = PageFacts(platform="vinted", url=URLS["vinted-jas"], title="Jas", price=18.0, currency="EUR")

    result = await _graph().ainvoke({"page_content": "", "url": facts.url, "flags": [], "page_facts": facts})

    assert result["page_type"] == "p2p_listing" and _Photo.calls == []


async def test_without_the_flag_ads_stay_product_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", False)

    result = await _graph().ainvoke(_input("marktplaats-iphone"))

    assert result["page_type"] == "storefront"
    assert not any(k.startswith(("seller_profile.", "off_platform.")) for k in result["features"])


async def test_the_p2p_skills_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from unfret.routes import analyze  # noqa: PLC0415 - keep the app out of import time for the other tests

    monkeypatch.setattr(settings, "expansion_classes", True)
    final: dict[str, Any] = {}
    events: list[str] = []
    async for _, update in _graph().astream(_input("vinted-jas"), stream_mode="updates", subgraphs=True):
        assert isinstance(update, dict)
        events += analyze._update_events(update, final)
    await drain()

    steps = "\n".join(e for e in events if e.startswith("event: step"))
    for skill in ("seller_profile", "off_platform", "price_outlier", "duplicate_ads", "reverse_image"):
        assert f'"skill": "{skill}"' in steps, skill
    assert final["verdict"]["band"] == "reliable"


# ── The content read ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("page_type", ["p2p_listing", "storefront"])
async def test_only_ads_are_asked_about_the_off_platform_push(monkeypatch: pytest.MonkeyPatch, page_type: str) -> None:
    asked: list[tuple[type[BaseModel], str]] = []

    def structured(schema: type[BaseModel]) -> RunnableLambda:
        def respond(messages: Any) -> BaseModel:
            asked.append((schema, messages[0].content))
            fields: dict[str, object] = {
                "scam_copy": 0.1,
                "generic_product": 0.0,
                "brand_recognized": False,
                "implausible_claims": 0.0,
                "missing_trust_info": 0.0,
                "addresses_the_reader_ai": False,
            }
            if schema is content._AdSignals:
                fields |= {"off_platform_push": 0.9, "off_platform_quote": "Stuur me een WhatsApp"}
            return schema(**fields)

        return RunnableLambda(respond)

    monkeypatch.setattr(content, "get_structured_llm", structured)
    state = state_of(PageFacts(title="iPhone"), "Stuur me een WhatsApp") | {"page_type": page_type}

    out = await ContentSkill().run(state)

    ((schema, system),) = asked
    if page_type == "p2p_listing":
        assert schema is content._AdSignals and "off_platform_push" in system
        assert out["features"]["content.off_platform_push"] == 0.9
        assert [f.type for f in out["findings"]] == ["off_platform_quote"]
    else:
        assert schema is content._Signals and "off_platform_push" not in system  # product pages as before
        assert "content.off_platform_push" not in out["features"]


def test_facts_only_ads_give_the_llm_the_ad_text() -> None:
    facts = PageFacts(title="Fiets", listing=ListingDetails(description="Alleen verzenden."))

    assert "Ad text: Alleen verzenden." in nodes._facts_text(facts)


@pytest.mark.parametrize(
    ("text", "abroad"),
    [
        ("Ik woon momenteel in het buitenland voor mijn werk.", True),
        ("Bezichtiging kan niet omdat ik in het buitenland zit.", True),
        ("Ik werk tijdelijk in het buitenland.", True),
        ("De telefoon is op de releasedatum in het buitenland aangeschaft.", False),
        ("We hebben in het buitenland gekocht, met bon.", False),
        ("De onderdelen zijn in het buitenland gemaakt.", False),
    ],
)
def test_abroad_means_the_seller_not_where_the_item_was_bought(text: str, abroad: bool) -> None:
    assert (_ABROAD_RE.search(text) is not None) == abroad


# ── Marktplaats and 2dehands: the ad's JSON ─────────────────────────────────────────────────────────────────


def test_marktplaats_ad_from_its_json() -> None:
    facts = facts_of("marktplaats-config")

    listing, profile = facts.listing, facts.seller_profile
    assert listing is not None and profile is not None
    assert (facts.price, facts.currency, facts.title) == (135.0, "EUR", "Gazelle damesfiets, opknapper")
    assert (listing.bidding, listing.delivery, listing.listed) == (True, ["pickup"], date(2026, 11, 10))
    assert (listing.location, listing.category) == ("Groningen", "Fietsen en Brommers > Damesfietsen")
    assert listing.payment_protection is None  # "not offered" in the JSON is no claim either way
    assert (profile.seller_type, profile.member_since) == ("private", TODAY - timedelta(days=11 * 365))
    assert len(facts.image_urls) == 2 and all("$_85" in u for u in facts.image_urls)


def test_no_name_or_profile_link_survives_the_json() -> None:
    html = (FIXTURES / "marktplaats-config.html").read_text()
    assert "Kees Testverkoper" in html

    facts, text = parse_page(html, URLS["marktplaats-config"])
    ad = p2p_facts.adevinta_listing(BeautifulSoup(html, "html.parser"))

    for kept in (facts.model_dump_json(), text, repr(ad)):
        for leak in ("Kees", "Testverkoper", "kees-testverkoper", "60000001", "D27A7E36", "53.2"):
            assert leak not in kept, leak


@pytest.mark.parametrize(
    ("since", "years", "expected"),
    [
        ("4 maanden", 0, TODAY - timedelta(days=120)),
        ("3 weken", 0, TODAY - timedelta(days=21)),
        ("5 jours", 0, TODAY - timedelta(days=5)),
        ("2 ans", 2, TODAY - timedelta(days=730)),
        ("2 jaar", 2, TODAY - timedelta(days=730)),
        ("", 5, TODAY - timedelta(days=5 * 365)),
        ("", 0, None),
    ],
)
def test_the_accounts_age_from_the_json(since: str, years: int, expected: date | None) -> None:
    assert p2p_facts._member_since({"activeSinceDiff": since, "activeYears": years}) == expected


async def test_an_old_marktplaats_account_with_pickup_looks_fine() -> None:
    # Marktplaats shows no rating count: an account of years with pickup and nothing against it is not "little to
    # go on" (10 of 15 real ads said so next to these very positives).
    _, verdict = await _verdict(facts_of("marktplaats-config"))

    assert verdict.band == "reliable"
    assert verdict.reasons == []
    assert {p.type for p in verdict.positives} >= {"p2p_established", "p2p_pickup"}


async def test_the_accounts_real_age_is_shown_beyond_ten_years() -> None:
    facts = facts_of("marktplaats-config")  # "activeYears": 11
    out = await SellerProfileSkill().run(state_of(facts))

    _, verdict = await _verdict(facts)

    assert out["features"]["seller_profile.account_days"] == 3650  # the scorer's feature keeps its cap
    established = next(p for p in verdict.positives if p.type == "p2p_established")
    assert established.detail == "The seller has been on the platform for 11 years."


async def test_an_llm_lean_towards_paying_off_the_platform_holds_back_looks_fine() -> None:
    llm_read = SkillOutput(features={"content.off_platform_push": 0.8}, findings=[], skill_results=[])

    _, verdict = await _verdict(facts_of("marktplaats-config"), llm_read)

    assert verdict.band == "concerns"


@pytest.mark.parametrize(
    ("since", "text", "kept"),
    [
        (date(2016, 1, 1), "Mooie fiets, ophalen in Groningen.", False),  # the seller's own other ads
        (date(2026, 10, 1), "Mooie fiets, ophalen in Groningen.", True),  # a young account
        (date(2016, 1, 1), "Mooie fiets. Betalen via overboeking, daarna verstuur ik hem.", True),  # another sign
    ],
)
async def test_same_ad_text_counts_only_with_something_else_against_it(since: date, text: str, kept: bool) -> None:
    facts = PageFacts(
        platform="marktplaats",
        url=URLS["marktplaats-config"],
        seller_profile=SellerProfile(member_since=since),
        listing=ListingDetails(description=text, delivery=["pickup"]),
    )
    dup = Finding(skill="duplicate_ads", type="duplicate_text", evidence="The same ad text is in 2 other ads (x).")
    copies = SkillOutput(features={"duplicate_ads.text_copies": 2.0}, findings=[dup], skill_results=[])

    _, verdict = await _verdict(facts, copies)

    assert ("p2p_duplicate_text" in {r.type for r in verdict.reasons}) == kept
    if not kept:
        assert verdict.band == "reliable"


async def test_the_content_reads_yes_no_on_ai_text_is_not_a_reason() -> None:
    llm_read = SkillOutput(features={"content.addresses_ai": 1.0}, findings=[], skill_results=[])
    visible = SkillOutput(features={"injection.instruction_visible": 1.0}, findings=[], skill_results=[])

    _, verdict = await _verdict(facts_of("marktplaats-config"), llm_read)
    _, quoted = await _verdict(facts_of("marktplaats-config"), visible)

    assert "ai_text" not in {r.type for r in verdict.reasons}
    assert "ai_text" in {r.type for r in quoted.reasons}


# ── Real ads that were flagged wrongly ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "advance"),
    [
        (
            "12-month lease, deposit equal to one month's rent, due at lease signing after a viewing.",
            False,
        ),
        (
            "Kamer te huur. Borg van een maand, te betalen bij ondertekening van het contract na de bezichtiging.",
            False,
        ),
        ("Lovely apartment. Once the deposit is wired the keys will be sent to you right away.", True),
        ("Deposit required before any viewing, the rest at signing.", True),
        ("Aanbetaling vooraf via overboeking, dan verstuur ik het.", True),
    ],
)
async def test_a_rental_deposit_at_signing_after_a_viewing_is_normal(text: str, advance: bool) -> None:
    out = await OffPlatformSkill().run(state_of(_ad(URLS["craigslist-apartment"], text, "craigslist")))

    assert out["features"]["off_platform.advance_payment"] == advance


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Betalen kan met ✅ Pinnen in de winkel ✅ Contant betalen ✅ Betalen via bankoverschrijving.", False),
        ("Betalen via overboeking, daarna verstuur ik hem.", True),
        ("Alleen verzenden. Betalen via overboeking of contant.", True),  # a transfer for an item never seen
    ],
)
async def test_a_bank_transfer_among_other_ways_to_pay_is_not_a_sign(text: str, flagged: bool) -> None:
    out = await OffPlatformSkill().run(state_of(_ad(URLS["marktplaats-iphone"], text)))

    assert out["features"]["off_platform.bank_transfer"] == flagged


@pytest.mark.parametrize(
    ("quote", "counts"),
    [
        ("Neem contact op voor specifieke prijzen en beschikbaarheid.", False),
        ("Stuur een bericht voor meer foto's.", False),
        ("Stuur me een WhatsApp op 06-12345678.", True),
        ("Betaling via Tikkie vooraf.", True),
    ],
)
def test_the_llm_off_platform_reading_needs_a_way_off_named(quote: str, counts: bool) -> None:
    assert names_a_way_off(quote) == counts


async def test_contact_us_for_prices_is_not_a_deal_off_the_platform() -> None:
    facts = _ad(URLS["marktplaats-iphone"], "Diverse PS4 en PS5 games. Neem contact op voor specifieke prijzen.")
    quote = "Neem contact op voor specifieke prijzen."
    llm_read = SkillOutput(
        features={"content.off_platform_push": 0.8},
        findings=[Finding(skill="content", type="off_platform_quote", evidence=quote)],
        skill_results=[],
    )

    _, verdict = await _verdict(facts, llm_read)

    assert "p2p_llm_off_platform" not in {r.type for r in verdict.reasons}
