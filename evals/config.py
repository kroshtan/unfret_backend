from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EvalSettings(BaseSettings):
    """Settings for the evaluation tooling, read from ``UNFRET_``-prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="UNFRET_", env_file=".env", extra="ignore")

    corpus_dir: Path = Path.home() / "repos" / "unfret-corpus"
    # The second-opinion labeller must differ from the pipeline's own model, or it only grades itself.
    labeller_model: str = "qwen3.5-397b-a17b"


eval_settings = EvalSettings()
