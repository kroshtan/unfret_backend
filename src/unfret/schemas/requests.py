from typing import Self

from pydantic import BaseModel, Field, model_validator

from unfret.config import settings
from unfret.facts import PageFacts


class AnalyzeRequest(BaseModel):
    """
    Request body for the analyze endpoint.

    Usually the page itself (``page_content`` and/or ``page_facts``). A request with only a ``url`` has the server
    fetch the page: one it can't read (an error, a bot check, a page that needs a browser) is refused before any
    charge.
    """

    page_content: str | None = Field(
        default=None,
        min_length=1,
        max_length=settings.max_page_bytes,
        description="Raw HTML, or the page's cleaned main text when page_facts is supplied.",
    )
    url: str = Field(
        default="",
        description="URL of the product page. Sent alone (no page_content or page_facts), the server fetches it.",
    )
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
        if self.page_content is None and self.page_facts is None and not self.url.strip():
            raise ValueError("Send page_content, page_facts or both, or a url to fetch.")
        return self

    @property
    def url_only(self) -> bool:
        """
        Whether the server has to fetch the page itself.

        :return: ``True`` when neither the page's content nor its facts were sent.
        """
        return self.page_content is None and self.page_facts is None
