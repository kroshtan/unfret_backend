"""The quick tier reads only the page, by rules: no LLM, no embeddings, no image index, no price history."""

from pathlib import Path
from typing import Any

import pytest

from unfret import image_index, llm, price_store, scoring
from unfret.config import settings
from unfret.graph import SKILLS, build_graph, nodes, triage
from unfret.graph.skills import content, reviews
from unfret.x402.verdict import skills_for, tier_description, to_verdict

# Captured at import, before the suite's autouse fixture hides the shipped models.
_LOAD_MODEL = scoring.load_model

PAGE = (Path(__file__).parent.parent / "fixtures" / "pages" / "shopify-productgroup-us.html").read_text()
URL = "https://www.allbirds.com/products/mens-tree-runners"


def _forbid(*_: Any, **__: Any) -> Any:
    pytest.fail("the quick tier must not call the LLM, embeddings, the image index or the price store")


@pytest.fixture
def no_paid_work(monkeypatch: pytest.MonkeyPatch) -> None:
    quick = _LOAD_MODEL.__wrapped__(scoring.MODEL_QUICK_PATH)
    assert quick is not None, "the quick model ships in the repo"
    monkeypatch.setattr(scoring, "load_model", lambda path=scoring.MODEL_PATH: quick)
    for module in (nodes, content, triage, llm):
        monkeypatch.setattr(module, "get_structured_llm", _forbid)
    monkeypatch.setattr(reviews, "get_embeddings", _forbid)
    monkeypatch.setattr(llm, "get_embeddings", _forbid)
    monkeypatch.setattr(image_index, "embedder", _forbid)
    monkeypatch.setattr(price_store, "observe", _forbid)
    monkeypatch.setattr(settings, "fingerprints", False)


async def _quick(html: str) -> dict[str, Any]:
    graph = build_graph(skills_for("quick", SKILLS), model_path=scoring.MODEL_QUICK_PATH, llm=False)
    return await graph.ainvoke({"page_content": html, "url": URL, "flags": [], "explain": True})


@pytest.mark.usefixtures("no_paid_work")
async def test_quick_scores_a_product_page_without_any_paid_work() -> None:
    result = await _quick(PAGE)
    assert result["page_type"] == "storefront"
    assert result["assessment"]["model_version"].startswith("v1-quick-")
    ran = {r.split("\n", 1)[0].strip("[]") for r in result["skill_results"]}
    assert not ran & {"content", "reviews", "reverse_image", "domain_age"}
    verdict = to_verdict(result, policy="balanced", tier="quick")
    assert verdict.assessed and verdict.tier == "quick"


@pytest.mark.usefixtures("no_paid_work")
async def test_quick_does_not_ask_an_llm_what_a_page_without_a_price_is() -> None:
    result = await _quick("<html><body><h1>Our story</h1><p>" + "We make shoes. " * 60 + "</p></body></html>")
    assert (result["page_type"], result["score"]) == ("non_product", -1)  # not assessed: the caller is refunded


def test_the_quick_model_reads_only_what_the_quick_tier_computes() -> None:
    quick = _LOAD_MODEL.__wrapped__(scoring.MODEL_QUICK_PATH)
    assert quick is not None, "the quick model ships in the repo"
    computed = {f"{s.name}.{f}" for s in skills_for("quick", SKILLS) for f in s.features}
    assert set(quick.features) <= computed


def test_tiers_say_what_they_include() -> None:
    assert "No AI read" in tier_description("quick")
    assert "AI read" in tier_description("deep") and "same check a shopper gets" in tier_description("deep")
