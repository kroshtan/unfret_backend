"""
The brand and the seller of a marketplace listing: bylines in every language, the sentence guard, seller vs brand.

All pages here are made up; they copy only the markup patterns of Amazon's product pages.
"""

from typing import Any

import pytest

from unfret import brands, shopper
from unfret.facts import PageFacts, parse_page
from unfret.facts.brand import clean_brand, plausible_brand, unwrap_byline
from unfret.graph import nodes
from unfret.graph.skills.listing import ListingSkill
from unfret.graph.state import Finding


@pytest.mark.parametrize(
    ("byline", "brand"),
    [
        ("Visit the Lumora Store", "Lumora"),
        ("Brand: Lumora", "Lumora"),
        ("Merk: Lumora", "Lumora"),
        ("Bezoek de Lumora-winkel", "Lumora"),
        ("Bezoek de Lumora winkel", "Lumora"),
        ("De Air Max Store openen", "Air Max"),
        ("De Lumora-Store openen", "Lumora"),
        ("Besuche den Lumora-Store", "Lumora"),
        ("Besuchen Sie den Lumora-Store", "Lumora"),
        ("Marke: Lumora", "Lumora"),
        ("Visiter la boutique Lumora", "Lumora"),
        ("Visitez la boutique Lumora", "Lumora"),
        ("Marque : Lumora", "Lumora"),
        ("Visita lo Store di Lumora", "Lumora"),
        ("Marca: Lumora", "Lumora"),
        ("Visita la tienda de Lumora", "Lumora"),
        ("Odwiedź sklep Lumora", "Lumora"),
        ("Marka: Lumora", "Lumora"),
        ("Besök Lumora-butiken", "Lumora"),
        ("Märke: Lumora", "Lumora"),
        ("Visite a loja da Lumora", "Lumora"),
        ("Lumora Mağazasını ziyaret edin", "Lumora"),
        ("Pokéball-Store", "Pokéball"),
        ("Lumora-winkel", "Lumora"),
        ("  Visit   the  Air  Max  Store ", "Air Max"),
        ("Lumora", "Lumora"),
    ],
)
def test_bylines_in_every_language(byline: str, brand: str) -> None:
    assert clean_brand(byline) == brand


@pytest.mark.parametrize(
    "name",
    [
        "Air Max Store openen",  # a phrase none of the patterns know is still no brand
        "Klik hier om de Lumora winkel te bezoeken",
        "Visit Lumora",
        "Tienda oficial Lumora",
        "The best nasal dilator for better sleep",  # more than five words
        "",
    ],
)
def test_a_sentence_or_a_store_link_is_no_brand(name: str) -> None:
    assert not plausible_brand(name)
    assert clean_brand(name) == ""


@pytest.mark.parametrize("name", ["The Body Shop", "Funky Flair Boutique LLC", "Levi Strauss & Co.", "3M", "Air Max"])
def test_real_names_with_store_words_stay(name: str) -> None:
    assert clean_brand(name) == name


def test_unwrap_leaves_other_names_alone() -> None:
    assert unwrap_byline("Etta James (Artist)") == "Etta James (Artist)"


def _amazon(byline: str, *, extra: str = "", href: str = "/stores/page/1") -> str:
    return f"""<html><head><link rel="canonical" href="https://www.amazon.nl/dp/B0FAKE0001"></head><body>
<span id="productTitle">Airmax neusspreider voor beter slapen</span>
<a id="bylineInfo" href="{href}">{byline}</a>
{extra}
<div offer-display-feature-name="desktop-merchant-info" class="offer-display-feature-label">Verkocht door</div>
<div offer-display-feature-name="desktop-merchant-info" class="offer-display-feature-text">
<span class="offer-display-feature-text-message">Airmax nasal dilator</span></div>
</body></html>"""


def test_the_dutch_store_byline_names_the_brand() -> None:
    facts, _ = parse_page(_amazon("De Air Max Store openen"), "https://www.amazon.nl/dp/B0FAKE0001")
    assert facts.brand == "Air Max"
    assert facts.seller == "Airmax nasal dilator"


def test_structured_brand_comes_before_the_byline() -> None:
    overview = """<div id="productOverview_feature_div"><table>
<tr class="a-spacing-small po-brand"><td><span>Merk</span></td><td><span>Airmax</span></td></tr></table></div>"""
    facts, _ = parse_page(_amazon("De Air Max Store openen", extra=overview), "https://www.amazon.nl/dp/B0FAKE0001")
    assert facts.brand == "Airmax"
    json_ld = '<script type="application/ld+json">{"@type": "Product", "name": "X", "brand": "Rhinomed"}</script>'
    facts, _ = parse_page(_amazon("Merk: Airmax", extra=json_ld + overview), "https://www.amazon.nl/dp/B0FAKE0001")
    assert facts.brand == "Rhinomed"
    filtered = "/s/ref=bl_dp_s_web_1?ie=UTF8&node=1&field-brandtextbin=Lumora+Care"
    facts, _ = parse_page(_amazon("Ga naar de winkel", href=filtered), "https://www.amazon.nl/dp/B0FAKE0001")
    assert facts.brand == "Lumora Care"


def test_a_byline_that_is_no_brand_leaves_the_brand_empty() -> None:
    facts, _ = parse_page(_amazon("Klik hier om de Airmax winkel te bezoeken"), "https://www.amazon.nl/dp/B0FAKE0001")
    assert facts.brand == ""


async def test_a_client_that_sends_the_byline_phrase_gets_the_brand() -> None:
    supplied = PageFacts(brand="De Air Max Store openen", seller="Airmax nasal dilator", platform="amazon")
    out = await nodes.preprocess_node({"page_content": "", "url": "", "page_facts": supplied})  # type: ignore[typeddict-item]
    assert out["facts"].brand == "Air Max"


@pytest.mark.parametrize(
    ("brand", "seller"),
    [
        ("Airmax", "Airmax nasal dilator"),
        ("Airmax", "Airmax Official"),
        ("Airmax", "AIRMAX-Store BV"),
        ("Airmax", "Air Max Benelux"),
        ("Air Max", "Airmax nasal dilator"),
        ("Anker", "AnkerDirect"),
        ("Crème Lumière", "creme-lumiere EU"),
        ("Nike Sportswear", "Nike"),
    ],
)
def test_the_brand_as_whole_words_in_the_seller_is_the_same_company(brand: str, seller: str) -> None:
    assert brands.same_company(brand, seller) is True
    assert brands.lookalike(brand, seller) is None


@pytest.mark.parametrize(
    "seller", ["Nikee", "Nlke", "N1ke", "Nike-e", "Nkie", "Nіke", "Nikee Official Store", "N1ke-Direct"]
)  # the last with a Cyrillic і
def test_a_lookalike_seller_is_not_the_brand(seller: str) -> None:
    assert brands.same_company("Nike", seller) is False
    assert brands.lookalike("Nike", seller) is not None


@pytest.mark.parametrize("seller", ["ShopXYZ", "Mike's Shop", "Amazon", "Nice Shoes", "Fleet Nikee Running"])
def test_a_plainly_different_seller_is_no_lookalike(seller: str) -> None:
    assert brands.same_company("Nike", seller) is False
    assert brands.lookalike("Nike", seller) is None


def test_a_changed_letter_in_a_short_name_is_another_word() -> None:
    assert brands.lookalike("Bose", "Boss") is None
    assert brands.lookalike("Camper", "Casper") is None
    assert brands.lookalike("Lumoracare", "Lumoracore") == "lumoracore"  # longer names: two edits of any kind


def _listing_state(brand: str, seller: str) -> Any:
    facts = PageFacts(brand=brand, seller=seller, platform="amazon")
    return {"facts": facts, "page_type": "aggregate_listing", "url": "https://www.amazon.nl/dp/B0FAKE0001"}


def _verdict(features: dict[str, float], findings: list[Finding], facts: PageFacts) -> shopper.Verdict:
    return shopper.build(40, features=features, findings=findings, facts=facts, page_type="aggregate_listing")


async def _reasons(brand: str, seller: str, extra: dict[str, float]) -> list[str]:
    state = _listing_state(brand, seller)
    out = await ListingSkill().run(state)
    verdict = _verdict(out["features"] | extra, out["findings"], state["facts"])
    return [r.type for r in verdict.reasons]


KNOWN = {"brand_presence.wikidata": 1.0}


async def test_sold_by_the_brand_under_a_longer_name_is_not_another_company() -> None:
    assert "seller_not_brand" not in await _reasons("Air Max", "Airmax nasal dilator", {})
    assert "seller_not_brand" not in await _reasons("Airmax", "AIRMAX-Store BV", KNOWN)


async def test_a_lookalike_seller_of_a_known_brand_is_said() -> None:
    reasons = await _reasons("Nike", "Nikee", KNOWN)
    assert "seller_lookalike" in reasons and "seller_not_brand" not in reasons
    out = await ListingSkill().run(_listing_state("Nike", "Nikee"))
    verdict = _verdict(out["features"] | KNOWN, out["findings"], PageFacts(brand="Nike", seller="Nikee"))
    reason = next(r for r in verdict.reasons if r.type == "seller_lookalike")
    assert reason.detail == "The seller's name looks like the brand's but isn't: “Nikee” sells this “Nike” product."
    assert "scam" not in (reason.title + reason.detail).lower()
    # Not for a brand nobody outside the page knows: then it's just another seller.
    assert await _reasons("Nike", "Nikee", {}) == ["seller_not_brand"]


async def test_a_plainly_different_seller_stays_another_company() -> None:
    assert await _reasons("Nike", "ShopXYZ", KNOWN) == ["seller_not_brand"]


def test_known_brand_and_no_presence_are_never_both_said() -> None:
    facts = PageFacts(brand="Air Max", seller="Airmax nasal dilator", platform="amazon")
    # The content read knows the brand, the lookups found it nowhere: neither claim.
    disagree = {"content.brand_recognized": 1.0, "brand_presence.no_presence": 1.0, "content.generic_product": 0.8}
    verdict = _verdict(disagree, [], facts)
    assert "brand_no_presence" not in [r.type for r in verdict.reasons]
    assert "known_brand" not in [p.type for p in verdict.positives]
    # Each alone is still said.
    alone = _verdict({"brand_presence.no_presence": 1.0, "content.generic_product": 0.8}, [], facts)
    assert "brand_no_presence" in [r.type for r in alone.reasons]
    assert "known_brand" in [p.type for p in _verdict({"content.brand_recognized": 1.0}, [], facts).positives]
