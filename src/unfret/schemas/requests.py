from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from unfret.config import settings
from unfret.facts import PageFacts


class ProofOfWork(BaseModel):
    """A solved challenge from ``GET /v1/analyze/challenge``: what a free deep check costs the client."""

    challenge: str = Field(max_length=200, description="The challenge, exactly as issued.")
    solution: str = Field(
        min_length=1,
        max_length=32,
        description="A string (e.g. a decimal counter) with SHA-256(challenge + ':' + solution) starting with the "
        "challenge's bits zero bits.",
    )


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
    tier: Literal["deep", "quick"] = Field(
        default="deep",
        description="deep (the default): the full analysis, which uses a check. quick: rule-based checks of the page "
        "itself only (no LLM, no lookups elsewhere; product pages), free for everyone and never charged.",
    )

    proof_of_work: ProofOfWork | None = Field(
        default=None,
        description="A free deep check (no pass, or a pass without bought checks): a solved challenge from GET "
        "/v1/analyze/challenge, used once. Not needed for a quick check or a deep check a pass pays for.",
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
