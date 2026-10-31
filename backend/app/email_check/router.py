"""POST /api/email/analyze -- phishing-signal triage for one opened email.

Rules score links, sender and attachments; a pretrained text model scores the
wording and is added as one more finding. Stateless: nothing is stored, and the
message content is never logged.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app.config import get_settings
from app.email_check.model_client import ModelUnavailable, load_model
from app.email_check.schemas import EmailAnalyzeRequest, EmailAnalyzeResponse, ModelInfo
from app.email_check.scorer import HIGH_AT, MEDIUM_AT, score_email, with_model_finding

logger = logging.getLogger("app.email_check")

router = APIRouter(prefix="/api/email", tags=["email"])

DISCLAIMER = (
    "Advisory only. This checks fixed phishing signals and, when enabled, the "
    "wording with a pretrained language model; it cannot prove an email is "
    "genuine, and it does not decide whether text was written by AI."
)

SUMMARIES = {
    "high": "Several phishing signals found. Do not click links or open attachments.",
    "medium": "Some phishing signals found. Verify the sender another way first.",
    "low": "No phishing signals found in this message.",
    "unknown": "Not enough content to check.",
}

MIN_MODEL_CHARS = 20


CHAT_DISCLAIMER = (
    "Advisory only. This checks fixed scam signals in the message text and links; "
    "it cannot prove a message is genuine or identify who sent it."
)

chat_router = APIRouter(prefix="/api/chat", tags=["chat"])


async def _model_verdict(
    text: str, *, model_id: str, indexes: str, threshold: float
) -> tuple[float | None, ModelInfo]:
    """Run a text model. Never raises: failure becomes an 'unavailable' status."""
    if not model_id:
        return None, ModelInfo(status="disabled")
    text = text.strip()
    if len(text) < MIN_MODEL_CHARS:
        return None, ModelInfo(id=model_id, status="disabled", detail="too little text for the model")
    try:
        model = await asyncio.to_thread(load_model, model_id, indexes)
        p_phishing = await asyncio.to_thread(model.p_phishing, text)
    except ModelUnavailable as exc:
        logger.warning("text model unavailable: %s", exc)
        return None, ModelInfo(id=model_id, status="unavailable", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - inference must never break the check
        logger.warning("text model failed: %s", type(exc).__name__)
        return None, ModelInfo(id=model_id, status="unavailable", detail=type(exc).__name__)
    status = "used" if p_phishing >= threshold else "below_threshold"
    return p_phishing, ModelInfo(id=model_id, p_phishing=round(p_phishing, 4), status=status)


@chat_router.post("/analyze", response_model=EmailAnalyzeResponse)
async def analyze_chat(request: EmailAnalyzeRequest) -> EmailAnalyzeResponse:
    """Score one received chat message: rules, chat-only rules, and the chat model.

    The chat model is configured separately (CHAT_MODEL_*) because a model's
    ranking on email says nothing about short informal chat. Its points are
    capped below the "high" bar, like the email model's.
    """
    settings = get_settings()
    score, level, findings, raw_points = score_email(request, chat=True)
    model_info = ModelInfo(status="disabled")
    if level != "unknown":
        p_phishing, model_info = await _model_verdict(
            request.body,
            model_id=settings.chat_model_id,
            indexes=settings.chat_model_phishing_indexes,
            threshold=settings.chat_model_threshold,
        )
        if p_phishing is not None:
            score, level, findings, raw_points = with_model_finding(
                findings,
                p_phishing,
                threshold=settings.chat_model_threshold,
                max_points=settings.chat_model_max_points,
            )
    logger.info(
        "chat message checked: level=%s score=%d findings=%s links=%d model=%s",
        level,
        score,
        ",".join(f.code for f in findings) or "-",
        len(request.links),
        model_info.status,
    )
    return EmailAnalyzeResponse(
        score=score,
        raw_points=raw_points,
        level=level,
        thresholds={"medium": MEDIUM_AT, "high": HIGH_AT},
        findings=findings,
        summary=SUMMARIES[level],
        disclaimer=CHAT_DISCLAIMER,
        model=model_info,
    )


@router.post("/analyze", response_model=EmailAnalyzeResponse)
async def analyze(request: EmailAnalyzeRequest) -> EmailAnalyzeResponse:
    score, level, findings, raw_points = score_email(request)
    model_info = ModelInfo(status="disabled")
    if level != "unknown":
        settings = get_settings()
        p_phishing, model_info = await _model_verdict(
            f"{request.subject}\n{request.body}",
            model_id=settings.email_model_id,
            indexes=settings.email_model_phishing_indexes,
            threshold=settings.email_model_threshold,
        )
        if p_phishing is not None:
            score, level, findings, raw_points = with_model_finding(
                findings,
                p_phishing,
                threshold=settings.email_model_threshold,
                max_points=settings.email_model_max_points,
            )
    # Counts and codes only -- never sender, subject, body or URLs.
    logger.info(
        "email checked: level=%s score=%d findings=%s links=%d attachments=%d model=%s",
        level,
        score,
        ",".join(f.code for f in findings) or "-",
        len(request.links),
        len(request.attachments),
        model_info.status,
    )
    return EmailAnalyzeResponse(
        score=score,
        raw_points=raw_points,
        level=level,
        thresholds={"medium": MEDIUM_AT, "high": HIGH_AT},
        findings=findings,
        summary=SUMMARIES[level],
        disclaimer=DISCLAIMER,
        model=model_info,
    )
