from typing import Any

from pydantic import BaseModel, Field

from veridex import __version__


class Healthy(BaseModel):
    """Response schema for the health check endpoint."""

    status: str = Field(default="healthy", description="The status of the health check")
    version: str = Field(description="The version of the API", default=__version__)
    x402: str | None = Field(default=None, description="Pay-per-check status: ok, off, low_balance, …")
    hot_sol_lamports: int | None = Field(default=None, description="The x402 hot wallet's SOL (fee reserve)")
    hot_wsol_lamports: int | None = Field(default=None, description="Received payments not yet swept to cold")
    x402_alerts: list[str] = Field(default_factory=list, description="sweep_failing, fee_reserve_low, …")
    credits: str | None = Field(default=None, description="Credits door: off, sign_in, ok (purchases on)")


class AnalyzeResponse(BaseModel):
    """Response body for the analyze endpoint."""

    score: int = Field(ge=-1, le=100, description="Reliability score: 0 (scam) → 100 (legitimate); -1 = not analysed.")
    explanation: str = Field(description="One-line explanation of the score.")
    assessment: dict[str, Any] | None = Field(
        default=None, description="The trained scorer's risk_score, confidence, weighted flags and model_version."
    )
    balance_lamports: int | None = Field(default=None, description="The wallet's balance after this analysis.")
