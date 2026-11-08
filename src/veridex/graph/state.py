from typing import Annotated, Literal, NotRequired

from typing_extensions import TypedDict

from veridex.facts import PageFacts

PageType = Literal["aggregate_listing", "storefront", "article", "non_product"]


def _append_results(left: list[str] | None, right: list[str]) -> list[str]:
    """Reducer: concatenate skill result lists, handling uninitialised state."""
    return (left or []) + right


class AnalysisState(TypedDict):
    """Shared state threaded through the analysis graph."""

    # ── Inputs (provided by the API route) ──────────────────────────────────
    page_content: str
    url: str
    flags: list[str]
    # Facts extracted by the client from the rendered page; the server extracts them from HTML otherwise.
    page_facts: NotRequired[PageFacts | None]

    # ── Pipeline (set by nodes) ──────────────────────────────────────────────
    facts: PageFacts
    cleaned_content: str
    skill_results: Annotated[list[str], _append_results]

    # ── Classification (set by classify_node) ───────────────────────────────
    # One of: "aggregate_listing", "storefront", "article", "non_product"
    page_type: PageType

    # ── Outputs (set by judge node) ──────────────────────────────────────────
    score: int
    explanation: str
