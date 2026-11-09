from typing import Any

from pydantic import BaseModel, Field

from veridex import __version__
from veridex.shopper import Verdict


class Healthy(BaseModel):
    """Response schema for the health check endpoint."""

    status: str = Field(default="healthy", description="The status of the health check")
    version: str = Field(description="The version of the API", default=__version__)
    x402: str | None = Field(default=None, description="Pay-per-check status: ok, off, low_balance, …")
    hot_sol_lamports: int | None = Field(default=None, description="The x402 hot wallet's SOL (fee reserve)")
    hot_wsol_lamports: int | None = Field(default=None, description="Received payments not yet swept to cold")
    hot_usdc_units: int | None = Field(default=None, description="Received USDC not yet swapped to SOL")
    x402_alerts: list[str] = Field(
        default_factory=list, description="sweep_failing, fee_reserve_low, … (with the operator token only)"
    )
    x402_alert_count: int = Field(default=0, description="How many x402 alerts are raised")
    credits: str | None = Field(default=None, description="Credits door: off, sign_in, ok (purchases on)")


class AnalyzeResponse(BaseModel):
    """Response body for the analyze endpoint."""

    score: int = Field(ge=-1, le=100, description="Reliability score: 0 (scam) → 100 (legitimate); -1 = not analysed.")
    explanation: str = Field(description="One-line explanation of the score.")
    assessment: dict[str, Any] | None = Field(
        default=None, description="The trained scorer's risk_score, confidence, weighted flags and model_version."
    )
    balance_usd_micros: int | None = Field(
        default=None,
        description="The account's balance after this analysis, in micro-dollars (1 USD = 1,000,000).",
    )
    balance_lamports: int | None = Field(
        default=None,
        deprecated=True,
        description="Deprecated alias of balance_usd_micros (same value; the name predates dollar prices). Kept for "
        "extension versions up to 0.5.0.",
    )
    charged: bool | None = Field(
        default=None,
        description="Whether this analysis used a check: false for pages that couldn't be analysed (score -1) and "
        "for results served from the cache.",
    )
    report_token: str | None = Field(
        default=None,
        description="Pass to POST /v1/reports to report this result as wrong (within 7 days, by the same account). "
        "Absent for pages that couldn't be analysed.",
    )
    verdict: Verdict | None = Field(
        default=None,
        description="For shoppers: the band in words, a headline and up to three plain reasons, in the language of "
        "the Accept-Language header (en, nl).",
    )
