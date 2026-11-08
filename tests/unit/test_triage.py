"""The triage: rules for clear cases, the LLM for unclear ones, a budget, and one re-triage."""

from typing import Any, ClassVar

import pytest
from langchain_core.runnables import RunnableLambda
from pydantic import SecretStr

from veridex.config import settings
from veridex.facts import PageFacts
from veridex.graph import build_graph, nodes, triage
from veridex.graph.skills import Skill, SkillOutput
from veridex.graph.state import AnalysisState

STOREFRONT = (
    "<html><body><h1>Glow Lamp</h1><img src='https://shop.test/1.jpg'><img src='https://shop.test/2.jpg'>"
    "</body></html>"
)


class _Cheap(Skill):
    name = "cheap"
    description = "reports canned features"

    def __init__(self, features: dict[str, float]) -> None:
        self.canned = features

    async def run(self, state: AnalysisState) -> SkillOutput:
        # Report the canned features under their real (other skills') names, as if those skills had run.
        return {"features": self.canned, "findings": [], "skill_results": ["[cheap]\nok"]}


class _Photo(Skill):
    name = "reverse_image"
    description = "records its runs"
    tier = "expensive"
    always_run = False
    runs: ClassVar[list[list[str]]] = []

    def __init__(self, supplier_hits: float = 0.0) -> None:
        self.supplier_hits = supplier_hits

    async def run(self, state: AnalysisState) -> SkillOutput:
        type(self).runs.append(list(state.get("expensive_runs", [])))
        return self.output({"searched": True, "supplier_hits": self.supplier_hits})


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    _Photo.runs = []
    monkeypatch.setattr(settings, "tineye_api_key", SecretStr("key"))
    prompts: list[Any] = []

    def structured(schema: type) -> RunnableLambda:
        def answer(messages: Any) -> Any:
            prompts.append((schema, messages))
            if schema is nodes._ClassifyOutput:
                return nodes._ClassifyOutput(page_type="storefront", platform="", reasoning="r")
            if schema is nodes._JudgeOutput:
                return nodes._JudgeOutput(score=40, explanation="x")
            return triage._TriageAnswer(run_reverse_image=True, reason="Generic lamp.")

        return RunnableLambda(answer)

    monkeypatch.setattr(nodes, "get_structured_llm", structured)
    monkeypatch.setattr(triage, "get_structured_llm", structured)
    return prompts


async def run(features: dict[str, float], photo: _Photo | None = None, flags: list[str] | None = None) -> Any:
    graph = build_graph([_Cheap(features), photo or _Photo()])
    return await graph.ainvoke({"page_content": STOREFRONT, "url": "", "flags": flags or []})


SUSPICIOUS = {"cheap.x": 0.0, "content.generic_product": 1.0, "listing.generated_brand": 0.8, "content.scam_copy": 0.6}
CLEAN = {"content.brand_recognized": 1.0, "content.generic_product": 0.1}
MIDDLE = {"content.generic_product": 0.5, "content.scam_copy": 0.3, "listing.generated_brand": 0.2}


def test_suspicion_and_rule() -> None:
    assert triage.reverse_image_rule(SUSPICIOUS) == "run"
    assert triage.reverse_image_rule(CLEAN) == "skip"
    assert triage.reverse_image_rule(MIDDLE) == "unclear"


async def test_clear_suspicion_runs_the_photo_check(setup: list[Any]) -> None:
    result = await run(SUSPICIOUS, _Photo(supplier_hits=1.0))

    assert _Photo.runs == [["reverse_image"]]
    assert result["features"]["reverse_image.searched"] == 1.0
    assert not any(schema is triage._TriageAnswer for schema, _ in setup)  # no LLM needed


async def test_clean_page_skips_it() -> None:
    await run(CLEAN)
    assert _Photo.runs == []


async def test_unclear_case_asks_the_llm(setup: list[Any]) -> None:
    await run(MIDDLE)

    assert _Photo.runs == [["reverse_image"]]
    assert any(schema is triage._TriageAnswer for schema, _ in setup)


async def test_retriage_tries_the_second_photo_once() -> None:
    very = {**SUSPICIOUS, "content.scam_copy": 1.0, "reviews.off_topic_share": 0.6}
    await run(very, _Photo(supplier_hits=0.0))

    assert _Photo.runs == [["reverse_image"], ["reverse_image", "reverse_image"]]


async def test_no_retriage_after_a_hit() -> None:
    await run({**SUSPICIOUS, "content.scam_copy": 1.0}, _Photo(supplier_hits=2.0))
    assert len(_Photo.runs) == 1


async def test_budget_caps_expensive_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "triage_max_expensive_runs", 1)
    await run({**SUSPICIOUS, "content.scam_copy": 1.0, "reviews.off_topic_share": 0.6}, _Photo(supplier_hits=0.0))
    assert len(_Photo.runs) == 1


async def test_flag_forces_the_check_even_on_a_clean_page() -> None:
    await run(CLEAN, flags=["reverse_image"])
    assert _Photo.runs == [["reverse_image"]]


async def test_nothing_runs_without_tineye(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tineye_api_key", SecretStr(""))
    await run(SUSPICIOUS)
    assert _Photo.runs == []


async def test_no_photos_no_check() -> None:
    state: Any = {"facts": PageFacts(), "features": SUSPICIOUS, "page_type": "storefront", "findings": []}
    assert (await triage.plan(state, {"reverse_image"})).run == ()
