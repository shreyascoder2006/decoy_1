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
    live_call_provider: Literal["resemble", "local", "mock"] = Field(
        default="local", alias="LIVE_CALL_PROVIDER"
    )
    live_call_mock_mode: bool = Field(default=False, alias="LIVE_CALL_MOCK_MODE")

    # --- Local open-source detector --------------------------------------
    # Runs in-process: no credential, no credits, no network at call time.
    live_call_local_model_id: str = Field(
        # Chosen by measurement against ElevenLabs output and a real
        # WhatsApp-codec human sample: AUC 1.000 with a very wide margin
        # (human 0.0006-0.0011, AI 0.78-0.999), and 378 MB rather than the
        # 1.26 GB of the large model that scored the same AUC.
        #
        # Of 11 community models measured on that data, 8 were INVERTED --
        # they scored AI speech lower than human speech. One scored AUC
        # 0.922 with every value inside a 0.013-wide band, so it had perfect
        # ordering and no usable threshold. Detector choice does not transfer
        # between TTS engines: see docs/DETECTOR_BENCHMARK.md and re-run
        # scripts/benchmark_detectors.py before changing this.
        default="Shahzaib-Arshad/deepfake_audio_detection",
        alias="LIVE_CALL_LOCAL_MODEL_ID",
    )
    live_call_local_device: str = Field(default="cpu", alias="LIVE_CALL_LOCAL_DEVICE")
    live_call_local_window_seconds: float = Field(
        default=4.0, alias="LIVE_CALL_LOCAL_WINDOW_SECONDS"
    )
    # Calibration-dependent. Tune on your own samples with scripts/detect_file.py.
    # 0.5 sits in the empty band between the two measured clusters
    # (human max 0.121, AI min 0.784), leaving margin on both sides for
    # codec degradation to move scores without flipping a verdict.
    live_call_local_fake_threshold: float = Field(
        default=0.5, alias="LIVE_CALL_LOCAL_FAKE_THRESHOLD"
    )
    # Voice-activity floor. Windows quieter than this are reported as skipped
    # rather than labelled, so silence cannot raise an alert.
    live_call_local_min_rms: float = Field(
        default=0.005, alias="LIVE_CALL_LOCAL_MIN_RMS"
    )
    # Escape hatch for a model whose own id2label is uninformative, e.g.
    # "0=fake,1=real". Leave empty to trust the model's labels.
    live_call_local_label_override: str = Field(
        default="", alias="LIVE_CALL_LOCAL_LABEL_OVERRIDE"
    )

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

    # --- Alert policy -----------------------------------------------------
    # Consecutive synthetic windows required before warning. 1 warns fastest
    # but multiplies the per-window false-alarm rate across a call; 2 is the
    # measured compromise for the default local model. See alert_policy.
    live_call_alert_min_fake_windows: int = Field(
        default=2, alias="LIVE_CALL_ALERT_MIN_FAKE_WINDOWS"
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

    @field_validator("live_call_local_fake_threshold")
    @classmethod
    def _threshold_range(cls, value: float) -> float:
        if not 0.0 < value < 1.0:
            raise ValueError("live_call_local_fake_threshold must be between 0 and 1")
        return value

    @field_validator("live_call_local_window_seconds")
    @classmethod
    def _window_range(cls, value: float) -> float:
        if not 0.5 <= value <= 30.0:
            raise ValueError("live_call_local_window_seconds must be 0.5-30")
        return value

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
    def use_local_provider(self) -> bool:
        """True when detection runs in-process with no hosted provider."""
        return not self.use_mock_provider and self.live_call_provider == "local"

    @property
    def public_base_url(self) -> str:
        if self.live_call_public_base_url:
            return self.live_call_public_base_url.rstrip("/")
        return f"ws://{self.live_call_host}:{self.live_call_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
