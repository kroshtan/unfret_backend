"""
The product-page upgrades: payment methods, claims vs facts, price vs market, cross-shop reviews, shared addresses,
brand presence and the EU Safety Gate. Each skill's evidence, its shopper reason or positive, and that none of them
moves the shipped scorer's score.
"""

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from unfret import fact_cache, market_prices, review_store, safety_gate, scoring, shop_store, shopper
from unfret.config import settings
from unfret.facts import PageFacts, Review
from unfret.facts.schema import Phone
from unfret.fingerprint import normalise_address
from unfret.graph import SKILLS
from unfret.graph.skills import brand_presence, domain_age
from unfret.graph.skills.brand_presence import BrandPresenceSkill, brand_domains, brand_key
from unfret.graph.skills.claims import ClaimsSkill, address_country, claimed_founding, origin_claims, store_claim
from unfret.graph.skills.domain_age import DomainAgeSkill
from unfret.graph.skills.market_price import MarketPriceSkill
from unfret.graph.skills.payment_methods import PaymentMethodsSkill
from unfret.graph.skills.review_copies import ReviewCopiesSkill
from unfret.graph.skills.safety_gate import SafetyGateSkill
from unfret.graph.skills.shop_links import ShopLinksSkill
from unfret.graph.state import Finding

NEW_SKILLS = ("payment_methods", "claims", "market_price", "review_copies", "brand_presence", "safety_gate")
GTIN = "4006381333931"  # a valid EAN-13


def state(facts: PageFacts, page_type: str = "storefront", **extra: Any) -> Any:
    return {
        "page_content": "",
        "url": "https://shop.example/products/thing",
        "flags": [],
        "facts": facts,
        "cleaned_content": "",
        "page_type": page_type,
        **extra,
    }


def verdict(features: dict[str, float], findings: list[Finding], page_type: str = "storefront", **kw: Any) -> Any:
    return shopper.build(
        40, features=features, findings=findings, facts=kw.pop("facts", PageFacts()), page_type=page_type, **kw
    )


def reason_types(v: Any) -> list[str]:
    return [r.type for r in v.reasons]


@pytest.fixture(autouse=True)
def fresh_stores() -> None:
    fact_cache.use_store(fact_cache.MemoryStore())
    market_prices.use_store(market_prices.MemoryMarketStore())
    review_store.use_store(review_store.MemoryReviewStore())
    shop_store.use_store(shop_store.MemoryShopStore())


# ── The hard rule: the shipped scorer ignores every new feature ────────────────────────────────────────────


def test_shipped_scorer_ignores_the_new_features() -> None:
    model = scoring.Model.from_dict(json.loads(scoring.MODEL_PATH.read_text()))
    new = {f"{s.name}.{f}" for s in SKILLS if s.name in NEW_SKILLS for f in s.features}
    new |= {"domain_age.founded_claim_years", "domain_age.founded_contradicted", "shop_links.shared_address_shops"}
    assert not new & set(model.features)
    base = {"content.scam_copy": 0.4, "listing.discount_pct": 0.3, "eu_registry.vat_present": 1.0}
    every = base | dict.fromkeys(new, 1.0) | {f"{n}.unavailable": 1.0 for n in NEW_SKILLS}
    assert scoring.score(every, [], model) == scoring.score(base, [], model)


def test_training_excludes_the_store_and_lookup_skills() -> None:
    from evals.train import EXCLUDED_SKILLS, feature_names  # noqa: PLC0415

    assert {"market_price", "review_copies", "brand_presence"} <= EXCLUDED_SKILLS
    names = feature_names()
    assert "payment_methods.irreversible_only" in names and "safety_gate.barcode_match" in names


# ── Payment methods ────────────────────────────────────────────────────────────────────────────────────────


async def test_irreversible_payment_only_is_flagged() -> None:
    out = await PaymentMethodsSkill().run(state(PageFacts(payment_methods=["bank_transfer", "crypto"])))
    assert out["features"]["payment_methods.irreversible_only"] == 1.0
    assert out["findings"][0].evidence.startswith("Payment methods mentioned: bank transfer and crypto;")
    v = verdict(out["features"], out["findings"])
    assert reason_types(v)[0] == "irreversible_payment_only"
    assert "bank transfer and crypto" in v.reasons[0].detail


@pytest.mark.parametrize(
    "facts",
    [
        PageFacts(payment_methods=["bank_transfer", "ideal", "paypal"]),  # a protected method next to it
        PageFacts(payment_methods=["gift_card"]),  # selling gift cards
        PageFacts(payment_methods=["bank_transfer"], script_srcs=["https://js.stripe.com/v3/"]),  # card checkout
        PageFacts(payment_methods=["bank_transfer"], platform="shopify"),  # Shopify's checkout takes cards
        PageFacts(),
    ],
)
async def test_irreversible_payment_needs_no_protected_alternative(facts: PageFacts) -> None:
    out = await PaymentMethodsSkill().run(state(facts))
    assert out["features"]["payment_methods.irreversible_only"] == 0.0


async def test_protected_payment_is_a_positive() -> None:
    out = await PaymentMethodsSkill().run(state(PageFacts(payment_methods=["ideal", "visa", "klarna"])))
    v = verdict(out["features"], out["findings"])
    positive = next(p for p in v.positives if p.type == "protected_payment")
    assert "mentions iDEAL, Visa and Klarna" in positive.detail
    assert not any(p.type == "protected_payment" for p in verdict(out["features"], [], "aggregate_listing").positives)


# ── Claims vs facts ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "country"),
    [
        ("Wij zijn een Nederlands familiebedrijf met hart voor kwaliteit.", "NL"),
        ("We are a small family business in Amsterdam.", "NL"),
        ("Our team is based in Germany.", "DE"),
        ("A proud British brand since day one.", "GB"),
    ],
)
def test_origin_claims(text: str, country: str) -> None:
    assert [c.value for c in origin_claims(text)] == [country]


def test_no_origin_claim_in_ordinary_text() -> None:
    assert origin_claims("Ships to the Netherlands and Germany. Dutch translation available.") == []


@pytest.mark.parametrize(
    ("address", "country"),
    [
        ("Herengracht 182, 1016 BR Amsterdam", "NL"),
        ("Room 1203, Tower A, Kowloon", "HK"),
        ("Bao'an District, Shenzhen, Guangdong", "CN"),
        ("12 High Street, London SW1A 1AA", "GB"),
        ("100 Main St, Austin, TX 78701", "US"),
        ("Unit 5, Business Park, Netherlands", "NL"),
        ("Somewhere 12", None),
    ],
)
def test_address_country(address: str, country: str | None) -> None:
    assert address_country(address) == country


async def test_dutch_company_with_only_a_chinese_address() -> None:
    facts = PageFacts(addresses=["Room 1203, Tower B, Bao'an District, Shenzhen, China"])
    text = "Welkom! Wij zijn een Nederlands bedrijf uit Utrecht met de beste lampen."
    out = await ClaimsSkill().run(state(facts, cleaned_content=text))
    assert out["features"]["claims.origin_contradicted"] == 1.0
    v = verdict(out["features"], out["findings"])
    reason = next(r for r in v.reasons if r.type == "origin_contradicted")
    assert "“een Nederlands bedrijf”" in reason.detail and "China" in reason.detail


async def test_origin_claim_with_a_matching_or_unknown_address_is_fine() -> None:
    text = "We are a Dutch company."
    for addresses in (["Herengracht 182, 1016 BR Amsterdam"], ["Somewhere 12"], []):
        out = await ClaimsSkill().run(state(PageFacts(addresses=addresses), cleaned_content=text))
        assert out["features"]["claims.origin_contradicted"] == 0.0, addresses


async def test_store_claim_without_any_address() -> None:
    out = await ClaimsSkill().run(state(PageFacts(), cleaned_content="Come by our showroom for advice!"))
    assert out["features"]["claims.store_without_address"] == 1.0
    assert reason_types(verdict(out["features"], out["findings"])) == ["store_without_address"]
    with_postcode = "Visit our store at Kerkstraat 1, 1017 GA Amsterdam."
    out = await ClaimsSkill().run(state(PageFacts(), cleaned_content=with_postcode))
    assert out["features"]["claims.store_without_address"] == 0.0
    assert store_claim("We ship from our warehouse.") is None
    assert store_claim("Visit our shop in your language: English, Nederlands.") is None
    phone = PageFacts(phones=[Phone(number="+31201234567", kind="local", country="NL")])
    out = await ClaimsSkill().run(state(phone, cleaned_content="Come by our showroom for advice!"))
    assert out["features"]["claims.store_without_address"] == 0.0
    legal = PageFacts(legal_mentions=["Partyverhuur Vliststraat 3-5 3044ET Rotterdam KvK 24368526"])
    out = await ClaimsSkill().run(state(legal, cleaned_content="Kom langs in onze showroom."))
    assert out["features"]["claims.store_without_address"] == 0.0


def test_claimed_founding_is_about_the_shop_itself() -> None:
    year = datetime.now(tz=UTC).year
    assert claimed_founding(PageFacts(), "Our family shop, since 1998, sells lamps.", year) == (1998, "since 1998")
    assert claimed_founding(PageFacts(), "Founded 20 years ago in a garage.", year) == (
        year - 20,
        "Founded 20 years ago",
    )
    # A brand the shop resells, not the shop: no "we", "our" or "shop" nearby.
    assert claimed_founding(PageFacts(), "Lego bricks: since 1932, a toy classic.", year) is None
    facts = PageFacts(founded_claims=["foundingDate 2004-01-01"])
    assert claimed_founding(facts, "", year) == (2004, "founded 2004")


def _rdap(registered: date) -> dict[str, Any]:
    return {"registered": registered.isoformat(), "expires": None, "registrar": "", "redacted": False}


async def test_founded_claim_on_a_new_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(tz=UTC).date()

    async def rdap(domain: str) -> dict[str, Any]:
        return _rdap(today - timedelta(days=60))

    async def cert(domain: str) -> str:
        return (today - timedelta(days=58)).isoformat()

    monkeypatch.setattr(domain_age, "rdap_lookup", rdap)
    monkeypatch.setattr(domain_age, "first_certificate", cert)
    text = "Since 1998 our family has made the finest lamps."
    out = await DomainAgeSkill().run(state(PageFacts(), cleaned_content=text))
    assert out["features"]["domain_age.founded_contradicted"] == 1.0
    assert out["features"]["domain_age.founded_claim_years"] == today.year - 1998
    v = verdict(out["features"], out["findings"])
    reason = next(r for r in v.reasons if r.type == "founded_contradicted")
    assert "“Since 1998”" in reason.detail and "2 months" in reason.detail


async def test_founded_claim_on_an_old_certificate_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(tz=UTC).date()

    async def rdap(domain: str) -> dict[str, Any]:
        return _rdap(today - timedelta(days=60))  # re-registered recently ...

    async def cert(domain: str) -> str:
        return (today - timedelta(days=3000)).isoformat()  # ... but in use for years

    monkeypatch.setattr(domain_age, "rdap_lookup", rdap)
    monkeypatch.setattr(domain_age, "first_certificate", cert)
    out = await DomainAgeSkill().run(state(PageFacts(), cleaned_content="Since 1998 our shop has sold lamps."))
    assert "domain_age.founded_contradicted" not in out["features"]


# ── Price vs market ────────────────────────────────────────────────────────────────────────────────────────


async def _market(prices: dict[str, float], facts: PageFacts) -> Any:
    for i, (shop, price) in enumerate(prices.items()):
        other = facts.model_copy(update={"price": price})
        await market_prices.observe(f"https://{shop}/p/{i}", other)
    return await MarketPriceSkill().run(state(facts))


async def test_far_below_market() -> None:
    facts = PageFacts(price=39.0, currency="EUR", product_ids={"gtin13": GTIN})
    out = await _market({"a.shop": 129.0, "b.shop": 119.0, "c.nl": 135.0}, facts)
    assert out["features"]["market_price.far_below"] == 1.0
    assert out["features"]["market_price.other_shops"] == 3
    v = verdict(out["features"], out["findings"])
    reason = next(r for r in v.reasons if r.type == "far_below_market")
    assert "70% less" in reason.detail and "3 other shops" in reason.detail


@pytest.mark.parametrize(
    "prices",
    [
        {"a.shop": 129.0, "b.shop": 119.0},  # too few other shops
        {"a.shop": 79.0, "b.shop": 89.0, "c.nl": 85.0},  # cheaper, not far below
    ],
)
async def test_market_needs_enough_shops_and_a_big_gap(prices: dict[str, float]) -> None:
    out = await _market(prices, PageFacts(price=39.0, currency="EUR", product_ids={"gtin13": GTIN}))
    assert "market_price.far_below" not in out["features"]


async def test_market_ignores_own_shop_other_currencies_and_bad_barcodes() -> None:
    facts = PageFacts(price=39.0, currency="EUR", product_ids={"gtin13": GTIN})
    for i in range(4):
        await market_prices.observe(f"https://shop.example/p/{i}", facts.model_copy(update={"price": 129.0}))
        usd = facts.model_copy(update={"price": 129.0, "currency": "USD"})
        await market_prices.observe(f"https://us{i}.example/p", usd)
    out = await MarketPriceSkill().run(state(facts))
    assert out["features"]["market_price.other_shops"] == 0
    assert market_prices.barcode(PageFacts(product_ids={"gtin13": "4006381333932"})) is None  # bad check digit


async def test_market_is_off_with_price_observations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "price_observations", False)
    out = await MarketPriceSkill().run(state(PageFacts(price=1.0, product_ids={"gtin13": GTIN})))
    assert out["features"] == {}
    assert market_prices._store.rows == {}  # type: ignore[attr-defined]


def test_market_keys_are_hashes() -> None:
    assert market_prices.product_key("0" + GTIN) != market_prices.product_key(GTIN)
    amazon = market_prices.shop_key("https://www.amazon.de/dp/B0", PageFacts(platform="amazon"))
    assert amazon == market_prices.shop_key("https://www.amazon.nl/dp/B0", PageFacts(platform="amazon"))
    assert len(amazon or b"") == 32


# ── Cross-shop reviews ─────────────────────────────────────────────────────────────────────────────────────

REVIEWS = [
    Review(body=f"Review number {i}: this lamp arrived quickly, the light is warm and bright, I really love it here.")
    for i in range(6)
]


async def test_reviews_copied_from_another_shop() -> None:
    other = PageFacts(reviews=REVIEWS[:4])
    await review_store.observe("https://other.shop/p", other)
    facts = PageFacts(reviews=[*REVIEWS[:3], Review(body="Great!")])
    out = await ReviewCopiesSkill().run(state(facts))
    f = out["features"]
    assert (f["review_copies.checked"], f["review_copies.copied"], f["review_copies.other_shops"]) == (3, 3, 1)
    # A shopper hears about it for an unknown brand with signs of a resold product.
    resale = f | {"content.generic_product": 0.8}
    v = verdict(resale, out["findings"])
    assert "copied_reviews_elsewhere" in reason_types(v)
    assert "3 of the 3 longer reviews" in next(r.detail for r in v.reasons if r.type == "copied_reviews_elsewhere")
    assert "copied_reviews_elsewhere" not in reason_types(verdict(resale | {"content.brand_recognized": 1.0}, []))
    assert "copied_reviews_elsewhere" not in reason_types(verdict(f, []))


async def test_same_shop_and_short_reviews_never_match() -> None:
    await review_store.observe("https://shop.example/other-product", PageFacts(reviews=REVIEWS))
    out = await ReviewCopiesSkill().run(state(PageFacts(reviews=REVIEWS)))
    assert out["features"]["review_copies.copied"] == 0
    assert review_store.review_hashes(PageFacts(reviews=[Review(body="Great product, fast delivery!")])) == set()


def test_review_normalisation() -> None:
    a = review_store.review_hashes(PageFacts(reviews=[Review(body="Très bien!! " + REVIEWS[0].body)]))
    b = review_store.review_hashes(PageFacts(reviews=[Review(body="tres BIEN " + REVIEWS[0].body.upper())]))
    assert a == b and len(next(iter(a))) == 32


async def test_review_fingerprints_are_off_with_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fingerprints", False)
    out = await ReviewCopiesSkill().run(state(PageFacts(reviews=REVIEWS)))
    assert out["features"] == {}


# ── Shared addresses ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Herengracht 182, 1016 BR Amsterdam", "Herengracht 182\n1016BR AMSTERDAM, Netherlands"),
        ("123 Main St., Suite 400, Austin, TX 78701, USA", "123 Main Street Ste 400 Austin TX 78701"),
    ],
)
def test_address_normalisation(a: str, b: str) -> None:
    assert normalise_address(a) == normalise_address(b) is not None


async def test_address_shared_by_many_unrelated_shops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr("k" * 32))
    address = "Kingsway 12, Suite 5, London WC2B 6AA"
    for i in range(6):
        s = state(PageFacts(addresses=[address]), url=f"https://shop{i}.example/")
        await shop_store.observe(s)
    out = await ShopLinksSkill().run(state(PageFacts(addresses=[address])))
    assert out["features"]["shop_links.shared_address_shops"] == 6
    assert out["features"]["shop_links.linked_shops"] == 0  # an address never links shops
    v = verdict(out["features"], out["findings"])
    assert "shared_address" in reason_types(v)
    stored = {kind for kind, _, _ in shop_store.store().links}  # type: ignore[attr-defined]
    assert stored == {"address"}
    values = {value for _, value, _ in shop_store.store().links}  # type: ignore[attr-defined]
    assert all(len(v) == 64 for v in values) and not any("kingsway" in v.lower() for v in values)


# ── Brand presence ─────────────────────────────────────────────────────────────────────────────────────────


def test_brand_keys_and_domains() -> None:
    assert brand_key("Jojo Maman Bébé") == "jojo maman bebe"
    assert brand_key("Generic") is None and brand_key("") is None
    assert brand_domains("jojo maman bebe") == ["jojomamanbebe.com", "jojomamanbebe.nl"]


def _wikidata(found: dict[str, Any] | None) -> Any:
    async def lookup(brand: str) -> dict[str, Any] | None:
        return found

    return lookup


async def test_known_brand_on_wikidata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", True)
    monkeypatch.setattr(brand_presence, "wikidata_lookup", _wikidata({"id": "Q170416", "websites": ["philips.com"]}))

    async def rdap(domain: str) -> dict[str, Any] | None:
        return _rdap(date(1995, 3, 1)) if domain == "philips.com" else None

    monkeypatch.setattr(brand_presence, "rdap_lookup", rdap)
    out = await BrandPresenceSkill().run(state(PageFacts(brand="Philips"), url="https://www.philips.com/p"))
    f = out["features"]
    assert f["brand_presence.wikidata"] == 1.0 and f["brand_presence.own_domain"] == 1.0
    assert f["brand_presence.shop_is_brand"] == 1.0 and f["brand_presence.own_domain_age_years"] > 25
    v = verdict(f, out["findings"], facts=PageFacts(brand="Philips"))
    assert any(p.type == "known_brand" for p in v.positives)


async def test_brand_with_no_presence_on_a_marketplace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", True)
    monkeypatch.setattr(brand_presence, "wikidata_lookup", _wikidata(None))

    async def rdap(domain: str) -> None:
        return None

    monkeypatch.setattr(brand_presence, "rdap_lookup", rdap)
    facts = PageFacts(brand="ZHUIBO")
    out = await BrandPresenceSkill().run(state(facts, "aggregate_listing"))
    f = out["features"]
    assert f["brand_presence.no_presence"] == 1.0
    white_label = f | {"content.generic_product": 0.8}
    assert "brand_no_presence" in reason_types(verdict(white_label, [], "aggregate_listing", facts=facts))
    # Not without other white-label signs, and never on a shop's own page.
    assert "brand_no_presence" not in reason_types(verdict(f, [], "aggregate_listing", facts=facts))
    assert "brand_no_presence" not in reason_types(verdict(white_label, [], "storefront", facts=facts))


async def test_brand_presence_failure_is_not_no_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", True)

    async def failing(brand: str) -> None:
        raise RuntimeError("Wikidata: cirrussearch-too-busy-error")

    async def rdap(domain: str) -> None:
        return None

    monkeypatch.setattr(brand_presence, "wikidata_lookup", failing)
    monkeypatch.setattr(brand_presence, "rdap_lookup", rdap)
    out = await BrandPresenceSkill().run(state(PageFacts(brand="ZHUIBO"), "aggregate_listing"))
    assert "brand_presence.no_presence" not in out["features"]
    assert await fact_cache.peek("wikidata_brand", "zhuibo") is None  # the failure wasn't cached


async def test_brand_presence_is_off_without_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "network_lookups", False)
    out = await BrandPresenceSkill().run(state(PageFacts(brand="Philips")))
    assert out["features"] == {}


async def test_wikidata_lookup_wants_a_business(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "wbsearchentities": {
            "search": [
                {"id": "Q29470680", "label": "Philips", "match": {"text": "Philips"}},  # a family name
                {"id": "Q170416", "label": "Philips", "match": {"text": "Philips"}},
                {"id": "Q1536003", "label": "Philips Records", "match": {"text": "Philips Records"}},
            ]
        },
        "wbgetentities": {
            "entities": {
                "Q29470680": {"claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q101352"}}}}]}},
                "Q170416": {
                    "labels": {"en": {"value": "Philips"}},
                    "claims": {"P856": [{"mainsnak": {"datavalue": {"value": "https://www.philips.com"}}}]},
                },
            }
        },
    }
    asked: list[str] = []

    async def fetch(url: str, **_: Any) -> bytes:
        asked.append(url)
        action = url.split("action=", 1)[1].split("&", 1)[0]
        return json.dumps(answers[action]).encode()

    monkeypatch.setattr(brand_presence, "fetch_public", fetch)
    found = await brand_presence.wikidata_lookup("Philips")
    assert found == {"id": "Q170416", "label": "Philips", "websites": ["philips.com"]}
    assert "ids=Q29470680|Q170416" in asked[1]  # "Philips Records" isn't the brand's own name

    async def busy(url: str, **_: Any) -> bytes:
        return b'{"error": {"code": "cirrussearch-too-busy-error"}}'

    monkeypatch.setattr(brand_presence, "fetch_public", busy)
    with pytest.raises(RuntimeError):
        await brand_presence.wikidata_lookup("Philips")


# ── EU Safety Gate ─────────────────────────────────────────────────────────────────────────────────────────

ALERTS = {
    "updated": "2026-11-15",
    "reports": ["Report-2026-45"],
    "alerts": [
        [10118633, "SR/03208/26", "2026-11-10", "Jojo Maman Bébé", "Baby Jacket Flower", "C52-683-145-000161",
         ["05054298241091"], "Children's jacket", "Chemical, Environment", "Serious risk"],
        [10118634, "SR/03217/26", "2026-11-10", "Unknown", "", "AB12345", [], "Cap", "Chemical", "Serious risk"],
        [10118700, "SR/03300/26", "2026-11-12", "Lumo", "Night light", "LX-200, LX-300 and 2024", [],
         "Night light", "Electric shock", "Serious risk"],
    ],
}  # fmt: skip


@pytest.fixture
def gate(tmp_path: Path) -> safety_gate.Index:
    path = tmp_path / "index.json.gz"
    path.write_bytes(gzip.compress(json.dumps(ALERTS).encode()))
    return safety_gate.index(path)


def test_safety_gate_matches_the_barcode(gate: safety_gate.Index) -> None:
    found = safety_gate.match(PageFacts(title="Jacket", product_ids={"gtin13": "5054298241091"}), gate)
    assert found is not None and (found.on, found.alert.case) == ("barcode", "SR/03208/26")


@pytest.mark.parametrize(
    ("brand", "title", "case"),
    [
        ("JoJo Maman Bebe", "Baby jacket C52-683-145-000161 with flowers", "SR/03208/26"),
        ("LUMO", "Lumo night light LX 200 for kids", "SR/03300/26"),
        ("Lumo", "Lumo night light LX-2000", None),  # a longer model number
        ("Lumo", "Lumo night light 2024 edition", None),  # "2024" is no model number
        ("Lumos", "Lumos night light LX-200", None),  # another brand
        ("Unknown", "Cap AB12345", None),  # "Unknown" never matches
    ],
)
def test_safety_gate_matches_brand_and_model(
    gate: safety_gate.Index, brand: str, title: str, case: str | None
) -> None:
    found = safety_gate.match(PageFacts(brand=brand, title=title), gate)
    assert (found.alert.case if found else None) == case


async def test_safety_gate_reason(monkeypatch: pytest.MonkeyPatch, gate: safety_gate.Index) -> None:
    monkeypatch.setattr(safety_gate, "index", lambda path=None: gate)
    out = await SafetyGateSkill().run(state(PageFacts(product_ids={"gtin13": "5054298241091"}), "aggregate_listing"))
    assert out["features"]["safety_gate.barcode_match"] == 1.0
    evidence = out["findings"][0].evidence
    assert "SR/03208/26 of 2026-11-10" in evidence and "Chemical, Environment (serious risk)" in evidence
    assert evidence.endswith("https://ec.europa.eu/safety-gate-alerts/screen/webReport/alertDetail/10118633")
    v = verdict(out["features"], out["findings"], "aggregate_listing")
    assert v.reasons[0].type == "safety_gate_alert" and v.reasons[0].severity == "high"
    assert "SR/03208/26" in v.reasons[0].detail


def test_vendored_safety_gate_index() -> None:
    idx = safety_gate.index()
    assert idx.alerts > 30_000 and idx.updated
    assert idx.by_gtin and idx.by_brand


def test_model_codes() -> None:
    assert safety_gate.model_codes("10.2.17.25.271, 2175334.5906.ONE SIZE") == {"1021725271", "21753345906ONESIZE"}
    assert safety_gate.model_codes("ONE SIZE / 2024 / S-XL") == set()
