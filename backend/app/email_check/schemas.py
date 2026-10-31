"""Typed request/response models for the email check endpoint."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_BODY_CHARS = 20_000


class EmailLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=500)
    href: str = Field(max_length=2048)


class EmailAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_name: str = Field(default="", max_length=300)
    sender_email: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=1000)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    links: list[EmailLink] = Field(default_factory=list, max_length=200)
    attachments: list[str] = Field(default_factory=list, max_length=50)


class Finding(BaseModel):
    code: str
    # What was found in this specific message (names the evidence).
    message: str
    points: int
    # Why that kind of signal is suspicious, independent of this message.
    reason: str = ""


class ModelInfo(BaseModel):
    """What the pretrained text model contributed to this result."""

    id: str | None = None
    # P(phishing) from the model's own softmax; not a calibrated probability.
    p_phishing: float | None = None
    # used | below_threshold | disabled | unavailable
    status: Literal["used", "below_threshold", "disabled", "unavailable"] = "disabled"
    detail: str | None = None


class EmailAnalyzeResponse(BaseModel):
    score: int
    # Sum of all rule points before the cap at 100.
    raw_points: int
    level: Literal["low", "medium", "high", "unknown"]
    # Lowest score for each level, so the UI can explain the banding.
    thresholds: dict[str, int]
    findings: list[Finding]
    summary: str
    disclaimer: str
    model: ModelInfo = Field(default_factory=ModelInfo)
