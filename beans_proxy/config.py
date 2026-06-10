"""Configuration loader for Beans Proxy.

Reads from environment variables (or a .env file in the working directory).
All settings are required except those with defaults.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Proxy configuration.

    All values can be set via environment variables prefixed or unprefixed;
    unprefixed names are matched exactly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="BEANS_PROXY_",
    )

    # Upstream target. Treated as a base URL.
    target_url: str = Field(
        ...,
        description="Base URL of the upstream LLM API (e.g. https://openrouter.ai/api/v1).",
    )
    target_api_key: str = Field(
        ...,
        description="API key sent to the upstream LLM API.",
    )

    host: str = Field(default="127.0.0.1", description="Host the proxy binds to.")
    port: int = Field(default=8000, description="Port the proxy listens on.")

    usage_dir: str = Field(
        default="token_usage",
        description="Directory where per-key token usage JSON files are stored.",
    )
    log_file: str = Field(
        default="beans_proxy.log",
        description="Path of the log file.",
    )

    # Endpoints under the base URL that have no token cost. They are passed through
    # transparently and not recorded.
    passthrough_paths: tuple[str, ...] = Field(
        default=("/v1/models",),
        description="Path prefixes that are forwarded without recording usage.",
    )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load settings, optionally from a specific .env file."""
    if env_file is not None:
        return Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    return Settings()
