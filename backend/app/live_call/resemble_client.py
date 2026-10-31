"""Server-side streaming detection adapters.

Two implementations share one interface:

* :class:`ResembleStreamingClient` -- the real provider. It is the only place
  the API key is used, and it is only ever constructed inside the backend
  process. The key is never serialised into an event, a log line or a URL path.
* :class:`MockDetectorClient` -- a deterministic stand-in used to exercise the
  transport when no provider credential or entitlement is available. Every
  event it produces is tagged ``provider="mock"`` so that a mock verdict can
  never be mistaken for a real one.

A missing API key does not silently select the mock. Selecting the mock is an
explicit configuration choice (``LIVE_CALL_MOCK_MODE=true`` or
``LIVE_CALL_PROVIDER=mock``).
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import urllib.parse
from typing import Any

import aiohttp

from app.live_call.audio_format import AudioContract, streaming_wav_header
from app.live_call.schemas import ProviderMessage

logger = logging.getLogger("live_call.provider")


class DetectorUnavailable(RuntimeError):
    """Raised when the provider stream cannot be established or sustained."""


class DetectorClient(abc.ABC):
    """Minimal streaming-detector interface used by the session manager."""

    provider_name: str = "unknown"
    is_mock: bool = False

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the upstream stream and block until it is ready for audio."""

    @abc.abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Forward one raw PCM frame upstream."""

    @abc.abstractmethod
    async def send_end(self) -> None:
        """Signal end of audio without closing the socket."""

    @abc.abstractmethod
    async def receive(self) -> ProviderMessage | None:
        """Return the next provider message, or None when the stream ends."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release all upstream resources. Must be safe to call repeatedly."""


# ---------------------------------------------------------------------------
# Real provider
# ---------------------------------------------------------------------------


class ResembleStreamingClient(DetectorClient):
    """WebSocket client for Resemble's streaming audio detection endpoint."""

    provider_name = "resemble"
    is_mock = False

    def __init__(
        self,
        *,
        api_key: str,
        session_id: str,
        endpoint: str,
        contract: AudioContract,
        connect_timeout: float = 10.0,
        ready_timeout: float = 15.0,
        final_timeout: float = 10.0,
    ) -> None:
        if not api_key:
            raise DetectorUnavailable("provider API key is not configured")
        self._api_key = api_key
        self._session_id = session_id
        self._endpoint = endpoint
        self._contract = contract
        self._connect_timeout = connect_timeout
        self._ready_timeout = ready_timeout
        self._final_timeout = final_timeout

        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._header_sent = False
        self._closed = False
        self._ready = False

    # -- URL ---------------------------------------------------------------
    def _upstream_url(self) -> str:
        """Append ``extra_params`` carrying our own session id, JSON encoded."""
        extra = json.dumps({"session_id": self._session_id}, separators=(",", ":"))
        query = urllib.parse.urlencode({"extra_params": extra})
        separator = "&" if "?" in self._endpoint else "?"
        return f"{self._endpoint}{separator}{query}"

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        url = self._upstream_url()
        # Logged without the Authorization header and without the key.
        logger.info("opening provider stream host=%s", urllib.parse.urlparse(url).netloc)

        self._http = aiohttp.ClientSession()
        try:
            self._ws = await asyncio.wait_for(
                self._http.ws_connect(
                    url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    heartbeat=20.0,
                    max_msg_size=0,
                ),
                timeout=self._connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            await self.close()
            raise DetectorUnavailable("timed out connecting to the provider") from exc
        except aiohttp.ClientError as exc:
            await self.close()
            # str(exc) for an auth failure contains a status code, not the key.
            raise DetectorUnavailable(f"provider connection refused: {exc}") from exc

        await self._await_ready()

    async def _await_ready(self) -> None:
        """Block until the provider says it will accept audio."""
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self.close()
                raise DetectorUnavailable("provider did not become ready in time")
            try:
                message = await asyncio.wait_for(self.receive(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                await self.close()
                raise DetectorUnavailable("provider did not become ready in time") from exc

            if message is None:
                await self.close()
                raise DetectorUnavailable("provider closed the stream before ready")
            if message.is_error:
                await self.close()
                raise DetectorUnavailable(
                    f"provider reported an error: {message.error_text}"
                )
            if message.is_ready:
                self._ready = True
                logger.info("provider stream ready")
                return
            # Any other frame before `ready` is ignored.

    # -- send --------------------------------------------------------------
    async def send_audio(self, pcm: bytes) -> None:
        if self._ws is None or self._ws.closed:
            raise DetectorUnavailable("provider stream is not open")
        if not pcm:
            return
        if not self._header_sent:
            payload = (
                streaming_wav_header(
                    sample_rate=self._contract.sample_rate,
                    channels=self._contract.channels,
                )
                + pcm
            )
            self._header_sent = True
        else:
            payload = pcm
        try:
            await self._ws.send_bytes(payload)
        except (aiohttp.ClientError, ConnectionResetError) as exc:
            raise DetectorUnavailable(f"provider stream write failed: {exc}") from exc

    async def send_end(self) -> None:
        if self._ws is None or self._ws.closed:
            return
        try:
            await self._ws.send_str(json.dumps({"type": "end"}))
        except (aiohttp.ClientError, ConnectionResetError):
            # Nothing useful to do; the drain loop will observe the closure.
            logger.debug("provider stream already closed when sending end marker")

    # -- receive -----------------------------------------------------------
    async def receive(self) -> ProviderMessage | None:
        if self._ws is None:
            return None
        try:
            message = await self._ws.receive()
        except (aiohttp.ClientError, ConnectionResetError) as exc:
            logger.warning("provider stream read failed: %s", exc)
            return None

        if message.type is aiohttp.WSMsgType.TEXT:
            return _decode_text_frame(message.data)
        if message.type is aiohttp.WSMsgType.BINARY:
            # The detector is not expected to send binary; ignore it.
            return ProviderMessage(type=None)
        if message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        ):
            return None
        if message.type is aiohttp.WSMsgType.ERROR:
            logger.warning("provider stream signalled an error frame")
            return None
        return ProviderMessage(type=None)

    async def drain_until_final(self) -> list[ProviderMessage]:
        """After ``send_end``, collect frames until ``final`` or a timeout.

        The provider is not closed immediately after the end marker, because
        the final aggregate result arrives after it.
        """
        collected: list[ProviderMessage] = []
        deadline = asyncio.get_running_loop().time() + self._final_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.info("timed out waiting for the provider final result")
                return collected
            try:
                message = await asyncio.wait_for(self.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.info("timed out waiting for the provider final result")
                return collected
            if message is None:
                return collected
            collected.append(message)
            if message.is_final or message.is_error:
                return collected

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                logger.debug("ignoring error while closing provider socket", exc_info=True)
        if self._http is not None and not self._http.closed:
            try:
                await self._http.close()
            except Exception:  # noqa: BLE001
                logger.debug("ignoring error while closing provider http session", exc_info=True)
        self._ws = None
        self._http = None


def _decode_text_frame(raw: str) -> ProviderMessage:
    """Parse a provider text frame, tolerating malformed JSON."""
    try:
        payload: Any = json.loads(raw)
    except (TypeError, ValueError):
        # Length only -- never the body, which could contain transcript text.
        logger.warning("discarding unparseable provider frame (%d bytes)", len(raw or ""))
        return ProviderMessage(type=None)
    return ProviderMessage.parse_payload(payload)


# ---------------------------------------------------------------------------
# Mock provider
# ---------------------------------------------------------------------------

DEFAULT_MOCK_LABELS = ("skipped", "fake", "fake")
MOCK_WINDOW_SECONDS = 4.0


class MockDetectorClient(DetectorClient):
    """Deterministic detector used to exercise the transport only.

    It consumes real audio byte counts so that window timing resembles the real
    provider, but its labels come from a configured script. It performs no
    audio analysis whatsoever and its verdicts carry no evidential value.
    """

    provider_name = "mock"
    is_mock = True

    def __init__(
        self,
        *,
        session_id: str,
        contract: AudioContract,
        labels: tuple[str, ...] = DEFAULT_MOCK_LABELS,
        window_seconds: float = MOCK_WINDOW_SECONDS,
    ) -> None:
        self._session_id = session_id
        self._contract = contract
        self._labels = labels or DEFAULT_MOCK_LABELS
        self._window_seconds = window_seconds
        self._queue: asyncio.Queue[ProviderMessage | None] = asyncio.Queue()
        self._seconds_buffered = 0.0
        self._windows_emitted = 0
        self._closed = False

    async def connect(self) -> None:
        logger.warning(
            "MOCK detector selected: verdicts are scripted and carry no evidential value"
        )
        await self._queue.put(ProviderMessage(type="ready"))

    def _next_label(self) -> str:
        index = min(self._windows_emitted, len(self._labels) - 1)
        return self._labels[index]

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            raise DetectorUnavailable("mock detector is closed")
        self._seconds_buffered += self._contract.frame_duration_seconds(len(pcm))
        while self._seconds_buffered >= self._window_seconds:
            self._seconds_buffered -= self._window_seconds
            await self._emit_window()

    async def _emit_window(self) -> None:
        label = self._next_label()
        begin = self._windows_emitted * self._window_seconds
        end = begin + self._window_seconds
        self._windows_emitted += 1

        if label == "skipped":
            message = ProviderMessage.model_validate(
                {
                    "type": "chunk",
                    "label": None,
                    "chunk_info": {
                        "begin_timestamp_s": begin,
                        "end_timestamp_s": end,
                        "chunk_label": "skipped",
                    },
                }
            )
        else:
            message = ProviderMessage.model_validate(
                {
                    "type": "chunk",
                    "label": label,
                    "aggregated_score": 0.91 if label == "fake" else 0.07,
                    "consistency": 92.0,
                    "chunk_info": {
                        "begin_timestamp_s": begin,
                        "end_timestamp_s": end,
                        "chunk_label": label,
                    },
                }
            )
        await self._queue.put(message)

    async def send_end(self) -> None:
        label = "fake" if self._windows_emitted and self._next_label() == "fake" else "real"
        await self._queue.put(
            ProviderMessage.model_validate(
                {
                    "type": "final",
                    "label": label if self._windows_emitted else None,
                    "aggregated_score": 0.91 if label == "fake" else 0.07,
                    "consistency": 90.0,
                }
            )
        )
        await self._queue.put(None)

    async def receive(self) -> ProviderMessage | None:
        if self._closed and self._queue.empty():
            return None
        return await self._queue.get()

    async def drain_until_final(self) -> list[ProviderMessage]:
        collected: list[ProviderMessage] = []
        while True:
            try:
                message = await asyncio.wait_for(self.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                return collected
            if message is None:
                return collected
            collected.append(message)
            if message.is_final or message.is_error:
                return collected

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)
