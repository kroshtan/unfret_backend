"""
A well-known US brand's own product page must not be called risky for lacking an EU imprint.

Regression for a paid check of allbirds.com (2026-11-09) that came back "careful" / ``warn`` with "No company
details" as its only reason: the Shopify ProductGroup JSON-LD wasn't read (so the content read was told the brand
was unknown), the EU imprint and VAT ID counted against a US shop, and an absence alone was enough for "careful".
The fixture is a hand-made page with the same structure (see tests/fixtures/pages/README.md).
"""

from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from unfret import scoring, shopper
from unfret.config import settings
from unfret.facts import PageFacts, parse_page
from unfret.graph import build_graph, nodes
from unfret.graph.skills import content
from unfret.graph.skills.content import ContentSkill
from unfret.graph.skills.dark_patterns import DarkPatternsSkill
from unfret.graph.skills.eu_registry import EuRegistrySkill
from unfret.graph.skills.html_source_signals import HtmlSourceSignalsSkill
from unfret.graph.skills.injection import InjectionSkill
from unfret.graph.skills.listing import ListingSkill
from unfret.graph.skills.shop_policies import ShopPoliciesSkill
from unfret.graph.skills.us_identity import UsIdentitySkill
from unfret.market import shop_market
from unfret.x402.verdict import to_verdict

# Captured at import, before the suite's autouse fixture hides the shipped model.
_LOAD_MODEL = scoring.load_model

URL = "https://www.allbirds.com/products/mens-tree-runners"
PAGE = (Path(__file__).parent.parent / "fixtures" / "pages" / "shopify-productgroup-us.html").read_text()
US = PageFacts(currency="USD", price=20.0)


def test_shopify_product_group_json_ld_is_read() -> None:
    facts, text = parse_page(PAGE, URL)
    assert facts.platform == "shopify"
    assert (facts.title, facts.brand) == ("Men's Tree Runner", "Allbirds")
    assert (facts.price, facts.currency) == (70.0, "USD")
    assert (facts.rating, facts.rating_count) == (4.6, 9951)  # from a separate AggregateRating block
    assert facts.json_ld[0]["@type"] == "ProductGroup"
    assert shop_market(facts, URL, text) == "us"
    assert nodes.strong_product_signals(facts, {"checkout_content": ""})  # type: ignore[typeddict-item]


def test_product_group_offers_come_from_its_variants() -> None:
    ld = (
        '{"@type": "ProductGroup", "name": "Tee", "brand": "Acme", "hasVariant": ['
        '{"@type": "Product", "name": "Tee S"},'
        '{"@type": "Product", "offers": {"@type": "Offer", "price": "25.00", "priceCurrency": "USD"}}]}'
    )
    facts, _ = parse_page(f'<script type="application/ld+json">{ld}</script><h1>Tee</h1>')
    assert (facts.brand, facts.price, facts.currency) == ("Acme", 25.0, "USD")


def test_a_shop_rating_is_not_a_product_rating() -> None:
    ld = '{"@type": "Organization", "aggregateRating": {"ratingValue": "4.9", "reviewCount": "120"}}'
    facts, _ = parse_page(f'<script type="application/ld+json">{ld}</script><h1>Shop</h1>')
    assert facts.rating is None


async def test_us_shop_without_imprint_is_not_marked_down() -> None:
    bare = await EuRegistrySkill(vies=False).run(_state(US, "Great shoes."))
    f = bare["features"]
    assert "eu_registry.imprint" not in f and f["eu_registry.imprint.not_applicable"] == 1.0
    assert f["eu_registry.vat_present.not_applicable"] == 1.0
    assert not any(x.type == "imprint" for x in bare["findings"])
    eu = await EuRegistrySkill(vies=False).run(_state(PageFacts(currency="EUR", price=20.0), "Great shoes."))
    assert eu["features"]["eu_registry.imprint"] == 0.0  # an EU shop still needs one


def test_scorer_reads_a_single_not_applicable_feature_as_neutral() -> None:
    model = scoring.Model(
        version="t",
        features=["eu_registry.imprint", "eu_registry.withdrawal_right"],
        means=[0.5, 0.5],
        scales=[0.5, 0.5],
        coefficients=[-1.0, -1.0],
        intercept=0.0,
        calibration=[],
    )
    contributions = model.contributions({"eu_registry.imprint.not_applicable": 1.0})
    assert contributions[0] == 0.0  # imprint: not applicable
    assert contributions[1] > 0  # the rest of the skill still counts (no return policy found)
    assessment = scoring.score(
        {"eu_registry.imprint.not_applicable": 1.0, "eu_registry.withdrawal_right": 0.0}, model=model
    )
    assert assessment is not None
    assert assessment.confidence == pytest.approx(abs(2 * assessment.risk_score - 1), abs=1e-3)  # fully measured


@pytest.mark.parametrize("market", ["us", "eu"])
def test_absences_alone_never_make_a_page_careful_when_the_scorer_is_unsure(market: str) -> None:
    features = {"eu_registry.imprint": 0.0, "eu_registry.withdrawal_right": 1.0}

    def band(confidence: float | None, extra: dict[str, float] | None = None, *, concrete_only: bool = False) -> str:
        return shopper.build(
            39,
            features=features | (extra or {}),
            findings=[],
            facts=US,
            page_type="storefront",
            market=market,
            confidence=confidence,
            concrete_only=concrete_only,
        ).band

    assert band(0.19) == "concerns"
    assert band(0.5) == "careful"  # a sure scorer still warns
    assert band(None) == "careful"  # the LLM judge's score: no confidence to go by
    assert band(0.19, {"dark_patterns.preticked_addons": 1.0}) == "careful"  # something concrete to show
    # The quick tier warns only on something concrete, however sure its scorer is.
    assert band(0.9, concrete_only=True) == "concerns"
    assert band(0.9, {"dark_patterns.preticked_addons": 1.0}, concrete_only=True) == "careful"


async def test_the_quick_judge_warns_only_on_something_concrete(monkeypatch: pytest.MonkeyPatch) -> None:
    sure = scoring.Model(
        version="t",
        features=["eu_registry.imprint"],
        means=[0.5],
        scales=[0.5],
        coefficients=[-5.0],
        intercept=0.0,
        calibration=[],
    )
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: sure)
    state: Any = {**_state(PageFacts(currency="EUR", price=20.0), "Shoes."), "features": {"eu_registry.imprint": 0.0}}
    state |= {"findings": [], "skill_results": [], "explain": False}

    quick = await nodes.make_judge_node(scoring.MODEL_QUICK_PATH, llm=False)(state)
    deep = await nodes.make_judge_node(scoring.MODEL_PATH, llm=False)(state)

    assert quick["assessment"]["confidence"] > 0.5
    assert quick["verdict"]["band"] == "concerns"
    assert deep["verdict"]["band"] in {"careful", "junk_or_scam"}


# ── The whole graph on the fetched page, with the shipped scorer ─────────────


class _Signals(BaseModel):
    """What the content read answered."""

    generic_brand: float
    generic_product: float
    missing_trust_info: float
    brand_recognized: bool


RECOGNISED = _Signals(generic_brand=0.0, generic_product=0.1, missing_trust_info=0.5, brand_recognized=True)
# What the content read answered when it was told the brand was unknown: the worst case.
MISREAD = _Signals(generic_brand=1.0, generic_product=0.5, missing_trust_info=0.8, brand_recognized=False)


async def _check(monkeypatch: pytest.MonkeyPatch, signals: _Signals) -> tuple[dict[str, Any], list[Any]]:
    model = _LOAD_MODEL.__wrapped__(scoring.MODEL_PATH)
    if model is None:
        pytest.skip("no model shipped")
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: model)
    monkeypatch.setattr(settings, "fingerprints", False)
    monkeypatch.setattr(nodes, "get_structured_llm", lambda schema: pytest.fail("no other LLM call"))
    prompts: list[Any] = []

    def read(messages: Any) -> Any:
        prompts.append(messages)
        return content._Signals(
            scam_copy=0.0, implausible_claims=0.0, addresses_the_reader_ai=False, **signals.model_dump()
        )

    monkeypatch.setattr(content, "get_structured_llm", lambda schema: RunnableLambda(read))
    skills = [
        ContentSkill(),
        ListingSkill(),
        HtmlSourceSignalsSkill(),
        DarkPatternsSkill(),
        InjectionSkill(),
        EuRegistrySkill(vies=False),
        UsIdentitySkill(),
        ShopPoliciesSkill(),
    ]
    result = await build_graph(skills).ainvoke({"page_content": PAGE, "url": URL, "flags": [], "explain": False})
    return result, prompts


async def test_allbirds_is_a_storefront_product_page_and_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    result, prompts = await _check(monkeypatch, RECOGNISED)
    assert result["page_type"] == "storefront"  # a product on the brand's own shop (classified without an LLM)
    assert "Brand: Allbirds" in prompts[0][1].content  # the content read is told whose shoe it is
    verdict = to_verdict(result, policy="balanced", tier="quick")
    assert verdict.action == "allow"
    assert verdict.band in {"reliable", "concerns"}
    assert "no_imprint" not in {f.type for f in verdict.flags}


async def test_even_a_misread_page_is_not_careful_on_absences_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = await _check(monkeypatch, MISREAD)
    verdict = to_verdict(result, policy="balanced", tier="quick")
    assert verdict.band == "concerns"
    assert "no_imprint" not in {f.type for f in verdict.flags}


def _state(facts: PageFacts, text: str) -> Any:
    return {
        "page_content": "",
        "url": URL,
        "flags": [],
        "facts": facts,
        "cleaned_content": text,
        "page_type": "storefront",
    }
