"""Service providers: extraction (testimonials, platform profiles), the skills, operator linking and the verdict."""

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, SecretStr

from unfret import fact_cache, shop_store, shopper
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.facts.schema import Email, Link, Phone, Testimonial
from unfret.graph import build_graph, nodes, services
from unfret.graph.skills import SERVICE_PAGES, Skill, SkillOutput
from unfret.graph.skills import platform_profile as platform_profile_module
from unfret.graph.skills.dark_patterns import DarkPatternsSkill
from unfret.graph.skills.doorway_pages import DoorwayPagesSkill, city_pages
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.graph.skills.injection import InjectionSkill
from unfret.graph.skills.platform_profile import PlatformProfileSkill
from unfret.graph.skills.reverse_image import main_photo
from unfret.graph.skills.service_identity import ServiceIdentitySkill
from unfret.graph.skills.service_terms import ServiceTermsSkill
from unfret.graph.skills.shop_links import ShopLinksSkill
from unfret.graph.skills.testimonials import TestimonialsSkill
from unfret.graph.state import AnalysisState, Finding, PageType
from unfret.market import service_market
from unfret.schemas.requests import AnalyzeRequest
from unfret.shop_store import MemoryShopStore

FIXTURES = Path(__file__).parent.parent / "fixtures" / "services"
URLS = {
    "nl-locksmith-doorway": "https://slotenmaker-utrecht-spoed.nl/",
    "nl-installer-established": "https://www.devries-installatie.nl/",
    "us-plumber-established": "https://www.bluebonnetplumbing.com/",
    "us-locksmith-doorway": "https://houston-locksmith-247.com/",
    "werkspot-profile": "https://www.werkspot.nl/profiel/loodgietersbedrijf-jansen",
    "thumbtack-profile": "https://www.thumbtack.com/tx/austin/plumbing/bluebonnet-plumbing/service/123456789",
    "angi-profile": "https://www.angi.com/companylist/us/tx/houston/lone-star-rooter-reviews-7654321.htm",
}
TODAY = date(2026, 11, 15)


def page(name: str) -> tuple[str, str]:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8"), URLS[name]


def facts_of(name: str) -> PageFacts:
    html, url = page(name)
    return parse_page(html, url)[0]


def state_of(name: str, page_type: str = "service_site") -> Any:
    html, url = page(name)
    facts, text = parse_page(html, url)
    return {
        "page_content": html,
        "url": url,
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": page_type,
    }


async def features_of(skill: Skill, name: str, page_type: str = "service_site") -> tuple[dict[str, float], set[str]]:
    out = await skill.run(state_of(name, page_type))
    return out["features"], {f.type for f in out["findings"]}


class _Age(Skill):
    """domain_age without the network: a fixed registration age."""

    name = "domain_age"
    description = "fixed"
    page_types = SERVICE_PAGES

    def __init__(self, years: float | None) -> None:
        self.years = years

    async def run(self, state: AnalysisState) -> SkillOutput:
        if self.years is None:
            return self.output({"known": False})
        return self.output({"known": True, "age_years": self.years, "under_6_months": self.years < 0.5})


class _LLM:
    def __init__(self, page_type: PageType = "service_site") -> None:
        self.page_type = page_type

    def structured(self, schema: type[BaseModel]) -> RunnableLambda:
        def respond(messages: Any) -> BaseModel:
            if schema is nodes._ClassifyOutputExpanded:
                return schema(page_type=self.page_type, platform="", reasoning="r")
            if schema is nodes._ClassifyOutput:
                return schema(page_type="storefront", platform="generic", reasoning="r")
            return nodes._JudgeOutput(score=70, explanation="Fine.")

        return RunnableLambda(respond)


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> MemoryShopStore:
    monkeypatch.setattr(settings, "expansion_classes", True)
    monkeypatch.setattr(settings, "fingerprints", True)
    monkeypatch.setattr(settings, "network_lookups", False)
    monkeypatch.setattr(settings, "contact_hash_key", SecretStr("test-contact-key"))
    monkeypatch.setattr(nodes, "get_structured_llm", _LLM().structured)
    monkeypatch.setattr(platform_profile_module, "today", lambda: TODAY)
    store = MemoryShopStore()
    shop_store.use_store(store)
    fact_cache.use_store(fact_cache.MemoryStore())
    return store


def skills(age: float | None) -> list[Skill]:
    return [
        ServiceIdentitySkill(),
        DoorwayPagesSkill(),
        ServiceTermsSkill(),
        TestimonialsSkill(),
        PlatformProfileSkill(),
        ShopLinksSkill(),
        EuRegistrySkill(vies=False),
        InjectionSkill(),
        DarkPatternsSkill(),
        _Age(age),
    ]


async def check(name: str, age: float | None = None, url: str | None = None) -> dict[str, Any]:
    html, default = page(name)
    target = url or default
    result: dict[str, Any] = await build_graph(skills(age)).ainvoke(
        {"page_content": html.replace(default.rstrip("/"), target.rstrip("/")), "url": target, "flags": []}
    )
    await shop_store.drain()
    return result


# ── Extraction ──────────────────────────────────────────────────────────────────────────────────────────────


def test_testimonials_are_read_with_name_rating_and_date() -> None:
    found = facts_of("nl-installer-established").testimonials

    assert [t.name for t in found] == ["Marieke, Zeist", "Hans", "Familie Bakker", "Ahmed"]
    assert [t.rating for t in found] == [5.0, 4.0, 5.0, 3.0]
    assert found[0].posted == date(2025, 3, 12)
    assert found[0].text.startswith("Ketel binnen een dag vervangen")


def test_testimonials_from_star_labels_cite_and_time() -> None:
    first = facts_of("nl-locksmith-doorway").testimonials[0]

    assert first == Testimonial(
        text="Snel geholpen in Amersfoort! Binnen 20 minuten was de slotenmaker er. Top service, aanrader!",
        name="Jan",
        rating=5.0,
        posted=date(2026, 11, 2),
    )


def test_structured_data_reviews_come_first_and_alone() -> None:
    found = facts_of("us-plumber-established").testimonials

    assert [t.name for t in found] == ["Dana R.", "Marcus T.", "Priya S.", "Glen W."]
    assert [t.rating for t in found] == [5.0, 4.0, 5.0, 2.0]


def test_review_microdata_is_read() -> None:
    found = facts_of("us-locksmith-doorway").testimonials

    assert [t.name for t in found] == ["John Smith", "Mike Johnson", "John Doe", "Sarah Lee"]
    assert found[0].posted == date(2026, 10, 28) and found[0].rating == 5.0


def test_a_product_pages_reviews_are_not_testimonials() -> None:
    html = (
        "<html><head><meta property='og:price:amount' content='49.90'></head><body><h1>Massagepistole</h1>"
        "<div class='review-card'><p>Super Gerät, sehr stark und leise!</p></div></body></html>"
    )

    assert parse_page(html, "https://gadget.example/p/1")[0].testimonials == []


def test_testimonials_in_a_form_are_the_visitors_not_the_companys() -> None:
    html = (
        "<html><body><form><div class='testimonial'><blockquote>Mijn eigen ervaring met deze slotenmaker"
        "</blockquote><cite>Jane Doe</cite></div></form></body></html>"
    )

    assert parse_page(html, "https://x.nl/")[0].testimonials == []


@pytest.mark.parametrize(
    ("name", "member_since", "jobs", "rating", "count", "badges", "rate"),
    [
        ("werkspot-profile", date(2017, 3, 1), 312, 4.6, 87, ["Topvakman", "KvK geverifieerd"], 96.0),
        ("thumbtack-profile", date(2018, 1, 1), 388, 4.9, 143, ["Top Pro", "Background checked", "License verified"],
         None),
        ("angi-profile", date(2026, 10, 1), None, 5.0, 3, [], 40.0),
    ],
)  # fmt: skip
def test_platform_profiles(
    *,
    name: str,
    member_since: date,
    jobs: int | None,
    rating: float,
    count: int,
    badges: list[str],
    rate: float | None,
) -> None:
    facts = facts_of(name)
    profile = facts.seller_profile

    assert profile is not None
    assert (profile.member_since, profile.jobs_completed, profile.rating, profile.rating_count) == (
        member_since,
        jobs,
        rating,
        count,
    )
    assert (profile.badges, profile.response_rate) == (badges, rate)
    assert facts.testimonials == []  # a platform's reviews go into reviews, without the reviewers' names
    assert facts.reviews and all(r.body for r in facts.reviews)


def test_a_logged_in_visitors_own_membership_is_not_the_providers() -> None:
    # The account menus say "Lid sinds 2014" and "Member since 2016": the visitor's, never read.
    assert facts_of("werkspot-profile").seller_profile.member_since == date(2017, 3, 1)  # type: ignore[union-attr]
    assert facts_of("thumbtack-profile").seller_profile.member_since == date(2018, 1, 1)  # type: ignore[union-attr]
    assert "Jane" not in facts_of("werkspot-profile").model_dump_json()


def test_service_markets() -> None:
    assert service_market(facts_of("nl-installer-established")) == "nl"
    assert service_market(facts_of("nl-locksmith-doorway"), URLS["nl-locksmith-doorway"]) == "nl"
    assert service_market(facts_of("us-plumber-established")) == "us"
    assert service_market(facts_of("us-locksmith-doorway"), URLS["us-locksmith-doorway"]) == "us"
    assert service_market(PageFacts(url="https://klempner.de/")) == "other"


# ── Skills ──────────────────────────────────────────────────────────────────────────────────────────────────


async def test_identity_of_an_established_dutch_company() -> None:
    features, found = await features_of(ServiceIdentitySkill(), "nl-installer-established")

    assert features["service_identity.kvk_shown"] == 1 and features["service_identity.complete"] == 1
    assert features["service_identity.vat_shown"] == 1 and features["service_identity.names_differ"] == 0
    assert {"kvk", "company"} <= found


async def test_identity_of_a_dutch_site_without_kvk() -> None:
    features, found = await features_of(ServiceIdentitySkill(), "nl-locksmith-doorway")

    assert features["service_identity.kvk_shown"] == 0 and features["service_identity.complete"] == 0
    assert {"no_kvk", "no_street_address"} <= found


async def test_a_kvk_number_without_eight_digits_is_malformed() -> None:
    facts = PageFacts(url="https://x.nl/", legal_mentions=["KvK-nummer: 1234567 · BTW NL0012"])
    state: Any = {"facts": facts, "url": facts.url, "cleaned_content": ""}

    out = await ServiceIdentitySkill().run(state)

    assert out["features"]["service_identity.kvk_malformed"] == 1
    assert "1234567" in out["findings"][-2].evidence or any("1234567" in f.evidence for f in out["findings"])


async def test_us_licence_shown_and_claimed_without_a_number() -> None:
    shown, found_shown = await features_of(ServiceIdentitySkill(), "us-plumber-established")
    claimed, found_claimed = await features_of(ServiceIdentitySkill(), "us-locksmith-doorway")

    assert shown["service_identity.licence_shown"] == 1 and shown["service_identity.complete"] == 1
    assert "licence" in found_shown
    assert claimed["service_identity.licence_claim_no_number"] == 1 and claimed["service_identity.complete"] == 0
    assert "licence_no_number" in found_claimed


async def test_two_company_names_are_reported() -> None:
    facts = PageFacts(
        url="https://x.nl/",
        business_name="Loodgieter Snel",
        copyright_lines=["© 2026 Dutch Home Services Holding B.V."],
        kvk_numbers=["12345678"],
    )
    out = await ServiceIdentitySkill().run({"facts": facts, "url": facts.url, "cleaned_content": ""})  # type: ignore[typeddict-item]

    assert out["features"]["service_identity.names_differ"] == 1


async def test_identity_does_not_apply_elsewhere() -> None:
    facts = PageFacts(url="https://klempner.de/")
    out = await ServiceIdentitySkill().run({"facts": facts, "url": facts.url, "cleaned_content": ""})  # type: ignore[typeddict-item]

    assert out["features"] == {"service_identity.not_applicable": 1.0}


def test_city_pages_from_link_text_and_addresses() -> None:
    links = [
        ("Loodgieter Utrecht", "https://x.nl/loodgieter-utrecht"),
        ("Bekijk", "https://x.nl/loodgieter-den-haag"),
        ("Plumber in Austin, TX", "https://x.com/a"),
        ("Loodgieter prijzen", "https://x.nl/loodgieter-prijzen"),
        ("Locksmith near me", "https://x.com/locksmith-near-me"),
        ("Over ons", "https://x.nl/over-ons"),
    ]

    assert city_pages(links) == ["utrecht", "den haag", "austin"]


async def test_doorway_pages() -> None:
    nl, nl_found = await features_of(DoorwayPagesSkill(), "nl-locksmith-doorway")
    us, _ = await features_of(DoorwayPagesSkill(), "us-locksmith-doorway")
    real, real_found = await features_of(DoorwayPagesSkill(), "nl-installer-established")

    assert (nl["doorway_pages.city_pages"], nl["doorway_pages.doorway"]) == (10, 1)
    assert nl["doorway_pages.central_number_only"] == 1 and nl["doorway_pages.areas_served"] == 12
    assert "doorway" in nl_found
    assert (us["doorway_pages.city_pages"], us["doorway_pages.doorway"]) == (8, 1)
    # A real local business with a few pages for the towns around it, and a street address: no doorway.
    assert (real["doorway_pages.city_pages"], real["doorway_pages.doorway"]) == (2, 0)
    assert real["doorway_pages.street_address"] == 1 and "doorway" not in real_found


async def test_many_town_pages_with_an_address_are_not_a_doorway() -> None:
    towns = ("Zeist", "Bunnik", "Houten", "Baarn", "Soest", "Leusden", "Woudenberg", "Maarn")
    links = [Link(text=f"Schilder {town}", url=f"https://x.nl/schilder-{town.lower()}") for town in towns]
    facts = PageFacts(url="https://x.nl/", internal_links=links, addresses=["Dorpsstraat 1, 1234 AB, Zeist"])
    out = await DoorwayPagesSkill().run({"facts": facts, "url": facts.url})  # type: ignore[typeddict-item]

    assert out["features"]["doorway_pages.city_pages"] == 8 and out["features"]["doorway_pages.doorway"] == 0


async def test_service_terms_of_an_emergency_locksmith() -> None:
    nl, nl_found = await features_of(ServiceTermsSkill(), "nl-locksmith-doorway")
    us, _ = await features_of(ServiceTermsSkill(), "us-locksmith-doorway")

    assert {k for k, v in nl.items() if v} == {
        "service_terms.cash_only",
        "service_terms.bait_callout",
        "service_terms.quote_on_site",
        "service_terms.emergency_pressure",
        "service_terms.premium_number",
    }
    assert {k for k, v in us.items() if v} == {
        "service_terms.upfront_payment",
        "service_terms.cash_only",
        "service_terms.bait_callout",
        "service_terms.quote_on_site",
        "service_terms.emergency_pressure",
    }
    assert "premium_number" in nl_found


async def test_service_terms_of_established_companies() -> None:
    nl, _ = await features_of(ServiceTermsSkill(), "nl-installer-established")
    us, _ = await features_of(ServiceTermsSkill(), "us-plumber-established")

    assert {k for k, v in nl.items() if v} == {"service_terms.written_quote"}
    assert {k for k, v in us.items() if v} == {"service_terms.written_quote"}


@pytest.mark.parametrize(
    ("text", "feature"),
    [
        ("Wij vragen 50% aanbetaling voor aanvang.", "upfront_payment"),
        ("A deposit of 20% secures your date.", None),
        ("Alleen contant te betalen.", "cash_only"),
        ("Geen voorrijkosten in de regio.", "bait_callout"),
        ("Call-out fee: $95.", None),
        ("Prijs op aanvraag.", "vague_pricing"),
        ("Vaste prijs vooraf afgesproken.", "written_quote"),
        ("Wij zijn 24/7 bereikbaar en binnen 15 minuten bij u.", "emergency_pressure"),
        ("Wij zijn 24/7 bereikbaar.", None),
    ],
)
async def test_service_terms_wording(text: str, feature: str | None) -> None:
    state: Any = {"facts": PageFacts(), "cleaned_content": text, "page_type": "service_site"}

    out = await ServiceTermsSkill().run(state)

    fired = {k.removeprefix("service_terms.") for k, v in out["features"].items() if v}
    assert fired == ({feature} if feature else set())


async def test_templated_testimonials() -> None:
    nl, nl_found = await features_of(TestimonialsSkill(), "nl-locksmith-doorway")
    real, real_found = await features_of(TestimonialsSkill(), "nl-installer-established")

    assert nl["testimonials.copied_share"] == 1.0 and nl["testimonials.all_five"] == 1
    assert nl["testimonials.repeated_names"] == 1 and nl["testimonials.placeholder_names"] == 1
    assert nl["testimonials.dates_clustered"] == 1
    assert {"copied_testimonials", "all_five", "placeholder_names", "dates_clustered"} <= nl_found
    assert real["testimonials.copied_share"] == 0 and not real_found


async def test_platform_reviews_are_compared_too() -> None:
    features, found = await features_of(TestimonialsSkill(), "angi-profile", "service_listing")

    assert features["testimonials.copied_share"] == 1.0 and "copied_testimonials" in found
    assert features["testimonials.repeated_names"] == 0  # no reviewer names are read on a platform


async def test_platform_profile() -> None:
    werkspot, found = await features_of(PlatformProfileSkill(), "werkspot-profile", "service_listing")
    angi, _ = await features_of(PlatformProfileSkill(), "angi-profile", "service_listing")

    assert werkspot["platform_profile.member_years"] > 9 and werkspot["platform_profile.new_member"] == 0
    assert werkspot["platform_profile.verified_badges"] == 1 and {"member_since", "rating", "badges"} <= found
    assert angi["platform_profile.new_member"] == 1 and angi["platform_profile.few_reviews"] == 1
    assert angi["platform_profile.low_response_rate"] == 1


def test_the_photo_looked_up_on_a_service_page_is_not_its_logo() -> None:
    facts = facts_of("nl-locksmith-doorway")

    assert main_photo(facts, "service_site") == "https://slotenmaker-utrecht-spoed.nl/img/busje.jpg"
    assert main_photo(facts, "storefront") == "https://slotenmaker-utrecht-spoed.nl/img/logo.png"


# ── Operator linking ────────────────────────────────────────────────────────────────────────────────────────


async def test_a_service_sites_contacts_include_national_numbers_and_its_email_domain() -> None:
    facts = PageFacts(
        url="https://loodgieter-a.nl/",
        phones=[Phone(number="+31302345678", kind="local", country="NL")],
        emails=[Email(address="info@callcentre.nl", kind="other"), Email(address="a@gmail.com", kind="webmail")],
    )
    state = {"url": facts.url, "facts": facts, "page_type": "service_site", "page_content": "<p>Bel 030 234 5678</p>"}

    found = await shop_store.page_links(state)

    assert found is not None and {"phone", "email", "email_domain"} <= set(found.links)
    assert len(found.links["email_domain"]) == 1  # callcentre.nl; gmail.com is shared by millions
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for k in ("phone", "email_domain") for v in found.links[k])


async def test_service_sites_of_one_operator_are_linked(setup: MemoryShopStore) -> None:
    first = await check("nl-locksmith-doorway", age=0.3)
    assert first["verdict"]["band"] == "junk_or_scam"
    assert "slotenmaker-utrecht-spoed.nl" in setup.shops  # a service site is recorded like a shop

    second = await check("nl-locksmith-doorway", age=0.3, url="https://slotenmaker-amersfoort-snel.nl/")

    assert second["features"]["shop_links.linked_shops"] == 1
    evidence = " ".join(f.evidence for f in second["findings"] if f.skill == "shop_links")
    assert "other site" in evidence and "slotenmaker-utrecht-spoed.nl" in evidence
    assert "0900" not in evidence and "1234567" not in evidence  # contacts are counted, never shown
    reasons = {r["type"] for r in second["verdict"]["reasons"]}
    assert {"doorway_pages", "linked_sites"} <= reasons


async def test_platform_profiles_are_never_recorded(setup: MemoryShopStore) -> None:
    await check("werkspot-profile", age=None)

    assert setup.shops == {}


# ── Verdict ─────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "age", "band"),
    [
        ("nl-installer-established", 20.0, "reliable"),
        ("nl-installer-established", None, "concerns"),  # no domain age: not enough to say it looks fine
        ("nl-installer-established", 0.2, "concerns"),
        ("us-plumber-established", 15.0, "reliable"),
        ("nl-locksmith-doorway", 6.0, "junk_or_scam"),
        ("us-locksmith-doorway", 0.2, "junk_or_scam"),
        ("werkspot-profile", None, "reliable"),
        ("thumbtack-profile", None, "reliable"),
        ("angi-profile", None, "concerns"),
    ],
)
async def test_verdict_bands_on_fixture_pages(name: str, age: float | None, band: str) -> None:
    result = await check(name, age=age)

    assert result["page_type"] == ("service_listing" if "profile" in name else "service_site")
    verdict = result["verdict"]
    assert verdict["band"] == band, verdict
    assert result["explanation"] == verdict["headline"]
    assert shopper.fit_headline(verdict["headline"]) == verdict["headline"]
    assert settings.verdict_careful_min <= result["score"] or band == "junk_or_scam"
    if band == "reliable":
        assert verdict["positives"] and len(verdict["reasons"]) <= 1


async def test_a_reliable_dutch_company_shows_its_positives() -> None:
    verdict = (await check("nl-installer-established", age=20.0))["verdict"]

    assert {p["type"] for p in verdict["positives"]} == {"service_established", "service_identity_nl", "written_quote"}


async def test_the_doorway_locksmiths_reasons() -> None:
    verdict = (await check("nl-locksmith-doorway", age=6.0))["verdict"]

    assert [r["type"] for r in verdict["reasons"]] == ["doorway_pages", "premium_number", "cash_only"]
    assert "10 towns" in verdict["reasons"][0]["detail"]


def _verdict(
    features: dict[str, float], findings: list[Finding] | None = None, page_type: str = "service_site"
) -> Any:
    return services.build(features=features, findings=findings or [], facts=PageFacts(), page_type=page_type)[1]


LINKED = Finding(
    skill="shop_links", type="shared_identifier", evidence="Shares its phone with 3 other sites: a, b, c."
)
PHOTO = Finding(
    skill="reverse_image", type="photo_on_other_shops", evidence="The same photo is on 2 other sites: a, b."
)


@pytest.mark.parametrize(
    ("features", "findings", "band"),
    [
        ({"doorway_pages.doorway": 1, "shop_links.linked_shops": 3}, [LINKED], "junk_or_scam"),
        ({"service_terms.premium_number": 1, "service_terms.upfront_payment": 1}, [], "junk_or_scam"),
        ({"service_terms.premium_number": 1, "service_terms.cash_only": 1}, [], "junk_or_scam"),
        ({"reverse_image.other_shops": 2}, [PHOTO], "careful"),
        ({"reverse_image.other_shops": 2, "domain_age.under_6_months": 1}, [PHOTO], "junk_or_scam"),
        ({"doorway_pages.doorway": 1}, [], "careful"),
        ({"service_terms.cash_only": 1, "service_terms.quote_on_site": 1}, [], "careful"),
        ({"service_terms.cash_only": 1}, [], "concerns"),
        ({"shop_links.linked_shops": 3}, [LINKED], "concerns"),
        ({"service_identity.kvk_shown": 0, "service_identity.address_shown": 0}, [], "concerns"),
        ({}, [], "concerns"),
    ],
)
def test_verdict_rules(features: dict[str, Any], findings: list[Finding], band: str) -> None:
    assert _verdict(features, findings).band == band


def test_looks_fine_needs_concrete_positives() -> None:
    fine: dict[str, Any] = {
        "domain_age.age_years": 8,
        "service_identity.complete": 1,
        "service_identity.market_nl": 1,
        "service_identity.kvk_shown": 1,
        "service_identity.address_shown": 1,
        "doorway_pages.street_address": 1,
    }
    assert _verdict(fine).band == "reliable"
    for missing in ("domain_age.age_years", "service_identity.complete", "doorway_pages.street_address"):
        assert _verdict({k: v for k, v in fine.items() if k != missing}).band == "concerns", missing
    assert _verdict(fine | {"doorway_pages.doorway": 1}).band == "careful"
    assert _verdict(fine | {"service_terms.upfront_payment": 1}).band == "concerns"
    # One small note is fine; it's shown with the positives.
    noted = _verdict(fine | {"service_terms.bait_callout": 1})
    assert noted.band == "reliable" and [r.type for r in noted.reasons] == ["bait_callout"]


def test_absence_alone_never_warns() -> None:
    verdict = _verdict(
        {"service_identity.kvk_shown": 0, "service_identity.address_shown": 0, "domain_age.age_years": 9}
    )

    assert verdict.band == "concerns"
    assert [r.type for r in verdict.reasons] == ["kvk_missing", "no_street_address"]


def test_service_copy_is_factual_and_never_says_scam() -> None:
    catalogue = shopper.catalogue()
    texts = [catalogue["reasons"][k]["title"] + catalogue["reasons"][k]["detail"] for k in services.REASON_SEVERITY]
    texts += [catalogue["positives"][k]["title"] + catalogue["positives"][k]["detail"] for k in services.POSITIVE_KEYS]
    texts += list(catalogue["service_headlines"].values())
    assert not [t for t in texts if re.search(r"scam|fraud|oplicht|criminal", t, re.IGNORECASE)]
    for headline in catalogue["service_headlines"].values():
        assert shopper.fit_headline(headline) == headline


# ── Graph, flag, streaming, privacy ─────────────────────────────────────────────────────────────────────────


async def test_the_photo_search_runs_when_the_cheap_skills_found_something() -> None:
    class _Photo(Skill):
        name = "reverse_image"
        description = "stub"
        tier = "expensive"
        page_types = SERVICE_PAGES
        calls = 0

        async def run(self, state: AnalysisState) -> SkillOutput:
            type(self).calls += 1
            return self.output({"searched": True, "other_shops": 0})

    html, url = page("nl-locksmith-doorway")
    await build_graph([DoorwayPagesSkill(), _Photo()]).ainvoke({"page_content": html, "url": url, "flags": []})
    assert _Photo.calls == 1

    html, url = page("nl-installer-established")
    await build_graph([DoorwayPagesSkill(), _Photo()]).ainvoke({"page_content": html, "url": url, "flags": []})
    assert _Photo.calls == 1  # nothing to confirm: no photo search


async def test_without_the_flag_no_service_skill_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", False)

    result = await check("nl-installer-established", age=20.0)

    assert result["page_type"] not in {"service_site", "service_listing"}
    assert not [r for r in result["skill_results"] if r.startswith(("[service_identity]", "[doorway_pages]"))]


async def test_service_steps_stream() -> None:
    from unfret.routes import analyze  # noqa: PLC0415 - keep the app out of import time for the other tests

    html, url = page("nl-locksmith-doorway")
    final: dict[str, Any] = {}
    events: list[str] = []
    graph = build_graph(skills(1.0))
    async for _, update in graph.astream(
        {"page_content": html, "url": url, "flags": []}, stream_mode="updates", subgraphs=True
    ):
        assert isinstance(update, dict)
        events += analyze._update_events(update, final)

    steps = " ".join(e for e in events if e.startswith("event: step"))
    for skill in ("service_identity", "doorway_pages", "service_terms", "testimonials"):
        assert f'"skill": "{skill}"' in steps
    assert '"page_type": "service_site"' in steps
    assert final["verdict"]["band"] == "junk_or_scam" and final[analyze._PRIVATE] is True


def test_service_results_get_no_share_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from unfret.routes import analyze  # noqa: PLC0415 - keep the app out of import time for the other tests

    monkeypatch.setattr(settings, "session_secret", SecretStr("test-session-secret"))
    body = AnalyzeRequest(url="https://slotenmaker.example/", page_content="<p>x</p>")
    result = {"score": 65, "explanation": "x", "verdict": None, "page_type": "service_site", "facts": PageFacts()}
    shop = {**result, "page_type": "storefront"}

    assert analyze._response(analyze._kept(result), 0, charged=True, shared_from=body).share_token is None
    assert analyze._response(analyze._kept(shop), 0, charged=True, shared_from=body).share_token is not None


async def test_admin_forgets_an_email_domain(setup: MemoryShopStore, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx  # noqa: PLC0415 - only this test talks to the app

    from unfret.main import app  # noqa: PLC0415 - keep the app out of import time for the other tests

    await check("nl-installer-established", age=20.0)
    assert any(kind == "email_domain" for kind, _, _ in setup.links)
    monkeypatch.setattr(settings, "admin_token", SecretStr("operator-token"))
    auth = {"Authorization": "Bearer operator-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"kind": "email_domain", "value": "@Devries-Installatie.nl"}
        assert (await client.post("/v1/admin/contacts/forget", json=body, headers=auth)).json() == {"removed": 1}
        bad = {"kind": "email_domain", "value": "info@devries-installatie.nl"}
        assert (await client.post("/v1/admin/contacts/forget", json=bad, headers=auth)).status_code == 422
    assert not any(kind == "email_domain" for kind, _, _ in setup.links)
