"""The anonymous price store and the fake-discount skill."""

from datetime import date, timedelta
from typing import Any

import pytest

from unfret import price_store
from unfret.config import settings
from unfret.facts import PageFacts
from unfret.facts.schema import Platform
from unfret.graph.skills.pricing import PricingSkill
from unfret.price_store import MemoryPriceStore, Observation


@pytest.fixture(autouse=True)
def store() -> MemoryPriceStore:
    memory = MemoryPriceStore()
    price_store.use_store(memory)
    return memory


def state(facts: PageFacts, url: str = "", text: str = "", page_type: str = "storefront") -> Any:
    return {"facts": facts, "url": url, "cleaned_content": text, "page_type": page_type, "flags": []}


def test_product_ids_are_public_identifiers_only() -> None:
    amazon = PageFacts(platform="amazon")
    assert price_store.product_id("https://www.amazon.nl/Some-Lamp/dp/B0ABC12345?th=1", amazon) == "asin:B0ABC12345"
    bol = PageFacts(platform="bol.com")
    assert (
        price_store.product_id("https://www.bol.com/nl/nl/p/glow-lamp/9300000123456789/", bol)
        == "bol:9300000123456789"
    )
    shop = PageFacts(json_ld=[{"@type": "Product", "offers": {"gtin13": "8712345678906"}}])
    assert price_store.product_id("https://glow.example/products/lamp", shop) == "gtin:08712345678906"
    assert price_store.product_id("https://glow.example/products/lamp", PageFacts()) is None
    bad_check_digit = PageFacts(json_ld=[{"@type": "Product", "gtin13": "8712345678907"}])
    assert price_store.product_id("https://glow.example/p", bad_check_digit) is None


def test_the_key_hides_the_url_and_ignores_subdomains() -> None:
    facts = PageFacts(platform="amazon")
    a = price_store.product_key("https://www.amazon.co.uk/x/dp/B0ABC12345", facts)
    b = price_store.product_key("https://smile.amazon.co.uk/dp/B0ABC12345?ref=x", facts)
    assert a == b and a is not None and len(a) == 32
    assert b"amazon" not in a
    assert price_store.registrable_domain("shop.example.co.uk") == "example.co.uk"


async def test_one_row_per_product_per_day_without_url_or_caller(store: MemoryPriceStore) -> None:
    facts = PageFacts(platform="amazon", price=19.95, list_price=79.95, currency="eur")
    url = "https://www.amazon.nl/dp/B0ABC12345"
    await price_store.observe(url, facts)
    await price_store.observe(url, facts.model_copy(update={"price": 9.0}))

    [(key, day)] = store.rows
    assert store.rows[(key, day)] == Observation(day, 19.95, "EUR", 79.95)  # the day's first observation
    assert set(vars(store.rows[(key, day)])) == {"day", "price", "currency", "was_price"}


async def test_old_observations_are_purged(store: MemoryPriceStore) -> None:
    key = b"k" * 32
    today = price_store.today()
    await store.record(key, Observation(today - timedelta(days=91), 10.0, "EUR", None))
    await store.record(key, Observation(today - timedelta(days=89), 10.0, "EUR", None))
    await store.purge(today - timedelta(days=price_store.RETENTION_DAYS))
    assert [d for _, d in store.rows] == [today - timedelta(days=89)]


async def test_fake_discount_wording() -> None:
    facts = PageFacts(price=19.95, list_price=99.95)
    text = "Adviesprijs: €99,95 Nu €19,95. Opheffingsuitverkoop, alles moet weg!"

    out = await PricingSkill().run(state(facts, text=text))

    f = out["features"]
    assert (f["pricing.implausible_discount"], f["pricing.reference_anchor"], f["pricing.permanent_sale"]) == (1, 1, 1)
    assert {o.type for o in out["findings"]} == {"implausible_discount", "reference_anchor", "permanent_sale"}


@pytest.mark.parametrize(
    ("platform", "text"),
    [
        # Wording from archived amazon.com and Walmart listings (US-market fixtures), and bol.com's own label.
        ("amazon", "Paperback $10.90 Print List Price: $14.39 $14.39"),
        ("amazon", "-25% $10.90 Typical price: $14.39"),
        ("walmart", "Current price is USD Now $89.00 You save $40.99 Typical price $129.99"),
        ("bol.com", "Adviesprijs € 14,39 € 10,90"),
        ("bol.com", "Meestal € 14,39 € 10,90"),
        # Amazon's own label on its other locales: .nl, .de, .co.uk, .fr.
        ("amazon", "-24% € 10,90 Adviesprijs: € 14,39"),
        ("amazon", "-24% 10,90 € UVP: €14,39"),
        ("amazon", "-24% 10,90 € Unverbindliche Preisempfehlung: €14,39"),
        ("amazon", "-24% £10.90 RRP: £14.39"),
        ("amazon", "-24% 10,90 € Prix conseillé : €14,39"),
    ],
)
async def test_a_marketplaces_own_reference_price_is_a_note(platform: Platform, text: str) -> None:
    facts = PageFacts(platform=platform, price=10.9, list_price=14.39)
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert "pricing.reference_anchor" not in out["features"]
    assert [o.type for o in out["findings"]] == ["marketplace_reference"]


@pytest.mark.parametrize(
    ("platform", "text"),
    [
        # Amazon's other EU locales: .it, .es, .pl, .se.
        ("amazon", "-24% 10,90 € Prezzo consigliato: 14,39 €"),
        ("amazon", "-24% 10,90 € Prezzo di listino: 14,39 €"),
        ("amazon", "-24% 10,90 € Precio recomendado: 14,39 €"),
        ("amazon", "-24% 10,90 € PVPR: 14,39 €"),
        ("amazon", "-24% 10,90 € Precio de venta recomendado: 14,39 €"),
        ("amazon", "-24% 10,90 zł Cena katalogowa: 14,39 zł"),
        ("amazon", "-24% 10,90 zł Sugerowana cena: 14,39 zł"),
        ("amazon", "-24% 109 kr Rek. pris: 143,90 kr"),
        ("amazon", "-24% 10,90 € Unverb. Preisempf.: 14,39 €"),
        # eBay's struck "List price".
        ("ebay", "US $10.90 List price US $14.39 Save US $3.49 (24% off)"),
    ],
)
async def test_eu_marketplace_reference_labels_are_a_note(platform: Platform, text: str) -> None:
    facts = PageFacts(platform=platform, price=10.9, list_price=14.39)
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert "pricing.reference_anchor" not in out["features"]
    assert [o.type for o in out["findings"]] == ["marketplace_reference"]


@pytest.mark.parametrize(
    "text",
    [
        # The currency before or after the number, "," or "." for decimals, "." "," or a space for thousands.
        "UVP: 14,39 €",
        "UVP: 14,39€ jetzt 10,90€",
        "UVP 1.299,00 € jetzt 999,00 €",
        "UVP: € 1.299,00",
        "UVP: EUR 14,39",
        "UVP: 14,39 EUR",
        "RRP: £1,299.00",
        "Prix conseillé : 1 299,00 €",
        "Prezzo consigliato: 19,99 €",
        "Precio recomendado: 1.299 €",
        "PVPR 19,99 €",
        "Cena katalogowa: 1 299,00 zł",
        "Rekommenderat pris: 1 299 kr",
        "Vejledende pris: 1.299,00 kr.",
    ],
)
async def test_a_reference_label_matches_whatever_the_price_format(text: str) -> None:
    out = await PricingSkill().run(state(PageFacts(price=10.9, list_price=14.39), text=text))
    assert out["features"] == {"pricing.reference_anchor": 1.0}
    assert [o.type for o in out["findings"]] == ["reference_anchor"]


@pytest.mark.parametrize(
    "text",
    ["Prezzo consigliato: 14,39 €", "PVPR: 14,39 €", "Cena katalogowa: 14,39 zł", "Rek. pris: 143,90 kr"],
)
async def test_eu_reference_labels_on_a_web_shop_are_the_reason(text: str) -> None:
    out = await PricingSkill().run(state(PageFacts(price=10.9, list_price=14.39), text=text))
    assert out["features"] == {"pricing.reference_anchor": 1.0}


async def test_a_label_without_a_price_after_it_is_not_an_anchor() -> None:
    text = "UVP des Herstellers beachten. Normaal 2 dagen levertijd. Prezzo consigliato dal produttore."
    out = await PricingSkill().run(state(PageFacts(price=10.9, list_price=14.39), text=text))
    assert out["features"] == {}


async def test_an_ebay_sellers_own_reference_label_still_counts() -> None:
    facts = PageFacts(platform="ebay", price=10.9, list_price=14.39)
    text = "US $10.90 List price US $14.39. RRP £49.99, yours for £10.90!"
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}
    assert [o.evidence for o in out["findings"]] == ['"RRP"']


async def test_a_marketplaces_own_reference_price_never_charged_is_the_reason(store: MemoryPriceStore) -> None:
    url = "https://www.amazon.nl/dp/B0ABC12345"
    facts = PageFacts(platform="amazon", price=39.95, list_price=79.95, currency="EUR")
    key = price_store.product_key(url, facts)
    assert key is not None
    today = price_store.today()
    await store.record(
        key, Observation(today - timedelta(days=settings.price_history_min_days + 5), 39.0, "EUR", None)
    )
    await store.record(key, Observation(today - timedelta(days=10), 34.95, "EUR", None))
    text = "-50% € 39,95 Adviesprijs: € 79,95"

    out = await PricingSkill().run(state(facts, url=url, text=text, page_type="aggregate_listing"))

    assert out["features"]["pricing.was_above_30d_low"] == 1.0
    assert out["features"]["pricing.reference_anchor"] == 1.0
    assert [o.type for o in out["findings"]] == ["was_never_charged", "reference_anchor"]


async def test_a_marketplaces_own_reference_price_charged_recently_stays_a_note(store: MemoryPriceStore) -> None:
    url = "https://www.amazon.nl/dp/B0ABC12345"
    facts = PageFacts(platform="amazon", price=39.95, list_price=79.95, currency="EUR")
    key = price_store.product_key(url, facts)
    assert key is not None
    today = price_store.today()
    for days in (settings.price_history_min_days + 5, 3):
        await store.record(key, Observation(today - timedelta(days=days), 79.95, "EUR", None))
    text = "-50% € 39,95 Adviesprijs: € 79,95"

    out = await PricingSkill().run(state(facts, url=url, text=text, page_type="aggregate_listing"))

    assert "pricing.reference_anchor" not in out["features"]
    assert [o.type for o in out["findings"]] == ["marketplace_reference"]


async def test_an_anchor_label_on_a_web_shop_is_still_the_reason() -> None:
    facts = PageFacts(price=10.9, list_price=14.39)
    out = await PricingSkill().run(state(facts, text="Adviesprijs € 14,39 € 10,90"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}
    assert [o.type for o in out["findings"]] == ["reference_anchor"]


async def test_a_marketplaces_plain_was_price_says_nothing() -> None:
    facts = PageFacts(platform="walmart", price=89.0, list_price=129.99)
    text = "Current price is USD Now $89.00 You save $40.99 was $129.99 $129.99"
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert (out["features"], out["findings"]) == ({}, [])


async def test_a_sellers_own_anchor_still_counts_on_a_marketplace() -> None:
    facts = PageFacts(platform="amazon", price=10.9, list_price=14.39)
    text = "List Price: $14.39. Retail value: $49.99, yours for $10.90!"
    out = await PricingSkill().run(state(facts, text=text, page_type="aggregate_listing"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}
    assert [o.evidence for o in out["findings"]] == ['"Retail value"']


async def test_a_list_price_label_still_counts_on_a_shop() -> None:
    out = await PricingSkill().run(state(PageFacts(price=10.9, list_price=14.39), text="List price: $14.39"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}


async def test_an_honest_was_price_is_not_an_anchor() -> None:
    out = await PricingSkill().run(state(PageFacts(price=40.0, list_price=50.0), text="Was €50, now €40."))
    assert out["features"] == {}


async def test_was_never_charged_needs_a_month_of_observations(store: MemoryPriceStore) -> None:
    url = "https://www.amazon.nl/dp/B0ABC12345"
    facts = PageFacts(platform="amazon", price=39.95, list_price=79.95, currency="EUR")
    key = price_store.product_key(url, facts)
    assert key is not None
    today = price_store.today()
    await store.record(key, Observation(today - timedelta(days=10), 34.95, "EUR", None))

    young = await PricingSkill().run(state(facts, url=url, page_type="aggregate_listing"))
    assert "pricing.was_above_30d_low" not in young["features"]  # gated: only 10 days of data

    await store.record(
        key, Observation(today - timedelta(days=settings.price_history_min_days + 5), 39.0, "EUR", None)
    )
    store.rows.pop((key, today))  # observed again "tomorrow"
    old = await PricingSkill().run(state(facts, url=url, page_type="aggregate_listing"))
    assert old["features"]["pricing.was_above_30d_low"] == 1.0
    assert "34.95" in old["findings"][0].evidence


async def test_the_store_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, store: MemoryPriceStore) -> None:
    monkeypatch.setattr(settings, "price_observations", False)
    facts = PageFacts(platform="amazon", price=19.95, currency="EUR")
    await PricingSkill().run(state(facts, url="https://www.amazon.nl/dp/B0ABC12345"))
    assert store.rows == {}


def test_gtin_check_digit() -> None:
    assert price_store.valid_gtin("4006381333931")
    assert not price_store.valid_gtin("4006381333932")
    assert not price_store.valid_gtin(str(date.today().year))


# The brand's own shop: its domain is the brand's name, or the seller is the brand (wording of a brand's own shop).
_OWN_SHOP_TEXT = "Sneaker Unisex\n77,95 €\nUVP:\n109,95 €\n(Preis inkl. 19% MwSt.)"


@pytest.mark.parametrize(
    ("url", "seller"),
    [
        ("https://eu.acmeshoes.com/de/de/pd/sneaker/398846", ""),
        ("https://acmeshoes.de/sneaker", ""),
        ("", "ACMESHOES"),
        ("https://shop.example/sneaker", "AcmeShoes Official Store"),
    ],
)
async def test_a_recommended_price_on_the_brands_own_shop_is_a_note(url: str, seller: str) -> None:
    facts = PageFacts(brand="AcmeShoes", seller=seller, price=77.95, list_price=109.95)
    out = await PricingSkill().run(state(facts, url=url, text=_OWN_SHOP_TEXT))
    assert "pricing.reference_anchor" not in out["features"]
    assert [(o.type, o.evidence) for o in out["findings"]] == [("brand_reference", '"UVP"')]


@pytest.mark.parametrize(
    ("facts", "url", "page_type"),
    [
        # Another web shop's "UVP" for the brand it stocks.
        (
            PageFacts(brand="AcmeShoes", seller="Sneakerhut", price=77.95, list_price=109.95),
            "https://sneakerhut.de/x",
            "storefront",
        ),
        # A brand that looks made up: its "recommended" price is the shop's own.
        (
            PageFacts(brand="XIEERDUO", seller="XIEERDUO", price=77.95, list_price=109.95),
            "https://xieerduo.shop/x",
            "storefront",
        ),
        # A marketplace listing is the marketplace's (or the seller's) label, not the brand's own shop.
        (
            PageFacts(platform="ebay", brand="AcmeShoes", seller="AcmeShoes", price=77.95, list_price=109.95),
            "",
            "aggregate_listing",
        ),
    ],
)
async def test_a_recommended_price_elsewhere_is_still_the_reason(facts: PageFacts, url: str, page_type: str) -> None:
    out = await PricingSkill().run(state(facts, url=url, text=_OWN_SHOP_TEXT, page_type=page_type))
    assert out["features"] == {"pricing.reference_anchor": 1.0}
    assert [o.type for o in out["findings"]] == ["reference_anchor"]


async def test_only_a_recommended_price_label_is_the_brands_own() -> None:
    facts = PageFacts(brand="AcmeShoes", seller="AcmeShoes", price=77.95, list_price=109.95)
    out = await PricingSkill().run(state(facts, text="77,95 € Compare at 109,95 €"))
    assert out["features"] == {"pricing.reference_anchor": 1.0}


@pytest.mark.parametrize(
    "disclosure",
    [
        "30 Tage Bestpreis: 88,95 €",
        "Niedrigster Preis der letzten 30 Tage: 88,95 €",
        "Laagste prijs van de afgelopen 30 dagen: € 88,95",
        "Lowest price in the last 30 days: €88.95",
        "30-day low: €88.95",
        "Prix le plus bas des 30 derniers jours : 88,95 €",
        "Prezzo più basso degli ultimi 30 giorni: 88,95 €",
        "Precio más bajo de los últimos 30 días: 88,95 €",
        "Najniższa cena z 30 dni przed obniżką: 88,95 zł",
        "Lägsta pris de senaste 30 dagarna: 88,95 kr",
    ],
)
async def test_a_recommended_price_next_to_the_30_day_low_is_a_note(disclosure: str) -> None:
    facts = PageFacts(brand="AcmeShoes", seller="Sneakerhut", price=77.95, list_price=109.95)
    text = f"77,95 € UVP: 109,95 € {disclosure}"
    out = await PricingSkill().run(state(facts, url="https://sneakerhut.example/x", text=text))
    assert "pricing.reference_anchor" not in out["features"]
    assert [(o.type, o.evidence) for o in out["findings"]] == [
        ("disclosed_reference", '"UVP"; lowest price in the last 30 days: 88.95')
    ]


@pytest.mark.parametrize(
    ("price", "disclosure"),
    [
        # Today's price is above the 30-day low: the discount isn't measured honestly.
        (99.95, "30 Tage Bestpreis: 88,95 €"),
        # The 30-day low above the "was" price doesn't fit either.
        (77.95, "30 Tage Bestpreis: 119,95 €"),
    ],
)
async def test_a_30_day_low_that_doesnt_fit_keeps_the_reason(price: float, disclosure: str) -> None:
    facts = PageFacts(brand="AcmeShoes", seller="Sneakerhut", price=price, list_price=109.95)
    text = f"{price} € UVP: 109,95 € {disclosure}"
    out = await PricingSkill().run(state(facts, url="https://sneakerhut.example/x", text=text))
    assert out["features"] == {"pricing.reference_anchor": 1.0}


async def test_a_brands_recommended_price_never_charged_is_the_reason(store: MemoryPriceStore) -> None:
    url = "https://acmeshoes.example/sneaker"
    gtin = [{"@type": "Product", "offers": {"gtin13": "8712345678906"}}]
    facts = PageFacts(
        brand="AcmeShoes", seller="AcmeShoes", price=39.95, list_price=79.95, currency="EUR", json_ld=gtin
    )
    key = price_store.product_key(url, facts)
    assert key is not None
    today = price_store.today()
    await store.record(
        key, Observation(today - timedelta(days=settings.price_history_min_days + 5), 39.0, "EUR", None)
    )
    await store.record(key, Observation(today - timedelta(days=10), 34.95, "EUR", None))
    out = await PricingSkill().run(state(facts, url=url, text="39,95 € UVP: 79,95 €"))
    assert out["features"]["pricing.reference_anchor"] == 1.0
    assert [o.type for o in out["findings"]] == ["was_never_charged", "reference_anchor"]
