"""Per-call lifecycle for the live-call channel.

One :class:`LiveCallSession` owns one browser WebSocket, one upstream provider
stream, one alert policy and one metrics record. Sessions live in memory only
and are swept on a timeout, which is appropriate for a hackathon MVP and keeps
call audio off disk entirely.

Concurrency model for an active session -- three cooperating tasks:

* the *client reader* (the caller of :meth:`LiveCallSession.run`) reads control
  frames and binary PCM from the extension and enqueues audio;
* the *forwarder* drains that bounded queue into the provider;
* the *pump* reads provider results, runs them through the alert policy and
  writes events back to the extension.

Backpressure policy: the audio queue is bounded. When it is full the newest
frame is dropped and counted, which keeps already-queued audio contiguous for
the provider's analysis windows. Sustained dropping past a configured budget
ends the session with an explicit error rather than growing memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.live_call.alert_policy import CODE_UNAVAILABLE, AlertPolicy
from app.live_call.audio_format import (
    AudioContract,
    is_plausible_pcm_frame,
)
from app.live_call.metrics import SessionMetrics
from app.live_call.resemble_client import (
    DetectorClient,
    DetectorUnavailable,
    MockDetectorClient,
    ResembleStreamingClient,
)
from app.live_call.schemas import (
    ErrorEvent,
    MetricsEvent,
    ProviderMessage,
    SpeechMarkFrame,
    StartFrame,
    StatusEvent,
    StopFrame,
)

logger = logging.getLogger("live_call.session")

STATE_CREATED = "created"
STATE_CONNECTING = "connecting_to_detector"
STATE_READY = "ready"
STATE_ANALYSING = "analysing"
STATE_ALERTED = "potential_synthetic_voice"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"

_SENTINEL = object()
LAG_NOTICE_INTERVAL_SECONDS = 5.0
START_FRAME_TIMEOUT_SECONDS = 15.0


class WebSocketLike:
    """Structural note: we only need these three methods from the socket.

    FastAPI's ``WebSocket`` satisfies this. Tests substitute a fake.
    """

    async def send_text(self, data: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def receive(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError

    async def close(self, code: int = 1000) -> None:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class LiveCallSession:
    session_id: str
    contract: AudioContract
    settings: Settings
    metrics: SessionMetrics = field(default_factory=SessionMetrics)
    state: str = STATE_CREATED
    created_at: float = field(default_factory=time.monotonic)
    error: str | None = None

    _policy: AlertPolicy | None = field(default=None, repr=False)
    _client: DetectorClient | None = field(default=None, repr=False)
    _queue: "asyncio.Queue[Any] | None" = field(default=None, repr=False)
    _socket: Any = field(default=None, repr=False)
    _last_lag_notice: float = field(default=0.0, repr=False)
    _analysing_announced: bool = field(default=False, repr=False)

    # -- derived -----------------------------------------------------------
    @property
    def provider_name(self) -> str:
        if self._client is not None:
            return self._client.provider_name
        return "mock" if self.settings.use_mock_provider else self.settings.live_call_provider

    @property
    def is_mock(self) -> bool:
        if self._client is not None:
            return self._client.is_mock
        return self.settings.use_mock_provider

    @property
    def alert_raised(self) -> bool:
        return bool(self._policy and self._policy.alert_raised)

    @property
    def last_label(self) -> str | None:
        return self._policy.last_label if self._policy else None

    def is_expired(self, now: float | None = None) -> bool:
        moment = now if now is not None else time.monotonic()
        age = moment - self.created_at
        return age > self.settings.live_call_session_timeout_seconds

    # -- socket helpers ----------------------------------------------------
    async def _send(self, event: BaseModel) -> None:
        """Send one event to the extension, tolerating a dead socket."""
        if self._socket is None:
            return
        try:
            await self._socket.send_text(event.model_dump_json(exclude_none=True))
        except Exception:  # noqa: BLE001 - a closed browser must not raise here
            logger.debug("dropping event for a closed client socket", exc_info=True)

    async def _send_status(
        self, status: str, *, reason: str | None = None, detail: str | None = None
    ) -> None:
        await self._send(
            StatusEvent(
                status=status, session_id=self.session_id, reason=reason, detail=detail
            )
        )

    async def _send_error(self, code: str, message: str) -> None:
        await self._send(
            ErrorEvent(code=code, message=message, session_id=self.session_id)
        )

    # -- main entry point ---------------------------------------------------
    async def run(self, socket: Any, client_factory: "ClientFactory") -> None:
        """Drive one accepted browser WebSocket to completion."""
        self._socket = socket
        self._queue = asyncio.Queue(maxsize=self.settings.live_call_max_queued_frames)

        try:
            await self._handshake(client_factory)
        except _SessionAborted as exc:
            logger.info("session %s aborted during handshake: %s", self.session_id, exc)
            return

        assert self._client is not None  # narrowed by a successful handshake

        forwarder = asyncio.create_task(
            self._forward_audio(), name=f"live-call-forward-{self.session_id}"
        )
        pump = asyncio.create_task(
            self._pump_provider(), name=f"live-call-pump-{self.session_id}"
        )

        try:
            await self._read_client()
        except _SessionAborted as exc:
            logger.info("session %s ended early: %s", self.session_id, exc)
        finally:
            await self._shutdown(forwarder, pump)

    # -- handshake ----------------------------------------------------------
    async def _handshake(self, client_factory: "ClientFactory") -> None:
        self.state = STATE_CONNECTING
        self.metrics.mark_started()
        await self._send_status(STATE_CONNECTING)

        start = await self._await_start_frame()
        self.contract = AudioContract(
            sample_rate=start.sample_rate,
            channels=start.channels,
            encoding=start.encoding,
            frame_ms=start.frame_ms,
        )

        self._policy = AlertPolicy(
            self.session_id,
            "mock" if self.settings.use_mock_provider else self.settings.live_call_provider,
        )

        try:
            client = client_factory(self)
            await client.connect()
        except DetectorUnavailable as exc:
            self.state = STATE_ERROR
            self.error = str(exc)
            self.metrics.error = str(exc)
            logger.warning("detector unavailable for %s: %s", self.session_id, exc)
            await self._send_error(CODE_UNAVAILABLE, "Streaming detection is unavailable.")
            raise _SessionAborted(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never leak a provider stack trace
            self.state = STATE_ERROR
            self.error = "unexpected provider failure"
            self.metrics.error = self.error
            logger.exception("unexpected provider failure for %s", self.session_id)
            await self._send_error(CODE_UNAVAILABLE, "Streaming detection is unavailable.")
            raise _SessionAborted(str(exc)) from exc

        self._client = client
        self._policy.provider = client.provider_name
        self.metrics.mark_provider_ready()
        self.state = STATE_READY
        await self._send(
            StatusEvent(status=STATE_READY, session_id=self.session_id)
        )

    async def _await_start_frame(self) -> StartFrame:
        """Read control frames until a valid ``start`` arrives."""
        deadline = asyncio.get_running_loop().time() + START_FRAME_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self._send_error("START_TIMEOUT", "No start frame was received.")
                raise _SessionAborted("start frame timeout")
            try:
                message = await asyncio.wait_for(self._socket.receive(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                await self._send_error("START_TIMEOUT", "No start frame was received.")
                raise _SessionAborted("start frame timeout") from exc
            except Exception as exc:  # noqa: BLE001 - client vanished
                raise _SessionAborted("client disconnected before start") from exc

            if message.get("type") == "websocket.disconnect":
                raise _SessionAborted("client disconnected before start")

            text = message.get("text")
            if text is None:
                # Binary before start is discarded: we do not yet know the format.
                continue
            try:
                payload = json.loads(text)
                return StartFrame.model_validate(payload)
            except (ValueError, ValidationError) as exc:
                logger.info("rejecting malformed start frame for %s: %s", self.session_id, exc)
                await self._send_error("BAD_START_FRAME", "The start frame was invalid.")
                raise _SessionAborted("invalid start frame") from exc

    # -- client reader ------------------------------------------------------
    async def _read_client(self) -> None:
        """Read control frames and PCM from the extension until it stops."""
        max_bytes = self.settings.live_call_max_frame_bytes
        while True:
            try:
                message = await self._socket.receive()
            except Exception:  # noqa: BLE001 - treated as a disconnect
                logger.info("client socket ended for %s", self.session_id)
                return

            if message.get("type") == "websocket.disconnect":
                logger.info("client disconnected for %s", self.session_id)
                return

            payload = message.get("bytes")
            if payload is not None:
                if len(payload) > max_bytes:
                    logger.warning(
                        "frame of %d bytes exceeds the %d byte limit for %s",
                        len(payload),
                        max_bytes,
                        self.session_id,
                    )
                    await self._send_error(
                        "FRAME_TOO_LARGE", "An audio frame exceeded the allowed size."
                    )
                    return
                if not is_plausible_pcm_frame(payload, self.contract.channels):
                    logger.warning("discarding misaligned PCM frame for %s", self.session_id)
                    continue
                await self._accept_audio(payload)
                continue

            text = message.get("text")
            if text is not None:
                should_continue = await self._handle_control_text(text)
                if not should_continue:
                    return

    async def _handle_control_text(self, text: str) -> bool:
        """Handle a control frame. Returns False when the session should end."""
        try:
            payload = json.loads(text)
        except ValueError:
            logger.info("ignoring non-JSON control frame for %s", self.session_id)
            return True

        frame_type = payload.get("type") if isinstance(payload, dict) else None
        if frame_type == "stop":
            StopFrame.model_validate(payload)
            logger.info("stop requested for %s", self.session_id)
            return False
        if frame_type == "speech_start":
            SpeechMarkFrame.model_validate(payload)
            self.metrics.mark_first_speech()
            return True
        if frame_type == "start":
            # A duplicate start is ignored rather than restarting the stream.
            return True
        logger.debug("ignoring unknown control frame type %r", frame_type)
        return True

    async def _accept_audio(self, payload: bytes) -> None:
        assert self._queue is not None
        seconds = self.contract.frame_duration_seconds(len(payload))
        self.metrics.record_frame(len(payload), seconds)

        if not self._analysing_announced:
            self._analysing_announced = True
            self.state = STATE_ANALYSING
            await self._send_status(STATE_ANALYSING)

        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Drop the newest frame: already-queued audio stays contiguous.
            self.metrics.record_dropped()
            await self._maybe_notify_lag()
            if self.metrics.frames_dropped > self.settings.live_call_max_dropped_frames:
                logger.error(
                    "dropping budget exhausted for %s after %d frames",
                    self.session_id,
                    self.metrics.frames_dropped,
                )
                self.error = "backpressure budget exhausted"
                self.metrics.error = self.error
                await self._send_error(
                    "BACKPRESSURE", "Audio could not be relayed fast enough."
                )
                raise _SessionAborted("backpressure budget exhausted")

    async def _maybe_notify_lag(self) -> None:
        now = time.monotonic()
        if now - self._last_lag_notice < LAG_NOTICE_INTERVAL_SECONDS:
            return
        self._last_lag_notice = now
        await self._send_status(
            "lagging",
            reason="relay_backlog",
            detail="Audio is arriving faster than it can be relayed; some frames were dropped.",
        )

    # -- forwarder ----------------------------------------------------------
    async def _forward_audio(self) -> None:
        """Drain the audio queue into the provider, then send the end marker."""
        assert self._queue is not None and self._client is not None
        try:
            while True:
                item = await self._queue.get()
                if item is _SENTINEL:
                    break
                try:
                    await self._client.send_audio(item)
                    self.metrics.record_forwarded()
                except DetectorUnavailable as exc:
                    logger.warning("provider write failed for %s: %s", self.session_id, exc)
                    self.error = str(exc)
                    self.metrics.error = str(exc)
                    await self._send_error(
                        CODE_UNAVAILABLE, "Streaming detection is unavailable."
                    )
                    return
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await self._client.send_end()

    # -- provider pump ------------------------------------------------------
    async def _pump_provider(self) -> None:
        """Read provider results and translate them into extension events."""
        assert self._client is not None and self._policy is not None
        try:
            while True:
                message = await self._client.receive()
                if message is None:
                    logger.info("provider stream ended for %s", self.session_id)
                    return
                await self._handle_provider_message(message)
                if message.is_final:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("provider pump failed for %s", self.session_id)
            await self._send_error(CODE_UNAVAILABLE, "Streaming detection is unavailable.")

    async def _handle_provider_message(self, message: ProviderMessage) -> None:
        assert self._policy is not None
        outcome = self._policy.evaluate(message)

        if outcome.detector_result is not None:
            self.metrics.record_verdict(
                label=outcome.detector_result.label,
                aggregated_score=outcome.detector_result.aggregated_score,
                consistency=outcome.detector_result.consistency,
                window_start_s=outcome.detector_result.window_start_s,
                window_end_s=outcome.detector_result.window_end_s,
            )

        if outcome.alert is not None:
            self.metrics.mark_alert()
            outcome.alert.latency_seconds = self.metrics.alert_latency_seconds
            self.state = STATE_ALERTED
            logger.info(
                "alert %s raised for %s at %.3fs after first speech/audio",
                outcome.alert.code,
                self.session_id,
                outcome.alert.latency_seconds or -1.0,
            )

        if outcome.terminal_error is not None:
            self.state = STATE_ERROR
            self.error = "provider reported an error"
            self.metrics.error = self.error

        for event in outcome.events:
            await self._send(event)

    # -- shutdown -----------------------------------------------------------
    async def _shutdown(self, forwarder: asyncio.Task, pump: asyncio.Task) -> None:
        """Close both sockets and the session, whichever side went away first."""
        assert self._queue is not None

        # Let the forwarder flush what is already queued, then end the stream.
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_SENTINEL)

        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(
                forwarder, timeout=self.settings.live_call_final_timeout_seconds
            )
        if not forwarder.done():
            forwarder.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await forwarder

        # The final aggregate result arrives after the end marker.
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(
                pump, timeout=self.settings.live_call_final_timeout_seconds
            )
        if not pump.done():
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump

        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()

        self.metrics.mark_stopped()
        if self.state not in (STATE_ERROR,):
            self.state = STATE_STOPPED

        await self._send(
            MetricsEvent(
                session_id=self.session_id,
                audio_seconds_sent=round(self.metrics.audio_seconds_sent, 3),
                frames_received=self.metrics.frames_received,
                frames_dropped=self.metrics.frames_dropped,
                verdicts_received=self.metrics.verdicts_received,
                first_verdict_latency_seconds=self.metrics.first_verdict_latency_seconds,
                alert_latency_seconds=self.metrics.alert_latency_seconds,
            )
        )
        await self._send_status(STATE_STOPPED)
        logger.info("session %s finished: %s", self.session_id, self.metrics.snapshot())


class _SessionAborted(Exception):
    """Internal signal that a session cannot continue."""


ClientFactory = Callable[[LiveCallSession], DetectorClient]


def default_client_factory(session: LiveCallSession) -> DetectorClient:
    """Build the configured detector client for a session."""
    settings = session.settings
    if settings.use_mock_provider:
        return MockDetectorClient(
            session_id=session.session_id, contract=session.contract
        )
    return ResembleStreamingClient(
        api_key=settings.resemble_api_key,
        session_id=session.session_id,
        endpoint=settings.resemble_stream_url,
        contract=session.contract,
        connect_timeout=settings.live_call_connect_timeout_seconds,
        ready_timeout=settings.live_call_ready_timeout_seconds,
        final_timeout=settings.live_call_final_timeout_seconds,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SessionRegistry:
    """Bounded in-memory registry of live-call sessions."""

    def __init__(self, settings: Settings, max_sessions: int = 32) -> None:
        self._settings = settings
        self._sessions: dict[str, LiveCallSession] = {}
        self._max_sessions = max_sessions

    def create(self, contract: AudioContract) -> LiveCallSession:
        self.purge_expired()
        if len(self._sessions) >= self._max_sessions:
            raise RuntimeError("too many concurrent live-call sessions")
        # token_urlsafe is cryptographically random; used as an unguessable id.
        session_id = secrets.token_urlsafe(24)
        session = LiveCallSession(
            session_id=session_id, contract=contract, settings=self._settings
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> LiveCallSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.is_expired():
            self._sessions.pop(session_id, None)
            return None
        return session

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def purge_expired(self) -> int:
        now = time.monotonic()
        stale = [sid for sid, s in self._sessions.items() if s.is_expired(now)]
        for sid in stale:
            self._sessions.pop(sid, None)
        return len(stale)

    def __len__(self) -> int:
        return len(self._sessions)
