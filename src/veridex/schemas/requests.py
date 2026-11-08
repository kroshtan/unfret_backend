from typing import Self

from pydantic import BaseModel, Field, model_validator

from veridex.config import settings
from veridex.facts import PageFacts


class AnalyzeRequest(BaseModel):
    """Request body for the analyze endpoint."""

    page_content: str | None = Field(
        default=None,
        min_length=1,
        max_length=settings.max_page_bytes,
        description="Raw HTML, or the page's cleaned main text when page_facts is supplied.",
    )
    url: str = Field(default="", description="URL of the product page.")
    flags: list[str] = Field(
        default_factory=list, description="Names of optional skills to activate in addition to the default set."
    )
    page_facts: PageFacts | None = Field(
        default=None,
        description="Facts extracted from the rendered page (e.g. by the browser extension). When absent, the "
        "server extracts them from page_content.",
    )
    checkout_content: str | None = Field(
        default=None,
        max_length=settings.max_page_bytes,
        description="HTML of the checkout page for this product, if available, to detect fees added at checkout.",
    )

    @model_validator(mode="after")
    def _has_a_page(self) -> Self:
        if self.page_content is None and self.page_facts is None:
            raise ValueError("Send page_content, page_facts or both.")
        return self
