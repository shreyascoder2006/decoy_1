"""POST /api/email/analyze -- phishing-signal triage for one opened email.

Stateless: nothing is stored, and the message content is never logged.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.email_check.schemas import EmailAnalyzeRequest, EmailAnalyzeResponse
from app.email_check.scorer import score_email

logger = logging.getLogger("app.email_check")

router = APIRouter(prefix="/api/email", tags=["email"])

DISCLAIMER = (
    "Advisory only. This checks fixed phishing signals in the message; it cannot "
    "prove an email is genuine, and it does not decide whether text was written by AI."
)

SUMMARIES = {
    "high": "Several phishing signals found. Do not click links or open attachments.",
    "medium": "Some phishing signals found. Verify the sender another way first.",
    "low": "No phishing signals found in this message.",
    "unknown": "Not enough content to check.",
}


@router.post("/analyze", response_model=EmailAnalyzeResponse)
async def analyze(request: EmailAnalyzeRequest) -> EmailAnalyzeResponse:
    score, level, findings = score_email(request)
    # Counts and codes only -- never sender, subject, body or URLs.
    logger.info(
        "email checked: level=%s score=%d findings=%s links=%d attachments=%d",
        level,
        score,
        ",".join(f.code for f in findings) or "-",
        len(request.links),
        len(request.attachments),
    )
    return EmailAnalyzeResponse(
        score=score,
        level=level,
        findings=findings,
        summary=SUMMARIES[level],
        disclaimer=DISCLAIMER,
    )
