"""HTTP and WebSocket routes for the live-call channel."""

from __future__ import annotations

import fnmatch
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status

from app.config import Settings, get_settings
from app.live_call.audio_format import AudioContract
from app.live_call.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    SessionStatusResponse,
)
from app.live_call.session_manager import (
    SessionRegistry,
    default_client_factory,
)

logger = logging.getLogger("live_call.router")

router = APIRouter(tags=["live-call"])


def get_registry(request: Request) -> SessionRegistry:
    return request.app.state.live_call_registry


def get_registry_ws(websocket: WebSocket) -> SessionRegistry:
    return websocket.app.state.live_call_registry


def get_client_factory(websocket: WebSocket):
    """Resolve the detector factory, allowing tests to substitute a fake."""
    return getattr(
        websocket.app.state, "live_call_client_factory", default_client_factory
    )


def origin_allowed(origin: str | None, patterns: list[str]) -> bool:
    """Check a WebSocket ``Origin`` header against the configured allowlist.

    A missing Origin is permitted because non-browser clients (our own tests
    and a ``websocat``-style manual check against localhost) do not send one.
    Browsers always do, so a browser from a disallowed origin is still blocked.
    """
    if origin is None or origin == "":
        return True
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(origin, pattern):
                return True
        elif origin == pattern or origin.startswith(pattern + ":"):
            # A bare scheme://host pattern also admits an explicit port.
            # Concatenating ":" is what stops "http://127.0.0.1" from
            # matching the unrelated host "http://127.0.0.1.evil.test".
            return True
    return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@router.post(
    "/api/live-calls",
    response_model=CreateSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_live_call(
    payload: CreateSessionRequest,
    registry: SessionRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
) -> CreateSessionResponse:
    """Allocate a session id and tell the extension where to stream audio."""
    contract = AudioContract(
        sample_rate=payload.sample_rate,
        channels=payload.channels,
        encoding=payload.audio_format,
    )
    try:
        session = registry.create(contract)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    websocket_url = f"{settings.public_base_url}/ws/live-calls/{session.session_id}"
    logger.info("created live-call session %s", session.session_id)
    return CreateSessionResponse(
        session_id=session.session_id,
        websocket_url=websocket_url,
        provider=session.provider_name,
        mock_mode=session.is_mock,
    )


@router.get("/api/live-calls/{session_id}", response_model=SessionStatusResponse)
async def get_live_call(
    session_id: str,
    registry: SessionRegistry = Depends(get_registry),
) -> SessionStatusResponse:
    """Status for popup reconnection. Never returns audio or credentials."""
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown session"
        )
    return SessionStatusResponse(
        session_id=session.session_id,
        state=session.state,
        provider=session.provider_name,
        mock_mode=session.is_mock,
        started_at=session.metrics.started_at,
        elapsed_seconds=session.metrics.elapsed_seconds,
        audio_seconds_sent=round(session.metrics.audio_seconds_sent, 3),
        frames_received=session.metrics.frames_received,
        frames_dropped=session.metrics.frames_dropped,
        verdicts_received=session.metrics.verdicts_received,
        first_verdict_latency_seconds=session.metrics.first_verdict_latency_seconds,
        alert_latency_seconds=session.metrics.alert_latency_seconds,
        last_label=session.last_label,
        alert_raised=session.alert_raised,
        error=session.error,
    )


@router.post("/api/live-calls/{session_id}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_live_call(
    session_id: str,
    registry: SessionRegistry = Depends(get_registry),
) -> dict[str, str]:
    """Forget a session. The WebSocket ``stop`` frame is the primary path.

    This exists so a popup that lost its socket can still release backend
    state. It does not interrupt the audio stream; closing the socket does.
    """
    session = registry.get(session_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unknown session"
        )
    registry.remove(session_id)
    return {"session_id": session_id, "status": "released"}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@router.websocket("/ws/live-calls/{session_id}")
async def live_call_socket(
    websocket: WebSocket,
    session_id: str,
    registry: SessionRegistry = Depends(get_registry_ws),
    settings: Settings = Depends(get_settings),
) -> None:
    """Relay PCM from the extension to the provider and events back."""
    origin = websocket.headers.get("origin")
    if not origin_allowed(origin, settings.allowed_origins):
        logger.warning("rejecting websocket from disallowed origin")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    session = registry.get(session_id)
    if session is None:
        # Accept-then-close so the extension can read a reason rather than
        # guessing from a bare handshake failure.
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "code": "UNKNOWN_SESSION",
                "message": "This live-call session does not exist or has expired.",
            }
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    client_factory = get_client_factory(websocket)
    try:
        await session.run(websocket, client_factory)
    finally:
        registry.remove(session_id)
