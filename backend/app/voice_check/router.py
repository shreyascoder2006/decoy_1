"""POST /api/voice/analyze -- score one voice note with the local detector.

The body is raw mono 16 kHz s16le PCM, the same wire format as the live call.
Nothing is stored or logged beyond counts, and the audio never leaves this
machine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.live_call.local_model_client import ModelEngine, pcm16_rms, pcm16_to_floats
from app.live_call.resemble_client import DetectorUnavailable
from app.voice_check.scoring import WindowScore, summarise

logger = logging.getLogger("app.voice_check")

router = APIRouter(prefix="/api/voice", tags=["voice"])

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MAX_SECONDS = 300
MIN_TAIL_SECONDS = 2.0

DISCLAIMER = (
    "Advisory only. This estimates whether the voice shows synthetic-speech "
    "signals; it cannot identify the speaker or prove a voice is genuine."
)
SUMMARIES = {
    "high": "Strong synthetic-speech signals in this voice note.",
    "medium": "A synthetic-speech signal appeared in part of this voice note.",
    "low": "No synthetic-speech signal found in this voice note.",
    "unknown": "Not enough speech to check.",
}


class WindowOut(BaseModel):
    start_s: float
    end_s: float
    p_fake: float | None


class VoiceAnalyzeResponse(BaseModel):
    score: int
    level: Literal["low", "medium", "high", "unknown"]
    reasons: list[str]
    windows: list[WindowOut]
    summary: str
    disclaimer: str
    model_id: str
    threshold: float


def load_engine() -> ModelEngine:
    settings = get_settings()
    return ModelEngine.load(
        settings.live_call_local_model_id,
        settings.live_call_local_device,
        settings.live_call_local_label_override,
    )


def split_windows(pcm: bytes, window_seconds: float) -> list[tuple[float, bytes]]:
    """Non-overlapping windows; a short tail is kept only if it is long enough to judge."""
    size = int(window_seconds * BYTES_PER_SECOND) // 2 * 2
    windows: list[tuple[float, bytes]] = []
    for offset in range(0, len(pcm), size):
        chunk = pcm[offset : offset + size]
        if len(chunk) == size or len(chunk) >= MIN_TAIL_SECONDS * BYTES_PER_SECOND:
            windows.append((offset / BYTES_PER_SECOND, chunk))
    return windows


@router.post("/analyze", response_model=VoiceAnalyzeResponse)
async def analyze(request: Request) -> VoiceAnalyzeResponse:
    settings = get_settings()
    if not settings.use_local_provider:
        raise HTTPException(
            status_code=503,
            detail="voice-note analysis needs the local detector (LIVE_CALL_PROVIDER=local)",
        )

    pcm = await request.body()
    if len(pcm) % 2 or not pcm:
        raise HTTPException(status_code=422, detail="body must be non-empty s16le PCM")
    if len(pcm) > MAX_SECONDS * BYTES_PER_SECOND:
        raise HTTPException(status_code=413, detail=f"voice note longer than {MAX_SECONDS}s")

    try:
        engine = await asyncio.to_thread(load_engine)
    except DetectorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    window_seconds = settings.live_call_local_window_seconds
    scores: list[WindowScore] = []
    for start, chunk in split_windows(pcm, window_seconds):
        end = start + len(chunk) / BYTES_PER_SECOND
        if pcm16_rms(chunk) < settings.live_call_local_min_rms:
            scores.append(WindowScore(start, end, None))
            continue
        result = await asyncio.to_thread(engine.classify, pcm16_to_floats(chunk))
        scores.append(WindowScore(start, end, result.p_fake))

    summary = summarise(
        scores,
        threshold=settings.live_call_local_fake_threshold,
        min_run=settings.live_call_alert_min_fake_windows,
        window_seconds=window_seconds,
    )
    logger.info(
        "voice note checked: level=%s score=%d analysed=%d flagged=%d run=%d skipped=%d seconds=%.1f",
        summary.level,
        summary.score,
        summary.analysed,
        summary.flagged,
        summary.longest_run,
        summary.skipped,
        len(pcm) / BYTES_PER_SECOND,
    )
    return VoiceAnalyzeResponse(
        score=summary.score,
        level=summary.level,
        reasons=summary.reasons,
        windows=[
            WindowOut(
                start_s=round(w.start_s, 2),
                end_s=round(w.end_s, 2),
                p_fake=None if w.p_fake is None else round(w.p_fake, 4),
            )
            for w in scores
        ],
        summary=SUMMARIES[summary.level],
        disclaimer=DISCLAIMER,
        model_id=engine.model_id,
        threshold=settings.live_call_local_fake_threshold,
    )
