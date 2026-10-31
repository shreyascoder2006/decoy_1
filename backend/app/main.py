"""FastAPI application entry point.

The live-call deepfake detection channel is mounted here as an isolated
router. Any other input channel (for example a chat-message pipeline) would be
mounted alongside it without sharing transport.
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.email_check import chat_router, router as email_router
from app.live_call import router as live_call_router
from app.live_call.session_manager import SessionRegistry, default_client_factory
from app.logging_config import configure_logging
from app.voice_check import router as voice_router

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


def _is_model_cached(model_id: str) -> bool:
    """True when the model appears to be in the local Hugging Face cache.

    Best effort and read-only: a false negative only costs an unnecessary
    warning, never a failed session.
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE  # noqa: PLC0415

        folder = pathlib.Path(HF_HUB_CACHE) / f"models--{model_id.replace('/', '--')}"
        if not folder.is_dir():
            return False
        return any(
            p.suffix in (".safetensors", ".bin")
            for p in folder.rglob("*")
            if p.is_file()
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break /health
        return False


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
    app.include_router(email_router)
    app.include_router(chat_router)
    app.include_router(voice_router)

    @app.get("/health")
    async def health() -> dict[str, object]:
        settings = get_settings()
        body = {
            "status": "ok",
            "live_call": {
                "provider": settings.live_call_provider,
                "mock_mode": settings.use_mock_provider,
                "provider_key_configured": bool(settings.resemble_api_key),
                "sample_rate": settings.live_call_sample_rate,
                "channels": settings.live_call_channels,
            },
        }
        if settings.use_local_provider:
            # Which model is configured, and whether it is already on disk.
            # Without this there is no way to tell a stale server from a
            # slow one: a running process keeps its old configuration, so a
            # restart that was skipped looks identical to a long download.
            model_id = settings.live_call_local_model_id
            body["live_call"]["model_id"] = model_id
            body["live_call"]["model_cached"] = _is_model_cached(model_id)
            body["live_call"]["fake_threshold"] = settings.live_call_local_fake_threshold
        return body

    return app


app = create_app()
