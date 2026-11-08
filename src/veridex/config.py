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

    # Database
    postgres_uri: str = "postgresql://veridex:veridex@localhost:5432/veridex"

    # Accounts
    signup_secret: str = ""
    free_daily_limit: int = 5
    premium_daily_limit: int = 25

    # Paddle billing
    paddle_webhook_secret: str = ""
    paddle_api_key: str = ""
    paddle_environment: Literal["production", "sandbox"] = "production"


settings = Settings()
