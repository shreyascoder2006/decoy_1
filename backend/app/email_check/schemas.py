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
    message: str
    points: int


class EmailAnalyzeResponse(BaseModel):
    score: int
    level: Literal["low", "medium", "high", "unknown"]
    findings: list[Finding]
    summary: str
    disclaimer: str
