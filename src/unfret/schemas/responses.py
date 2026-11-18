from typing import Any

from pydantic import BaseModel, Field

from unfret import __version__
from unfret.shopper import Verdict


class Healthy(BaseModel):
    """Response schema for the health check endpoint."""

    status: str = Field(default="healthy", description="The status of the health check")
    version: str = Field(description="The version of the API", default=__version__)
    commit: str | None = Field(default=None, description="The git commit the running image was built from")
    model_version: str | None = Field(
        default=None, description="The scorer analyses and deep checks run (the open baseline or a retrained one)"
    )
    quick_model_version: str | None = Field(default=None, description="The scorer the x402 quick tier runs")
    x402: str | None = Field(default=None, description="Pay-per-check status: ok, off, low_balance, …")
    hot_sol_lamports: int | None = Field(default=None, description="The x402 hot wallet's SOL (fee reserve)")
    hot_wsol_lamports: int | None = Field(default=None, description="Received payments not yet swept to cold")
    hot_usdc_units: int | None = Field(default=None, description="Received USDC not yet swept to the cold wallet")
    x402_alerts: list[str] = Field(
        default_factory=list, description="sweep_failing, fee_reserve_low, … (with the operator token only)"
    )
    x402_alert_count: int = Field(default=0, description="How many x402 alerts are raised")
    x402_base: str | None = Field(default=None, description="Pay-per-check on Base: ok, off, mainnet_refused, …")
    x402_base_alert_count: int = Field(default=0, description="How many x402-on-Base alerts are raised")
    x402_base_alerts: list[str] = Field(
        default_factory=list, description="fee_reserve_low, outflow_to_non_cold, … (with the operator token only)"
    )
    base_hot_usdc_units: int | None = Field(
        default=None, description="USDC on the Base hot wallet, not yet swept to cold (with the operator token only)"
    )
    base_hot_eth_wei: int | None = Field(
        default=None, description="ETH on the Base hot wallet, for the sweeps' gas (with the operator token only)"
    )
    credits: str | None = Field(default=None, description="Credits door: off, sign_in, ok (purchases on)")
    fiat: str | None = Field(default=None, description="Card top-ups: off, or the provider (polar:production, …)")


class AnalyzeResponse(BaseModel):
    """Response body for the analyze endpoint."""

    score: int = Field(ge=-1, le=100, description="Reliability score: 0 (scam) → 100 (legitimate); -1 = not analysed.")
    explanation: str = Field(description="One-line explanation of the score.")
    page_type: str | None = Field(
        default=None,
        description="What kind of page was checked: aggregate_listing (a product on a marketplace), storefront (a "
        "shop's own product page), p2p_listing (a private person's ad), service_listing (a service provider on a "
        "platform), service_site (a service company's own site), article or non_product (not analysed). The same "
        "value the stream's classify step carries; new values may be added.",
    )
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
        description="Whether this analysis used a check: false for pages that couldn't be analysed (score -1), "
        "quick checks, and a result this account already paid for within the hour, served again from the cache. "
        "Any other result from the cache uses a check like a fresh analysis.",
    )
    tier: str | None = Field(
        default=None, description="Which check this was: deep (the full analysis) or quick (rules only, free)."
    )
    free_check: bool | None = Field(
        default=None, description="True when this check was one of the free ones (a pass's, or a day's without one)."
    )
    report_token: str | None = Field(
        default=None,
        description="Pass to POST /v1/reports to report this result as wrong (within 7 days, by the same account), "
        "for a quick check as for a deep one. Absent for pages that couldn't be analysed and without an account.",
    )
    verdict: Verdict | None = Field(
        default=None,
        description="For shoppers: the band in words, a headline and up to three plain reasons, in English.",
    )
    share_token: str | None = Field(
        default=None,
        description="Pass to POST /v1/shares (without Authorization) with the url and this result to make the "
        "verdict public as a link (within 7 days; it names no account). Absent for pages that couldn't be analysed. "
        "A check that included checkout_content can't be shared.",
    )
