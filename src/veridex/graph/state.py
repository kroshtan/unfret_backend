from typing import Annotated, Literal, NotRequired

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from veridex.facts import PageFacts

PageType = Literal["aggregate_listing", "storefront", "article", "non_product"]


class Finding(BaseModel):
    """A factual observation by a skill: what was seen and the evidence for it, never a verdict."""

    skill: str
    type: str
    evidence: str = Field(max_length=500)


def _append_results(left: list[str] | None, right: list[str]) -> list[str]:
    """Reducer: concatenate skill result lists, handling uninitialised state."""
    return (left or []) + right


def _append_findings(left: list[Finding] | None, right: list[Finding]) -> list[Finding]:
    """Reducer: concatenate findings from parallel skills."""
    return (left or []) + right


def _merge_features(left: dict[str, float] | None, right: dict[str, float]) -> dict[str, float]:
    """Reducer: merge feature dicts from parallel skills (names are prefixed per skill, so they don't clash)."""
    return {**(left or {}), **right}


class AnalysisState(TypedDict):
    """Shared state threaded through the analysis graph."""

    # ── Inputs (provided by the API route) ──────────────────────────────────
    page_content: str
    url: str
    flags: list[str]
    # Facts extracted by the client from the rendered page; the server extracts them from HTML otherwise.
    page_facts: NotRequired[PageFacts | None]
    # Optional checkout page HTML, to compare the final total with the listed price (drip pricing).
    checkout_content: NotRequired[str]

    # ── Pipeline (set by nodes) ──────────────────────────────────────────────
    facts: PageFacts
    cleaned_content: str
    skill_results: Annotated[list[str], _append_results]
    features: Annotated[dict[str, float], _merge_features]
    findings: Annotated[list[Finding], _append_findings]

    # ── Classification (set by classify_node) ───────────────────────────────
    # One of: "aggregate_listing", "storefront", "article", "non_product"
    page_type: PageType

    # ── Outputs (set by judge node) ──────────────────────────────────────────
    score: int
    explanation: str
