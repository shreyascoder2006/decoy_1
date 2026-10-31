"""Typed protocol models for the live-call channel.

Three boundaries are modelled here:
  1. HTTP session creation  (extension -> backend)
  2. WebSocket control frames (extension <-> backend)
  3. Provider result frames  (provider -> backend)

Provider models are deliberately tolerant of unknown fields so that a provider
adding a field does not break the demo.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# 1. HTTP session creation
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["whatsapp_web_tab"] = "whatsapp_web_tab"
    audio_format: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: int = 16000
    channels: int = 1

    @field_validator("sample_rate")
    @classmethod
    def _rate(cls, value: int) -> int:
        if value != 16000:
            raise ValueError("this MVP only accepts a 16000 Hz stream")
        return value

    @field_validator("channels")
    @classmethod
    def _channels(cls, value: int) -> int:
        if value != 1:
            raise ValueError("this MVP only accepts mono audio")
        return value


class CreateSessionResponse(BaseModel):
    session_id: str
    websocket_url: str
    status: Literal["created"] = "created"
    provider: str
    mock_mode: bool


class SessionStatusResponse(BaseModel):
    session_id: str
    state: str
    provider: str
    mock_mode: bool
    started_at: float | None = None
    elapsed_seconds: float | None = None
    audio_seconds_sent: float = 0.0
    frames_received: int = 0
    frames_dropped: int = 0
    verdicts_received: int = 0
    first_verdict_latency_seconds: float | None = None
    alert_latency_seconds: float | None = None
    last_label: str | None = None
    alert_raised: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# 2. Extension -> backend control frames
# ---------------------------------------------------------------------------


class StartFrame(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["start"]
    sample_rate: int = 16000
    channels: int = 1
    encoding: Literal["pcm_s16le"] = "pcm_s16le"
    frame_ms: int = 100

    @field_validator("sample_rate")
    @classmethod
    def _rate(cls, value: int) -> int:
        if value != 16000:
            raise ValueError("sample_rate must be 16000")
        return value

    @field_validator("channels")
    @classmethod
    def _channels(cls, value: int) -> int:
        if value != 1:
            raise ValueError("channels must be 1")
        return value

    @field_validator("frame_ms")
    @classmethod
    def _frame_ms(cls, value: int) -> int:
        if not 10 <= value <= 1000:
            raise ValueError("frame_ms must be between 10 and 1000")
        return value


class StopFrame(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["stop"]


class SpeechMarkFrame(BaseModel):
    """Optional client hint: the extension believes speech just started.

    Used only to start the latency clock at first speech rather than at the
    moment the user pressed Start Protection.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["speech_start"]
    at_ms: float | None = None


# ---------------------------------------------------------------------------
# 3. Backend -> extension events
# ---------------------------------------------------------------------------


class StatusEvent(BaseModel):
    type: Literal["status"] = "status"
    status: str
    session_id: str | None = None
    reason: str | None = None
    detail: str | None = None


class DetectorResultEvent(BaseModel):
    type: Literal["detector_result"] = "detector_result"
    session_id: str
    label: str | None = None
    aggregated_score: float | None = None
    consistency: float | None = None
    window_start_s: float | None = None
    window_end_s: float | None = None
    chunk_label: str | None = None
    provider: str
    provisional: bool = True
    result_kind: str = "chunk"


class AlertEvent(BaseModel):
    type: Literal["alert"] = "alert"
    level: Literal["low", "medium", "high"]
    code: str
    title: str
    message: str
    provisional: bool = True
    session_id: str | None = None
    latency_seconds: float | None = None


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    session_id: str | None = None


class MetricsEvent(BaseModel):
    type: Literal["metrics"] = "metrics"
    session_id: str
    audio_seconds_sent: float
    frames_received: int
    frames_dropped: int
    verdicts_received: int
    first_verdict_latency_seconds: float | None = None
    alert_latency_seconds: float | None = None


# ---------------------------------------------------------------------------
# 4. Provider result frames
# ---------------------------------------------------------------------------


class ProviderChunkInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    begin_timestamp_s: float | None = None
    end_timestamp_s: float | None = None
    chunk_label: str | None = None


class ProviderMessage(BaseModel):
    """A single JSON text frame received from the detection provider.

    `extra="ignore"` is deliberate: the provider may add fields, and an unknown
    field must never break an in-progress call.
    """

    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    label: str | None = None
    aggregated_score: float | None = None
    consistency: float | None = None
    chunk_info: ProviderChunkInfo | None = None
    error: str | None = None
    error_message: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.type == "ready"

    @property
    def is_chunk(self) -> bool:
        return self.type == "chunk"

    @property
    def is_final(self) -> bool:
        return self.type == "final"

    @property
    def is_error(self) -> bool:
        return self.type == "error" or bool(self.error) or bool(self.error_message)

    @property
    def error_text(self) -> str | None:
        return self.error_message or self.error

    @property
    def chunk_label(self) -> str | None:
        return self.chunk_info.chunk_label if self.chunk_info else None

    @property
    def window_start_s(self) -> float | None:
        return self.chunk_info.begin_timestamp_s if self.chunk_info else None

    @property
    def window_end_s(self) -> float | None:
        return self.chunk_info.end_timestamp_s if self.chunk_info else None

    @property
    def is_skipped(self) -> bool:
        return (self.chunk_label or "").lower() == "skipped"

    @classmethod
    def parse_payload(cls, payload: Any) -> "ProviderMessage":
        """Parse a decoded JSON payload, tolerating a non-dict body."""
        if isinstance(payload, dict):
            return cls.model_validate(payload)
        return cls(type=None)
