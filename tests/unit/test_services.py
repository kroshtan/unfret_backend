"""Service providers: extraction (testimonials, platform profiles), the skills, operator linking and the verdict."""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, SecretStr

from tests.conftest import page_fetcher
from unfret import fact_cache, page_fetch, shop_store, shopper
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.facts.schema import Email, Link, Phone, Testimonial
from unfret.facts.service_platforms import profile
from unfret.graph import build_graph, nodes, services
from unfret.graph.platforms import platform_rule
from unfret.graph.skills import SERVICE_PAGES, Skill, SkillOutput
from unfret.graph.skills import platform_profile as platform_profile_module
from unfret.graph.skills.dark_patterns import DarkPatternsSkill
from unfret.graph.skills.doorway_pages import DoorwayPagesSkill, address_elsewhere, city_pages, trade_town_name
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
from unfret.page_fetch import is_challenge_page
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
    "werkspot-new-profile": "https://www.werkspot.nl/profiel/slotenservice-direct-7809630",
    "werkspot-rated-profile": "https://www.werkspot.nl/profiel/bos-elektrotechniek-2152848",
    "homeguide-profile": "https://homeguide.com/co/denver/plumbers/mile-high-rooter-llc",
    "houzz-profile": "https://www.houzz.com/professionals/kitchen-and-bath-remodelers/lakeside-remodeling-llc-pfvwus-pf~1234567",
    "homeadvisor-profile": "https://www.homeadvisor.com/rated.FrontRangeHeating.12345678.html",
    "nl-locksmith-callcentre": "https://zwolle-slotenmakers.nl/inbraakschade/",
    "nl-locksmith-callcentre-plain": "https://zwolle-slotenmakers.nl/inbraakschade/",
    "nl-keyword-domain-established": "https://www.loodgieter-amsterdam.nl/",
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
            if schema is nodes._NewClassOutput:  # the second step, for a "shop" without anything to buy
                kind = self.page_type if self.page_type in {"service_site", "service_listing"} else "none"
                return schema(page_type=kind, reasoning="r")
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
        ("angi-profile", None, "careful"),  # a month-old profile whose three reviews are one text
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


def test_text_aimed_at_ai_needs_the_injection_skills_quote() -> None:
    # The content read's yes/no "addresses an AI" has no quote to show; product pages dropped it for that reason.
    assert "ai_text" not in {r.type for r in _verdict({"content.addresses_ai": 1}).reasons}
    assert "ai_text" in {r.type for r in _verdict({"injection.instruction_visible": 1}).reasons}


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
    # Progress counts the checks the result then says it ran (the content read and triage are steps, not checks).
    done = sum(json.loads(e.split("data: ", 1)[1])["check"] for e in events if e.startswith("event: step"))
    assert done == final["verdict"]["checks_run"] > 0


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


def test_street_addresses_written_out_in_the_footer_are_read() -> None:
    html = """<html><body><h1>Plumbing</h1><p>We fix leaks.</p><footer>
    <p>Austin<br>12111 Menchaca Rd<br>Austin, Texas 78748<br><a href="tel:5125550100">(512) 555-0100</a></p>
    <p>Waco<br>2505 Washington Ave, Ste 3<br>Waco, TX 76710</p>
    <p>Gascontrol B.V.<br>Tingietersweg 129<br>2031 ER Haarlem</p>
    <p>Postbus 123, 1000 AB Amsterdam</p>
    <p>Master License #MP.00188386</p>
    </footer></body></html>"""

    facts = parse_page(html, "https://example-plumbing.com/")[0]

    assert facts.addresses == [
        "12111 Menchaca Rd Austin, Texas 78748",
        "2505 Washington Ave, Ste 3 Waco, TX 76710",
        "Tingietersweg 129 2031 ER",
    ]
    assert [lic.number for lic in facts.licences] == ["MP.00188386"]


def test_an_address_outside_the_business_areas_is_not_read() -> None:
    html = """<html><body><main><p>Deliver to 12 Elm St Springfield, IL 62701</p></main>
    <footer><p>Call us</p></footer></body></html>"""

    assert parse_page(html, "https://example-plumbing.com/")[0].addresses == []


async def test_town_pages_with_a_written_out_address_are_not_a_doorway() -> None:
    towns = ("Austin", "Round Rock", "Cedar Park", "Pflugerville", "Georgetown", "Leander")
    links = [Link(text=f"Plumber {town}", url=f"https://x.com/plumber-{town.lower()}") for town in towns]
    facts = PageFacts(url="https://x.com/", internal_links=links, addresses=["12111 Menchaca Rd Austin, Texas 78748"])
    out = await DoorwayPagesSkill().run({"facts": facts, "url": facts.url})  # type: ignore[typeddict-item]

    assert out["features"]["doorway_pages.doorway"] == 0


async def test_service_terms_leave_the_customers_testimonials_out() -> None:
    quote = "Hij was er binnen 20 minuten en ik heb niks af hoeven rekenen. Geen voorrijkosten niets."
    facts = PageFacts(testimonials=[Testimonial(text=quote, name="Jan")], promises=[f"... {quote} ..."])
    text = f"Loodgieter Utrecht. Wij zijn 24/7 bereikbaar. {quote} Vraag een offerte aan."
    state: Any = {"facts": facts, "cleaned_content": text, "page_type": "service_site"}

    out = await ServiceTermsSkill().run(state)

    assert out["features"]["service_terms.bait_callout"] == 0
    assert out["features"]["service_terms.emergency_pressure"] == 0


# ── Platform profiles as the platforms write them, and the call centre's single page ────────────────────────


@pytest.mark.parametrize(
    ("name", "platform", "member_since", "jobs", "rating", "count", "badges", "years"),
    [
        ("werkspot-new-profile", "werkspot", date(2026, 10, 1), 0, None, 0, [], None),
        ("werkspot-rated-profile", "werkspot", date(2012, 1, 1), 356, 4.8, 163, ["KvK-nummer gecontroleerd"], None),
        ("homeguide-profile", "homeguide", None, 41, 4.8, 27, [], 12),
        ("houzz-profile", "houzz", None, None, 4.9, 38, ["Best of Houzz", "Verified License"], None),
        ("homeadvisor-profile", "homeadvisor", None, None, 4.6, 312, ["Screened & Approved", "Elite Service",
         "Top Rated"], 15),
    ],
)  # fmt: skip
def test_platform_wording(
    *,
    name: str,
    platform: str,
    member_since: date | None,
    jobs: int | None,
    rating: float | None,
    count: int,
    badges: list[str],
    years: int | None,
) -> None:
    facts = facts_of(name)
    profile = facts.seller_profile

    assert facts.platform == platform and profile is not None
    assert (profile.member_since, profile.jobs_completed, profile.rating, profile.rating_count) == (
        member_since,
        jobs,
        rating,
        count,
    )
    assert (profile.badges, profile.years_in_business) == (badges, years)


def test_a_rating_is_never_the_first_digit_of_the_count() -> None:
    # "4.5 ★ uit 160 beoordelingen" was read as a 1 from 60 reviews.
    found = profile("Lid sinds 2012 4.5 ★ uit 160 beoordelingen", [])
    assert found is not None and (found.rating, found.rating_count) == (4.5, 160)
    thumbtack = profile("Joined 2013 4.5 ★ (350 reviews) Responds within about 2 hours", [])
    assert thumbtack is not None and (thumbtack.rating, thumbtack.rating_count) == (4.5, 350)
    assert thumbtack.response_time == "within about 2 hours"
    brackets = profile("Exceptional 4.9 ★★★★★ (143)", [])
    assert brackets is not None and (brackets.rating, brackets.rating_count) == (4.9, 143)
    empty = profile("Reviews 0.0 (0 reviews)", [])
    assert empty is not None and (empty.rating, empty.rating_count) == (None, 0)


def test_a_homeguide_profile_is_a_service_listing() -> None:
    facts = facts_of("homeguide-profile")
    assert platform_rule(facts, URLS["homeguide-profile"], "") == ("service_listing", "HomeGuide")
    assert platform_rule(facts, "https://homeguide.com/co/denver/plumbers/", "") is None  # a city's list of pros


@pytest.mark.parametrize(
    ("name", "band", "reasons"),
    [
        # New, no reviews, "€0 call-out, price agreed on the spot", details only on request.
        ("werkspot-new-profile", "junk_or_scam", {"unproven_profile", "details_on_request", "quote_on_site"}),
        ("werkspot-rated-profile", "reliable", set()),
        ("homeadvisor-profile", "reliable", set()),
        ("homeguide-profile", "reliable", set()),
        ("houzz-profile", "concerns", set()),  # no years on the platform or in business shown
    ],
)
async def test_platform_profile_bands(name: str, band: str, reasons: set[str]) -> None:
    result = await check(name)

    assert result["page_type"] == "service_listing"
    verdict = result["verdict"]
    assert verdict["band"] == band, verdict
    assert reasons <= {r["type"] for r in verdict["reasons"]}


async def test_a_new_profile_alone_is_only_something_to_check() -> None:
    html, url = page("werkspot-new-profile")
    html = html.replace(
        "24/7 spoedservice, binnen 20 minuten bij u. Voorrijkosten € 0. Prijs in overleg ter plaatse.", ""
    )
    html = html.replace("Bedrijfsgegevens op aanvraag.", "Vooraf een vaste prijs.")
    result = await build_graph(skills(None)).ainvoke({"page_content": html, "url": url, "flags": []})

    assert result["verdict"]["band"] == "concerns"
    assert [r["type"] for r in result["verdict"]["reasons"]] == ["unproven_profile"]


@pytest.mark.parametrize(
    ("name", "band", "reasons"),
    [
        # Says its locksmith isn't necessarily in Zwolle: with a trade-and-town name and only an 085 number.
        ("nl-locksmith-callcentre", "junk_or_scam", {"not_local", "trade_town_name"}),
        # The same page without that sentence: a trade-and-town site with no KvK number and no address.
        ("nl-locksmith-callcentre-plain", "careful", {"trade_town_name", "national_number_only"}),
        # A keyword domain of a real firm: KvK number, its own address in the town, a local number.
        ("nl-keyword-domain-established", "reliable", set()),
    ],
)
async def test_call_centre_marks_on_one_page(name: str, band: str, reasons: set[str]) -> None:
    result = await check(name, age=12.0)

    assert result["page_type"] == "service_site"
    verdict = result["verdict"]
    assert verdict["band"] == band, verdict
    assert reasons <= {r["type"] for r in verdict["reasons"]}
    assert "scam" not in verdict["headline"].lower()


async def test_call_centre_features() -> None:
    features, found = await features_of(DoorwayPagesSkill(), "nl-locksmith-callcentre")

    assert features["doorway_pages.trade_town_name"] == 1.0
    assert features["doorway_pages.not_local"] == 1.0
    assert features["doorway_pages.national_number_only"] == 1.0
    assert {"trade_town_name", "not_local", "national_number_only"} <= found
    established, _ = await features_of(DoorwayPagesSkill(), "nl-keyword-domain-established")
    assert established["doorway_pages.trade_town_name"] == 1.0  # the domain is a keyword one…
    assert established["doorway_pages.national_number_only"] == 0.0  # …but the number is local
    assert established["doorway_pages.address_elsewhere"] == 0.0


def test_trade_town_names() -> None:
    assert trade_town_name("https://zwolle-slotenmakers.nl/x/", "Inbraakschade in Zwolle") == (True, "zwolle")
    assert trade_town_name("https://denbosch-slotenmakers.nl/", "Sloten vervangen in Den Bosch") == (True, "denbosch")
    assert trade_town_name("https://www.slotenmakerijarnhem.nl/", "Uw slotenmaker in Arnhem") == (True, "arnhem")
    assert trade_town_name("https://kerkradeslotenmaker.nl/", "Sloten in Kerkrade") == (True, "kerkrade")
    assert trade_town_name("https://www.loodgieters.nl/", "Loodgieters.nl") == (True, "")
    assert trade_town_name("https://spoedslotenmakeramsterdam.com/", "Slotenmaker in Amsterdam") == (True, "amsterdam")
    # A company or a person behind the trade, or a town the page never says it works in: not a trade-town name.
    assert trade_town_name("https://robisonplumbing.com/", "Robison Plumbing in Austin") is None
    assert trade_town_name("https://www.slotenmakergerard.nl/", "Slotenmaker Gerard") is None
    assert trade_town_name("https://www.jonker-schilders.nl/", "Welkom bij Jonker Schilders") is None
    assert address_elsewhere("barendrecht", ["Minoes erf 3 2907VG Capelle aan den IJssel"])
    assert not address_elsewhere("amsterdam", ["Kinkerstraat 112, 1053 ED, Amsterdam"])
    assert not address_elsewhere("nieuwegein", ["Nieuwe Boteringestraat 28 9712PM"])  # no town to compare


def test_a_town_called_evergreen_is_not_a_countdown() -> None:
    html = (
        '<html><body><h1>Plumber Denver</h1><script>var areas = ["Englewood,CO", "Evergreen,CO", "Golden,CO"];'
        "</script></body></html>"
    )

    assert parse_page(html, "https://www.example-plumber.com/denver/")[0].timers == []


def test_fastly_client_challenge_is_a_blocked_page() -> None:
    html = '<!doctype html><html><head><title>Client Challenge</title><link href="/_fs-ch-1T1/assets/styles.css">'
    assert is_challenge_page(html)


# Houzz's page frame without the provider (what a server gets for some /professionals/ pages): navigation, footer,
# and no rating, reviews or membership.
HOUZZ_FRAME = (
    "<html><head><title>Houzz - Home Design, Decorating and Remodeling Ideas</title></head><body>"
    "<header><a href='/'>Houzz</a><nav>Get Ideas · Find Professionals · Shop · Sign In</nav></header><main><p>"
    + "Find the right local pro for your project, browse photos and get inspired. " * 8
    + "</p></main><footer><p>© 2026 Houzz Inc.</p></footer></body></html>"
)
HOUZZ_PRO = "https://www.houzz.com/professionals/handyman/24-7-lightning-locksmith-chicago-pfvwus-pf~1707147861"


@pytest.mark.parametrize("expansion", [True, False])
def test_a_platform_frame_without_the_profile_is_withheld(monkeypatch: pytest.MonkeyPatch, expansion: bool) -> None:
    monkeypatch.setattr(settings, "expansion_classes", expansion)
    html, url = page("houzz-profile")

    assert page_fetch.is_withheld_profile(HOUZZ_FRAME, HOUZZ_PRO) == expansion  # off: as before the flag
    assert not page_fetch.is_withheld_profile(html, url)  # the profile came with it
    assert not page_fetch.is_withheld_profile(HOUZZ_FRAME, "https://www.houzz.com/photos/kitchen")  # not a profile


async def test_a_link_to_a_withheld_profile_is_blocked_before_any_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "expansion_classes", True)

    async def frame(url: str, **_: Any) -> bytes:
        return HOUZZ_FRAME.encode()

    monkeypatch.setattr(page_fetch, "fetch_public_page", page_fetcher(frame))
    with pytest.raises(page_fetch.PageUnreadableError) as raised:
        await page_fetch.fetch_page(HOUZZ_PRO)

    assert raised.value.reason == "PAGE_BLOCKED"
    assert "extension" in raised.value.detail and "houzz" not in raised.value.detail.lower()


async def test_a_profile_page_with_nothing_read_is_not_checked() -> None:
    # The extension's path: the page sent as it is, with nothing of the provider in it. Not "Worth a look"
    # about nothing: not analysed, so not charged.
    result = await build_graph(skills(None)).ainvoke({"page_content": HOUZZ_FRAME, "url": HOUZZ_PRO, "flags": []})

    assert result["page_type"] == "service_listing"
    assert result["score"] == -1
    assert result["explanation"] == services.PROFILE_NOT_READ
    assert (await check("houzz-profile"))["score"] >= 0  # a profile that was read is checked as before


def test_us_directionals_are_not_dutch_postcodes() -> None:
    facts = PageFacts(addresses=["2500 SW 7th Ave Amarillo, TX 79106"], phones=[Phone(number="+18063765098",
                      kind="local", country="US")])  # fmt: skip
    assert service_market(facts, "https://claytonsglass.com/") == "us"
