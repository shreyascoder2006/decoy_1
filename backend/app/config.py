"""Backend configuration.

All provider credentials live here, server-side only. Nothing in this module
may ever be serialised to the browser extension.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings for the live-call feature."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Provider credentials (server-side only) -------------------------
    resemble_api_key: str = Field(default="", alias="RESEMBLE_API_KEY")
    resemble_stream_url: str = Field(
        default="wss://stream.resemble.ai/api/v1/detect/audio",
        alias="RESEMBLE_STREAM_URL",
    )

    # --- Provider selection ---------------------------------------------
    live_call_provider: Literal["resemble", "mock"] = Field(
        default="resemble", alias="LIVE_CALL_PROVIDER"
    )
    live_call_mock_mode: bool = Field(default=False, alias="LIVE_CALL_MOCK_MODE")

    # --- Bind address ----------------------------------------------------
    live_call_host: str = Field(default="127.0.0.1", alias="LIVE_CALL_HOST")
    live_call_port: int = Field(default=8000, alias="LIVE_CALL_PORT")
    live_call_public_base_url: str = Field(
        default="", alias="LIVE_CALL_PUBLIC_BASE_URL"
    )

    # --- Transport limits / backpressure ---------------------------------
    live_call_max_frame_bytes: int = Field(
        default=16384, alias="LIVE_CALL_MAX_FRAME_BYTES"
    )
    live_call_session_timeout_seconds: int = Field(
        default=3600, alias="LIVE_CALL_SESSION_TIMEOUT_SECONDS"
    )
    live_call_max_queued_frames: int = Field(
        default=64, alias="LIVE_CALL_MAX_QUEUED_FRAMES"
    )
    live_call_max_dropped_frames: int = Field(
        default=200, alias="LIVE_CALL_MAX_DROPPED_FRAMES"
    )

    # --- Provider handshake timeouts (seconds) ---------------------------
    live_call_connect_timeout_seconds: float = Field(
        default=10.0, alias="LIVE_CALL_CONNECT_TIMEOUT_SECONDS"
    )
    live_call_ready_timeout_seconds: float = Field(
        default=15.0, alias="LIVE_CALL_READY_TIMEOUT_SECONDS"
    )
    live_call_final_timeout_seconds: float = Field(
        default=10.0, alias="LIVE_CALL_FINAL_TIMEOUT_SECONDS"
    )

    # --- Origin allowlist -------------------------------------------------
    # Comma-separated. "chrome-extension://*" allows any unpacked extension ID,
    # which is the practical default for a local hackathon demo because the ID
    # changes each time the extension is reloaded from disk.
    live_call_allowed_origins: str = Field(
        default="chrome-extension://*,http://127.0.0.1,http://localhost",
        alias="LIVE_CALL_ALLOWED_ORIGINS",
    )

    # --- Audio contract ---------------------------------------------------
    live_call_sample_rate: int = Field(default=16000, alias="LIVE_CALL_SAMPLE_RATE")
    live_call_channels: int = Field(default=1, alias="LIVE_CALL_CHANNELS")

    @field_validator("live_call_max_frame_bytes")
    @classmethod
    def _frame_bytes_sane(cls, value: int) -> int:
        if value < 320:
            raise ValueError("live_call_max_frame_bytes must be at least 320 bytes")
        return value

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.live_call_allowed_origins.split(",") if o.strip()]

    @property
    def use_mock_provider(self) -> bool:
        """True when the mock detector should be used instead of a real one.

        Mock mode is only ever enabled by explicit configuration. A missing API
        key does NOT silently fall back to the mock -- that would risk
        presenting fabricated verdicts as real ones.
        """
        return self.live_call_mock_mode or self.live_call_provider == "mock"

    @property
    def public_base_url(self) -> str:
        if self.live_call_public_base_url:
            return self.live_call_public_base_url.rstrip("/")
        return f"ws://{self.live_call_host}:{self.live_call_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
