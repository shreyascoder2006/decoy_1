"""FastAPI application entry point.

The live-call deepfake detection channel is mounted here as an isolated
router. Any other input channel (for example a chat-message pipeline) would be
mounted alongside it without sharing transport.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.live_call import router as live_call_router
from app.live_call.session_manager import SessionRegistry, default_client_factory
from app.logging_config import configure_logging

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging()
    app.state.live_call_registry = SessionRegistry(settings)
    app.state.live_call_client_factory = default_client_factory

    if settings.use_mock_provider:
        logger.warning(
            "LIVE CALL MOCK MODE IS ENABLED - detector verdicts are scripted "
            "and must not be presented as real detection results"
        )
    elif settings.use_local_provider:
        logger.info(
            "local detector selected (%s); no credential needed. The model is "
            "loaded on the first analysis session.",
            settings.live_call_local_model_id,
        )
    elif not settings.resemble_api_key:
        # Only meaningful for a hosted provider: the local detector needs no key.
        logger.warning(
            "no provider API key is configured; live-call sessions will report "
            "DETECTOR_UNAVAILABLE rather than a verdict"
        )
    logger.info(
        "live-call channel ready (provider=%s, mock=%s)",
        settings.live_call_provider,
        settings.use_mock_provider,
    )
    yield
    app.state.live_call_registry = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Live Call Deepfake Detection Backend",
        version="0.1.0",
        description=(
            "Relays WhatsApp Web tab audio captured by a Chrome extension to a "
            "streaming AI-voice detection provider and returns advisory alerts."
        ),
        lifespan=lifespan,
    )
    app.include_router(live_call_router)

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_settings()
        return {
            "status": "ok",
            "live_call": {
                "provider": settings.live_call_provider,
                "mock_mode": settings.use_mock_provider,
                "provider_key_configured": bool(settings.resemble_api_key),
                "sample_rate": settings.live_call_sample_rate,
                "channels": settings.live_call_channels,
            },
        }

    return app


app = create_app()
