"""
False warnings on product pages of established shops, from real pages checked on dev (2026-11-16).

ikea.nl (reviews "about something else", no company details), bever.nl (no company details, "text aimed at AI",
"registered for a year" on a 29-year-old domain), fonq.nl (HKliving "isn't a recognizable brand") and mrmarvis.nl
("little shop information" next to "shop details present").
"""

from typing import Any

import pytest

from unfret import fact_cache, shopper
from unfret.config import settings
from unfret.facts import PageFacts, bolcom, parse_page
from unfret.graph.skills import eu_registry
from unfret.graph.skills.eu_registry import EuRegistrySkill, legal_page_urls, shows_imprint
from unfret.graph.state import Finding

BEVER_LINKS = {
    "refund": "https://www.bever.nl/klantenservice/retourneren.html",
    "contact": "https://www.bever.nl/klantenservice/contactgegevens.html",
}


def build(features: dict[str, float], *, page_type: str = "storefront", score: int = 55, **kw: Any) -> shopper.Verdict:
    return shopper.build(
        score,
        features=features,
        findings=kw.pop("findings", []),
        facts=kw.pop("facts", PageFacts()),
        page_type=page_type,
        **kw,
    )


def types(verdict: shopper.Verdict) -> set[str]:
    return {r.type for r in verdict.reasons}


# ── Off-topic reviews ─────────────────────────────────────────────────────────


def test_off_topic_reviews_on_a_shops_own_page_need_a_majority() -> None:
    assert "off_topic_reviews" not in types(build({"reviews.off_topic_share": 0.4}))
    assert "off_topic_reviews" in types(build({"reviews.off_topic_share": 0.6}))
    assert "off_topic_reviews" in types(build({"reviews.off_topic_share": 0.35}, page_type="aggregate_listing"))
    assert shopper.REASON_SEVERITY["off_topic_reviews"] == "medium"


# ── No company details ────────────────────────────────────────────────────────


def test_no_imprint_is_not_said_when_the_page_links_to_unread_legal_pages() -> None:
    features = {"eu_registry.imprint": 0.0, "eu_registry.withdrawal_right": 1.0}
    assert "no_imprint" not in types(build(features, facts=PageFacts(policy_links=BEVER_LINKS)))
    # Read and empty: said, with where it looked.
    checked = build(features | {"eu_registry.legal_pages_checked": 1.0}, facts=PageFacts(policy_links=BEVER_LINKS))
    reason = next(r for r in checked.reasons if r.type == "no_imprint")
    assert reason.detail == "No company name or address found on the page or on the shop's contact and terms pages."
    # No legal page to look at: the page is all there is.
    alone = build(features, facts=PageFacts(policy_links={"refund": BEVER_LINKS["refund"]}))
    assert next(r for r in alone.reasons if r.type == "no_imprint").detail.endswith("found on the page.")


def test_no_imprint_is_not_said_for_a_vat_registered_shop() -> None:
    features = {"eu_registry.imprint": 0.0, "eu_registry.vat_valid": 1.0}
    assert "no_imprint" not in types(build(features))


def test_no_imprint_is_not_said_for_a_long_established_shop() -> None:
    # ikea.nl: its contact and terms pages render the company details with scripts, so reading them finds nothing.
    features = {"eu_registry.imprint": 0.0, "eu_registry.legal_pages_checked": 1.0}
    facts = PageFacts(policy_links=BEVER_LINKS)
    assert "no_imprint" in types(build(features | {"domain_age.age_years": 4.0}, facts=facts))
    assert "no_imprint" not in types(build(features | {"domain_age.age_years": 31.3}, facts=facts))
    own_site = {"brand_presence.shop_is_brand": 1.0, "brand_presence.wikidata": 1.0}
    assert "no_imprint" not in types(build(features | own_site, facts=facts))


def test_legal_page_urls_stay_on_the_shop_and_skip_pdfs() -> None:
    links = {
        "contact": "https://www.fonq.nl/veelgesteldevragen/contact-fonq",
        "terms": "https://documents.cdn.etrias.nl/nl/terms/Algemene_voorwaarden_fonQ-NL.pdf",
    }
    assert legal_page_urls(links, "https://www.fonq.nl/products/x") == [links["contact"]]
    pdf = {"terms": "https://www.shop.nl/voorwaarden.PDF?v=2"}
    assert legal_page_urls(pdf, "https://www.shop.nl/p/1") == []
    assert legal_page_urls(BEVER_LINKS, "") == []


def test_shows_imprint_reads_a_register_number_or_vat_number() -> None:
    assert shows_imprint("<p>Bever B.V., KvK-nummer 12345678</p>", "https://www.bever.nl/c")
    assert shows_imprint("<p>BTW: NL810433941B01</p>", "https://www.bever.nl/c")
    assert not shows_imprint("<p>Bel ons of mail ons.</p>", "https://www.bever.nl/c")


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    fact_cache.use_store(fact_cache.MemoryStore())
    monkeypatch.setattr(settings, "network_lookups", True)
    fetched: list[str] = []

    async def fetch(url: str, **_: Any) -> bytes:
        fetched.append(url)
        if "contactgegevens" in url:
            return b"<html><body>JD Sports Fashion B.V. Kamer van Koophandel 12345678</body></html>"
        raise TimeoutError

    monkeypatch.setattr(eu_registry, "fetch_public", fetch)
    return fetched


def _state(facts: PageFacts, url: str = "https://www.bever.nl/p/aevor-sling-bag.html") -> Any:
    return {
        "page_content": "",
        "url": url,
        "flags": [],
        "facts": facts,
        "cleaned_content": "",
        "page_type": "storefront",
    }


async def test_company_details_on_the_contact_page_count(lookups: list[str]) -> None:
    out = await EuRegistrySkill().run(_state(PageFacts(policy_links=BEVER_LINKS)))
    assert out["features"]["eu_registry.imprint"] == 1.0
    assert out["features"]["eu_registry.legal_pages_checked"] == 1.0
    assert "imprint_on_legal_page" in {f.type for f in out["findings"]}
    assert lookups == [BEVER_LINKS["contact"]]
    await EuRegistrySkill().run(_state(PageFacts(policy_links=BEVER_LINKS), "https://www.bever.nl/p/other.html"))
    assert len(lookups) == 1  # cached per shop domain


async def test_unreadable_legal_pages_are_no_finding(lookups: list[str]) -> None:
    links = {"terms": "https://www.shop.nl/voorwaarden"}
    out = await EuRegistrySkill().run(_state(PageFacts(policy_links=links), "https://www.shop.nl/p/1"))
    assert out["features"]["eu_registry.imprint"] == 0.0
    assert "eu_registry.legal_pages_checked" not in out["features"]


async def test_the_quick_tier_and_evaluations_read_no_legal_pages(
    lookups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await EuRegistrySkill(vies=False).run(_state(PageFacts(policy_links=BEVER_LINKS)))
    monkeypatch.setattr(settings, "network_lookups", False)
    await EuRegistrySkill().run(_state(PageFacts(policy_links=BEVER_LINKS)))
    assert lookups == []


async def test_an_imprint_on_the_page_needs_no_legal_page(lookups: list[str]) -> None:
    facts = PageFacts(policy_links=BEVER_LINKS, legal_mentions=["KvK 12345678"])
    out = await EuRegistrySkill().run(_state(facts))
    assert out["features"]["eu_registry.imprint"] == 1.0
    assert lookups == []


# ── Brands ────────────────────────────────────────────────────────────────────

HKLIVING = PageFacts(brand="HKLIVING", title="HKliving 70's Ceramics Mokken 0,23 L - Set van 6 - Surreal")
GENERATED = [Finding(skill="listing", type="brand_name", evidence='The brand "HKLIVING" looks generated.')]


@pytest.mark.parametrize(
    "presence",
    [
        {"brand_presence.wikidata": 1.0},
        {"brand_presence.own_domain_age_years": 18.9},
        {"content.brand_recognized": 1.0},
    ],
)
def test_a_brand_that_exists_is_not_called_unknown(presence: dict[str, float]) -> None:
    verdict = build(
        {"listing.generated_brand": 0.6, "content.generic_brand": 0.5} | presence, findings=GENERATED, facts=HKLIVING
    )
    assert "generic_brand" not in types(verdict)


def test_a_generated_looking_brand_without_any_presence_is_still_unknown() -> None:
    verdict = build({"brand_presence.own_domain_age_years": 1.0}, findings=GENERATED, facts=HKLIVING)
    assert "generic_brand" in types(verdict)


def test_known_brand_positive_once_wikidata_has_it() -> None:
    verdict = build({"brand_presence.wikidata": 1.0}, findings=GENERATED, facts=HKLIVING, score=90)
    assert "known_brand" in {p.type for p in verdict.positives}


# ── Text aimed at AI ──────────────────────────────────────────────────────────


def test_ai_text_needs_the_quoted_phrase_not_the_llms_impression() -> None:
    assert "ai_text" not in types(build({"content.addresses_ai": 1.0}))
    assert "ai_text" in types(build({"injection.instruction_visible": 1.0}))


# ── Domain registration ───────────────────────────────────────────────────────


def test_an_unknown_term_or_an_old_domain_is_never_a_short_registration() -> None:
    old = {"domain_age.known": 1.0, "domain_age.age_years": 29.7}
    assert "short_registration" not in types(build(old | {"domain_age.term_years": 0.0}))  # .nl: no expiry published
    assert "short_registration" not in types(build(old | {"domain_age.term_years": 1.0}))
    young = {"domain_age.known": 1.0, "domain_age.age_years": 0.8, "domain_age.term_years": 1.0}
    reason = next(r for r in build(young).reasons if r.type == "short_registration")
    assert "less than a year ago" in reason.detail


# ── Shop information ──────────────────────────────────────────────────────────


def test_missing_and_present_trust_info_are_mutually_exclusive() -> None:
    present = {"eu_registry.withdrawal_right": 1.0, "eu_registry.imprint": 1.0, "content.missing_trust_info": 1.0}
    verdict = build(present, score=90)
    assert "missing_trust_info" not in types(verdict)
    assert "trust_info_present" in {p.type for p in verdict.positives}
    lacking = {"eu_registry.withdrawal_right": 1.0, "eu_registry.imprint": 0.0, "content.missing_trust_info": 1.0}
    missing = build(lacking, facts=PageFacts(policy_links=BEVER_LINKS))
    assert "missing_trust_info" in types(missing)
    assert "trust_info_present" not in {p.type for p in missing.positives}


def test_a_bedrijfsgegevens_link_is_the_imprint() -> None:
    # mrmarvis.nl's footer: "Bedrijfsgegevens" (company details) is its imprint page.
    facts, _ = parse_page(
        '<footer><a href="/nl-nl/bedrijfsgegevens">Bedrijfsgegevens</a></footer>', "https://www.mrmarvis.nl/"
    )
    assert facts.policy_links["imprint"] == "https://www.mrmarvis.nl/nl-nl/bedrijfsgegevens"


@pytest.mark.parametrize(
    ("block", "name"),
    [
        ("Verkoop door Light Gallery NL beoordeeld met een", "Light Gallery NL"),  # bol.com, 2026-11
        ("Verkoop door Light Gallery NL beoordeeld met een 8,5", "Light Gallery NL"),
        ("Verkocht door KoffieShop BV 9,1", "KoffieShop BV"),
        ("KoffieShop BV", "KoffieShop BV"),
        ("bol", "bol"),
    ],
)
def test_bol_seller_name_drops_the_label_and_rating(block: str, name: str) -> None:
    assert bolcom.seller_name(block) == name
