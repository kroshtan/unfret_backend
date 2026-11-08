from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Service configuration, read from ``VERIDEX_``-prefixed environment variables or a git-ignored ``.env``.

    Secrets default to empty so the app can be imported (e.g. in tests) without them; each feature that needs
    one fails closed while it is unset.
    """

    model_config = SettingsConfigDict(env_prefix="VERIDEX_", env_file=".env", extra="ignore")

    # LLM: any OpenAI-compatible chat completions API (Scaleway Generative APIs by default)
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.scaleway.ai/v1"
    llm_model: str = "mistral-small-3.2-24b-instruct-2506"
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 1
    # Embeddings for review/title similarity, from the same OpenAI-compatible provider.
    embedding_model: str = "bge-multilingual-gemma2"

    # Analysis limits
    max_page_bytes: int = 2 * 1024 * 1024
    skill_timeout_s: float = 45.0
    analysis_timeout_s: float = 90.0

    # Outbound lookups about the shop (RDAP, certificate transparency, EU VIES). Off in offline evaluation, where
    # today's registry data says nothing about a page captured years ago.
    network_lookups: bool = True
    rdap_bootstrap_url: str = "https://rdap.org/domain/"
    crtsh_url: str = "https://crt.sh/"
    vies_url: str = "https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-number"

    # TinEye reverse image search (paid per search): off while the key is unset; at most this many searches per
    # calendar month, after which the skill reports itself unavailable instead of spending more.
    tineye_api_key: SecretStr = SecretStr("")
    tineye_api_url: str = "https://api.tineye.com/rest/"
    tineye_monthly_budget: int = 1_000
    # Expensive skill runs (e.g. TinEye searches) the triage may spend on one analysis.
    triage_max_expensive_runs: int = 2

    # Database
    postgres_uri: str = "postgresql://veridex:veridex@localhost:5432/veridex"

    # Credits door: wallets sign in with SIWS and spend a balance of micro-dollars (POST /v1/analyze). Sign-in is
    # off (503) until the session secret is set; buying credits also needs the x402 hot wallet, which receives them.
    session_secret: SecretStr = SecretStr("")
    siws_domain: str = "veridex.kroshtan.com"
    session_ttl_s: int = 30 * 24 * 3600
    nonce_ttl_s: int = 300
    pairing_ttl_s: int = 300  # phone sign-in pairings (in memory)
    pairing_per_ip: int = 10  # pairings a visitor may start per 10 minutes
    # Prices are canonical in US dollars, as integer micro-dollars (1 USD = 1_000_000 = USDC's base unit). USDC is
    # charged exactly; SOL (wSOL) is converted at quote and payment time from Pyth's SOL/USD price, read on chain
    # through our RPC, and refused while that price is stale or uncertain. Credit balances are micro-dollars too.
    analysis_price_usd_micros: int = 150_000  # $0.15 per extension analysis
    # (minimum purchase in micro-dollars, bonus percent): ≥$5 buys 10% more balance, ≥$25 20% more.
    credit_bonus_tiers: list[tuple[int, int]] = [(5_000_000, 10), (25_000_000, 20)]
    suggested_packs: list[int] = [10, 50, 200]  # checks
    # Purchase limits by dollar value, whatever the currency: at least one check, at most $1,000.
    purchase_min_usd_micros: int = 150_000
    purchase_max_usd_micros: int = 1_000_000_000
    pyth_sol_usd_account: str = "7UVimffxr9ow1uXYxsr4LHAcV58mLzhmwaeKvJ1pjLiE"  # sponsored push feed, shard 0
    # Where the oracle is read: empty means our own RPC on mainnet and mainnet's public RPC on devnet (Pyth's devnet
    # copy of the feed updates too rarely to pass the staleness check; the price is the same real SOL/USD price).
    pyth_rpc_url: str = ""
    pyth_max_age_s: float = 60.0
    pyth_max_conf_bps: int = 100  # confidence interval at most 1% of the price
    # How far an x402 wSOL payment may differ from the current quote (SOL moves between 402 and payment). USDC
    # amounts are exact.
    quote_tolerance_bps: int = 200
    # USDC mint; empty picks Circle's mint for the configured network.
    usdc_mint: str = ""

    # USDC received by the hot wallet is swapped to SOL (through Jupiter's swap API) before sweeping, so the cold
    # wallet only ever receives SOL. Every swap is verified by simulation before the hot wallet signs it, including
    # a floor from the Pyth price, so no swap runs while the oracle is unavailable. Mainnet only: Jupiter has no
    # devnet routes, so on devnet USDC stays in the hot wallet (test money).
    swap_api_url: str = "https://lite-api.jup.ag/swap/v1"
    swap_api_key: SecretStr = SecretStr("")
    swap_threshold_usdc: int = 5_000_000  # swap once the hot wallet holds this much USDC
    swap_dust_usdc: int = 500_000  # …or at least this much and nothing was swapped for swap_max_interval_s
    swap_max_interval_s: float = 24 * 3600.0
    swap_max_slippage_bps: int = 100
    swap_oracle_tolerance_bps: int = 300  # minimum output at most this far below the Pyth-implied amount
    swap_max_priority_lamports: int = 100_000
    swap_max_compute_units: int = 1_400_000
    credits_scan_interval_s: float = 20.0
    # Free trial on a wallet's first sign-in, only for wallets with real on-chain history.
    trial_checks: int = 5
    trial_min_wallet_age_days: int = 30
    trial_min_signatures: int = 10
    trial_per_ip_per_day: int = 2
    trial_daily_cap: int = 50

    # x402 pay-per-check (POST /v1/check). Off (503) until both the hot wallet key and the cold address are set.
    public_base_url: str = "https://veridex.kroshtan.com"
    solana_network: Literal["solana-devnet", "solana-mainnet"] = "solana-devnet"
    solana_rpc_url: str = "https://api.devnet.solana.com"
    # Mainnet stays refused until this is set deliberately.
    x402_allow_mainnet: bool = False
    # The hot wallet receives payments (its wSOL account) and pays their fees; base58 secret or a Solana CLI JSON
    # byte array. The only transfers it ever originates are sweeps to the cold address.
    hot_wallet_key: SecretStr = SecretStr("")
    # The cold wallet (e.g. a Ledger): sweep destination only. The server never holds its key.
    cold_address: str = ""
    # Sweep to cold once the hot wallet holds more than this (SOL + wSOL), keeping the reserve for fees.
    sweep_threshold_lamports: int = 100_000_000
    sweep_reserve_lamports: int = 10_000_000
    sweep_interval_s: float = 60.0
    # Payments are refused (503) while the hot wallet's SOL (for fees) is below this. Kept under the reserve, and
    # a sweep refills it from received wSOL, so normal operation never trips it.
    x402_min_fee_lamports: int = 2_000_000
    x402_price_quick_usd_micros: int = 10_000  # $0.01
    x402_price_deep_usd_micros: int = 150_000  # $0.15
    x402_max_compute_units: int = 400_000
    x402_max_compute_unit_price_microlamports: int = 50_000
    x402_confirm_timeout_s: float = 30.0

    @property
    def analysis_price_lamports(self) -> int:
        """
        Deprecated alias of :attr:`analysis_price_usd_micros`: the analysis price in balance units (micro-dollars).

        Kept so code that still says "lamports" for the balance keeps debiting the right amount until it's renamed.

        :return: Micro-dollars.
        """
        return self.analysis_price_usd_micros


settings = Settings()
