"""Runtime configuration, loaded from the environment / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Inference. No default endpoint on purpose: single-tenant by construction.
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str = "EMPTY"
    llm_reasoning: bool = True
    llm_timeout_seconds: float = 120.0
    # Reasoning traces need headroom; NVIDIA suggests ~10k for Nemotron 3 Nano.
    llm_max_tokens: int = Field(default=10_000, ge=256)
    # NVIDIA recommends temperature 1.0 / top_p 1.0 when reasoning is on.
    llm_temperature_reasoning: float = Field(default=1.0, ge=0, le=2)
    # How to ask the server for JSON: "json_schema" (OpenAI/vLLM response_format),
    # "guided_json" (vLLM extra_body), or "prompt" (no server-side enforcement).
    llm_structured_mode: Literal["json_schema", "guided_json", "prompt"] = "json_schema"

    # Verification (optional).
    tavily_api_key: str | None = None
    max_searches_per_review: int = Field(default=5, ge=0)

    # App.
    vaults_dir: Path = Path("vaults")
    audit_log_path: Path = Path("audit/audit.jsonl")
    max_upload_mb: int = Field(default=20, ge=1)
    max_workers: int = Field(default=4, ge=1)
    max_document_chars: int = Field(default=300_000, ge=1000)

    def require_llm(self) -> tuple[str, str]:
        """Return (base_url, model) or raise a clear error if unset."""
        missing = [
            name
            for name, value in (("LLM_BASE_URL", self.llm_base_url), ("LLM_MODEL", self.llm_model))
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Missing required setting(s): {', '.join(missing)}. Sentinel has no default "
                "inference endpoint; point LLM_BASE_URL at your dedicated vLLM server."
            )
        assert self.llm_base_url and self.llm_model
        return self.llm_base_url, self.llm_model

    @property
    def llm_host(self) -> str | None:
        """Host the LLM client talks to (for startup logs and /healthz)."""
        return urlparse(self.llm_base_url).netloc if self.llm_base_url else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
