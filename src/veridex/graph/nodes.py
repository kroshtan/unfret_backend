from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from veridex.graph.state import AnalysisState, PageType
from veridex.html_cleaner import clean_html
from veridex.llm import get_structured_llm


def preprocess_node(state: AnalysisState) -> dict:
    """
    Clean raw HTML and initialise the skill_results accumulator.

    :param state: Current graph state; uses ``page_content``.
    :return: ``cleaned_content`` and an empty ``skill_results`` list.
    """
    return {
        "cleaned_content": clean_html(state["page_content"]),
        "skill_results": [],
    }


# ── Classification node ───────────────────────────────────────────────────────


class _ClassifyOutput(BaseModel):
    page_type: Literal["aggregate_listing", "storefront", "article", "non_product"] = Field(
        description=(
            "aggregate_listing: a product listed on a marketplace (Amazon, Etsy, eBay, …); "
            "storefront: a brand's own shop; "
            "article: news/blog/review describing a product; "
            "non_product: page is not about a product at all."
        )
    )
    platform: str = Field(
        description='Marketplace name when page_type is aggregate_listing (e.g. "Amazon"), otherwise empty string.'
    )
    reasoning: str = Field(description="One sentence explaining the classification.")


_CLASSIFY_SYSTEM = """\
You are a web-page classifier. Given the text of a page, determine:
1. Whether it is about a product or service.
2. If so, what kind of page it is:
   - aggregate_listing: a product listed on a third-party marketplace (Amazon, Etsy, eBay, Walmart, \
bol.com, Zalando, Cdiscount, etc.)
   - storefront: a brand's own shop or website selling directly
   - article: a news article, blog post, or review that discusses a product or service
   - non_product: the page is not about a product at all

Note: bol.com is a Dutch marketplace — always classify it as aggregate_listing, never as storefront.

Return the most specific match. When the platform is a known marketplace, include its name.\
"""


async def classify_node(state: AnalysisState) -> dict:
    """
    Classify the page type before skills run.

    Determines whether the page is a marketplace listing, a storefront,
    an article about a product, or unrelated to products entirely.

    :param state: Current graph state; uses ``cleaned_content``.
    :return: ``page_type`` and a classification entry in ``skill_results``.
    """
    if not state["cleaned_content"]:
        return {"page_type": "non_product", "skill_results": ["[classify]\nPage type: non_product\nPage has no text."]}

    output: _ClassifyOutput = await get_structured_llm(_ClassifyOutput).ainvoke(
        [
            SystemMessage(content=_CLASSIFY_SYSTEM),
            HumanMessage(content=state["cleaned_content"][:8_000]),
        ]
    )

    page_type: PageType = output.page_type

    label: str = output.page_type
    if output.platform:
        label = f"{output.page_type} ({output.platform})"

    finding = f"[classify]\nPage type: {label}\n{output.reasoning}"
    return {"page_type": page_type, "skill_results": [finding]}


_EARLY_EXIT_EXPLANATIONS: dict[PageType, str] = {
    "article": "Articles are not supported yet",
    "non_product": "No product referenced on this page",
}


def early_exit_node(state: AnalysisState) -> dict:
    """
    Return a sentinel score for pages that cannot be analysed yet.

    Called for articles and pages that are not about a product; marketplace
    listings and storefronts go through the skills instead.

    :param state: Current graph state; uses ``page_type``.
    :return: ``score`` of ``-1`` and a short ``explanation``.
    """
    explanation = _EARLY_EXIT_EXPLANATIONS.get(state["page_type"], "Page type is not supported")
    return {"score": -1, "explanation": explanation}


class _JudgeOutput(BaseModel):
    score: int = Field(ge=0, le=100, description="0 = scam, 100 = fully legitimate")
    explanation: str = Field(description="One sentence explaining the score")


_JUDGE_SYSTEM = """\
You are a shopping reliability judge. Based on the skill findings below, output:
- score: 0 (definite scam / dropship junk) → 100 (fully legitimate retailer)
- explanation: one sentence (≤ 20 words) explaining the verdict\
"""


async def judge_node(state: AnalysisState) -> dict:
    """
    Synthesise all skill findings into a final score and explanation.

    Skills run in parallel and their findings arrive in completion order, so they are sorted (classification
    first, then by skill name) to give the judge the same prompt for the same evidence.

    :param state: Current graph state; uses ``skill_results``.
    :return: ``score`` and ``explanation``.
    """
    findings = "\n\n".join(sorted(state["skill_results"], key=lambda r: (not r.startswith("[classify]"), r)))
    output: _JudgeOutput = await get_structured_llm(_JudgeOutput).ainvoke(
        [
            SystemMessage(content=_JUDGE_SYSTEM),
            HumanMessage(content=f"Skill findings:\n{findings}"),
        ]
    )
    return {"score": output.score, "explanation": output.explanation}
