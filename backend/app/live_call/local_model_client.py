"""Local open-source detector adapter.

Runs an audio anti-spoofing / deepfake-detection model in this process, so no
provider credential, no credits and no network are required at call time. It
implements the same :class:`DetectorClient` interface as the hosted provider,
so the alert policy, the metrics, the WebSocket protocol and the extension are
all unchanged.

Deliberate properties:

* **The label map comes from the model, never from an assumption.** Community
  models disagree on class order and some are mutually inverted; see
  :mod:`app.live_call.label_vocab`. A model whose labels are uninformative is
  refused at connect time rather than guessed at.
* **Silence is never labelled.** A window below the voice-activity floor is
  reported as ``skipped`` with a null label, exactly as the hosted provider
  reports a window it declined to analyse. Without this, near-silence tends to
  land on whichever class the model prefers and would fire a false alert.
* **Inference never blocks the event loop.** Windows go through a bounded
  queue to a worker task that runs the model in a thread.
* **torch and transformers are imported lazily**, so the backend and its test
  suite still run without them installed.

What this adapter does NOT do is tell you the model is accurate. Threshold and
model choice must be validated on your own samples; use
``scripts/detect_file.py`` for that.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass, field

from app.live_call.audio_format import AudioContract
from app.live_call.label_vocab import (
    LABEL_FAKE,
    LABEL_REAL,
    UnmappableLabels,
    build_label_map,
    parse_label_override,
)
from app.live_call.resemble_client import DetectorClient, DetectorUnavailable
from app.live_call.schemas import ProviderMessage

logger = logging.getLogger("live_call.local_model")

DEFAULT_MODEL_ID = "mo-thecreator/Deepfake-audio-detection"
DEFAULT_WINDOW_SECONDS = 4.0
DEFAULT_FAKE_THRESHOLD = 0.5
DEFAULT_MIN_RMS = 0.005
MAX_PENDING_WINDOWS = 4

_INT16_FULL_SCALE = 32768.0


# ---------------------------------------------------------------------------
# Model wrapper
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    """One window's probabilities, already normalised onto our vocabulary."""

    p_fake: float
    p_real: float
    raw: dict[str, float] = field(default_factory=dict)


class ModelEngine:
    """Wraps a transformers audio-classification model.

    Instances are cached per (model_id, device) so a second call does not
    reload hundreds of megabytes.
    """

    _cache: dict[tuple[str, str], "ModelEngine"] = {}

    def __init__(self, model_id: str, device: str, model, extractor, label_map: dict[int, str]) -> None:
        self.model_id = model_id
        self.device = device
        self._model = model
        self._extractor = extractor
        self.label_map = label_map

    # -- loading -----------------------------------------------------------
    @classmethod
    def load(
        cls,
        model_id: str,
        device: str = "cpu",
        label_override: str = "",
    ) -> "ModelEngine":
        key = (model_id, device)
        cached = cls._cache.get(key)
        if cached is not None:
            return cached

        try:
            import torch  # noqa: PLC0415 - deliberately lazy
            from transformers import (  # noqa: PLC0415
                AutoFeatureExtractor,
                AutoModelForAudioClassification,
            )
        except ImportError as exc:
            raise DetectorUnavailable(
                "the local detector needs torch and transformers: "
                "pip install -r requirements-local-model.txt"
            ) from exc

        started = time.monotonic()
        logger.info("loading local detector model %s on %s", model_id, device)
        try:
            extractor = AutoFeatureExtractor.from_pretrained(model_id)
            model = AutoModelForAudioClassification.from_pretrained(model_id)
        except Exception as exc:  # noqa: BLE001 - network, auth, disk, bad repo
            raise DetectorUnavailable(
                f"could not load local model {model_id!r}: {type(exc).__name__}"
            ) from exc

        model.eval()
        try:
            model.to(device)
        except Exception as exc:  # noqa: BLE001
            raise DetectorUnavailable(f"could not place the model on {device!r}") from exc

        # The label map is derived from the model, never assumed.
        config_labels = getattr(model.config, "id2label", None) or {}
        if label_override:
            label_map = parse_label_override(label_override)
            logger.warning(
                "using LIVE_CALL_LOCAL_LABEL_OVERRIDE=%r instead of the model's own labels",
                label_override,
            )
        else:
            try:
                label_map = build_label_map(config_labels)
            except UnmappableLabels as exc:
                raise DetectorUnavailable(f"unusable model labels: {exc}") from exc

        logger.info(
            "local detector ready in %.1fs (labels %s)",
            time.monotonic() - started,
            {int(k): v for k, v in sorted(label_map.items())},
        )

        engine = cls(model_id, device, model, extractor, label_map)
        cls._cache[key] = engine
        return engine

    # -- inference ---------------------------------------------------------
    def classify(self, samples: list[float]) -> ClassificationResult:
        """Classify one mono 16 kHz window. Runs synchronously; call in a thread."""
        import torch  # noqa: PLC0415

        sample_rate = int(getattr(self._extractor, "sampling_rate", 16000) or 16000)
        inputs = self._extractor(
            samples, sampling_rate=sample_rate, return_tensors="pt"
        )
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        with torch.no_grad():
            logits = self._model(**inputs).logits

        probabilities = torch.softmax(logits, dim=-1)[0].tolist()

        p_fake = 0.0
        p_real = 0.0
        raw: dict[str, float] = {}
        for index, probability in enumerate(probabilities):
            label = self.label_map.get(index)
            raw[str(label or index)] = float(probability)
            if label == LABEL_FAKE:
                p_fake += float(probability)
            elif label == LABEL_REAL:
                p_real += float(probability)

        return ClassificationResult(p_fake=p_fake, p_real=p_real, raw=raw)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def pcm16_rms(payload: bytes) -> float:
    """RMS of an s16le payload, normalised to [0, 1]."""
    count = len(payload) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", payload[: count * 2])
    total = 0.0
    for sample in samples:
        normalised = sample / _INT16_FULL_SCALE
        total += normalised * normalised
    return math.sqrt(total / count)


def pcm16_to_floats(payload: bytes) -> list[float]:
    count = len(payload) // 2
    if count == 0:
        return []
    samples = struct.unpack(f"<{count}h", payload[: count * 2])
    return [sample / _INT16_FULL_SCALE for sample in samples]


class LocalModelDetectorClient(DetectorClient):
    """Streaming detector backed by a locally executed model."""

    provider_name = "local-model"
    is_mock = False

    def __init__(
        self,
        *,
        session_id: str,
        contract: AudioContract,
        model_id: str = DEFAULT_MODEL_ID,
        device: str = "cpu",
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        fake_threshold: float = DEFAULT_FAKE_THRESHOLD,
        min_rms: float = DEFAULT_MIN_RMS,
        label_override: str = "",
        engine: ModelEngine | None = None,
    ) -> None:
        self._session_id = session_id
        self._contract = contract
        self._model_id = model_id
        self._device = device
        self._window_seconds = window_seconds
        self._fake_threshold = fake_threshold
        self._min_rms = min_rms
        self._label_override = label_override
        self._engine = engine

        self._window_bytes = int(
            window_seconds * contract.sample_rate * contract.channels * 2
        )
        self._buffer = bytearray()
        self._windows_seen = 0
        self._analysed = 0
        self._fake_windows = 0
        self._real_windows = 0
        self._skipped_windows = 0
        self._overload_drops = 0
        self._score_total = 0.0

        self._results: asyncio.Queue[ProviderMessage | None] = asyncio.Queue()
        self._pending: asyncio.Queue = asyncio.Queue(maxsize=MAX_PENDING_WINDOWS)
        self._worker: asyncio.Task | None = None
        self._closed = False

    @property
    def model_id(self) -> str:
        return self._model_id

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        """Load the model. Blocking work runs in a thread."""
        if self._engine is None:
            self._engine = await asyncio.to_thread(
                ModelEngine.load, self._model_id, self._device, self._label_override
            )
        logger.info(
            "local detector session %s ready (model=%s window=%.1fs threshold=%.2f)",
            self._session_id,
            self._model_id,
            self._window_seconds,
            self._fake_threshold,
        )
        self._worker = asyncio.create_task(
            self._run_worker(), name=f"local-detect-{self._session_id}"
        )

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed:
            raise DetectorUnavailable("the local detector is closed")
        self._buffer.extend(pcm)
        while len(self._buffer) >= self._window_bytes:
            window = bytes(self._buffer[: self._window_bytes])
            del self._buffer[: self._window_bytes]
            await self._enqueue_window(window)

    async def _enqueue_window(self, window: bytes) -> None:
        """Hand a window to the worker, applying backpressure rather than
        dropping on the first full queue.

        An earlier version used ``put_nowait`` and dropped on ``QueueFull``.
        That was wrong: a burst of audio fills the queue before the worker task
        has even been scheduled, so windows were discarded as "overloaded"
        while the model was in fact idle. Awaiting the put lets the worker
        drain, and pushes genuine congestion back up to the session manager,
        which already has one documented drop policy. Only a put that cannot
        complete within a whole window duration counts as real overload.
        """
        begin = self._windows_seen * self._window_seconds
        self._windows_seen += 1
        try:
            await asyncio.wait_for(
                self._pending.put((window, begin)),
                timeout=max(self._window_seconds, 1.0),
            )
        except asyncio.TimeoutError:
            # Inference really is slower than real time. Report the window as
            # unanalysed rather than growing a backlog or inventing a verdict.
            self._overload_drops += 1
            logger.warning(
                "local detector cannot keep up; window %d went unanalysed (%d total). "
                "Consider a smaller model or a longer window.",
                self._windows_seen,
                self._overload_drops,
            )
            await self._results.put(
                self._window_message(
                    label=None,
                    score=None,
                    begin=begin,
                    chunk_label="skipped",
                )
            )

    async def _run_worker(self) -> None:
        """Analyse queued windows one at a time.

        ``task_done`` is called for every item taken, so ``send_end`` can use
        ``Queue.join()`` to wait for the backlog to clear before summarising.
        """
        try:
            while True:
                item = await self._pending.get()
                try:
                    if item is None:
                        return
                    window, begin = item
                    try:
                        message = await self._analyse_window(window, begin)
                    except Exception:  # noqa: BLE001 - one bad window must not end the call
                        logger.exception("local inference failed for a window")
                        message = ProviderMessage.model_validate(
                            {"type": "error", "error_message": "local inference failed"}
                        )
                    await self._results.put(message)
                finally:
                    self._pending.task_done()
        except asyncio.CancelledError:
            raise

    async def _analyse_window(self, window: bytes, begin: float) -> ProviderMessage:
        # Voice-activity gate first: never label near-silence.
        rms = pcm16_rms(window)
        if rms < self._min_rms:
            self._skipped_windows += 1
            logger.debug("window at %.1fs skipped: rms %.5f below floor", begin, rms)
            return self._window_message(
                label=None, score=None, begin=begin, chunk_label="skipped"
            )

        assert self._engine is not None
        samples = pcm16_to_floats(window)
        result = await asyncio.to_thread(self._engine.classify, samples)

        label = LABEL_FAKE if result.p_fake >= self._fake_threshold else LABEL_REAL
        self._analysed += 1
        self._score_total += result.p_fake
        if label == LABEL_FAKE:
            self._fake_windows += 1
        else:
            self._real_windows += 1

        logger.info(
            "window %.1f-%.1fs -> %s (p_fake=%.3f rms=%.4f)",
            begin,
            begin + self._window_seconds,
            label,
            result.p_fake,
            rms,
        )
        return self._window_message(
            label=label, score=result.p_fake, begin=begin, chunk_label=label
        )

    def _consistency(self) -> float | None:
        """Share of analysed windows agreeing with the majority label.

        Locally computed, not a provider metric. 100 means every analysed
        window so far agreed.
        """
        if self._analysed == 0:
            return None
        majority = max(self._fake_windows, self._real_windows)
        return round(100.0 * majority / self._analysed, 1)

    def _window_message(
        self,
        *,
        label: str | None,
        score: float | None,
        begin: float,
        chunk_label: str | None,
    ) -> ProviderMessage:
        payload: dict = {
            "type": "chunk",
            "label": label,
            "chunk_info": {
                "begin_timestamp_s": round(begin, 3),
                "end_timestamp_s": round(begin + self._window_seconds, 3),
                "chunk_label": chunk_label,
            },
        }
        if score is not None:
            payload["aggregated_score"] = round(score, 4)
            payload["consistency"] = self._consistency()
        return ProviderMessage.model_validate(payload)

    async def send_end(self) -> None:
        """Flush, then emit an aggregate ``final`` result."""
        # Analyse a trailing partial window only if it is long enough to be
        # meaningful (at least half a window).
        if len(self._buffer) >= self._window_bytes // 2:
            await self._enqueue_window(bytes(self._buffer))
        self._buffer.clear()

        # Let the worker drain what is queued.
        try:
            await asyncio.wait_for(self._pending.join(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("timed out draining local detector windows")

        if self._analysed:
            mean_score = self._score_total / self._analysed
            # Majority vote over analysed windows, NOT the mean score against
            # the per-window threshold. That threshold is calibrated for a
            # single window; comparing a mean to it produced contradictory
            # summaries such as "6 of 8 windows fake ... final label: real"
            # (mean 0.8934 against a 0.9 threshold). A tie is reported as no
            # verdict rather than broken arbitrarily.
            if self._fake_windows > self._real_windows:
                label = LABEL_FAKE
            elif self._real_windows > self._fake_windows:
                label = LABEL_REAL
            else:
                label = None
            final = {
                "type": "final",
                "label": label,
                "aggregated_score": round(mean_score, 4),
                "consistency": self._consistency(),
            }
        else:
            # Nothing was analysable: report no verdict, not a guess.
            final = {"type": "final", "label": None}

        logger.info(
            "local detector session %s summary: analysed=%d fake=%d real=%d "
            "skipped=%d overload_drops=%d",
            self._session_id,
            self._analysed,
            self._fake_windows,
            self._real_windows,
            self._skipped_windows,
            self._overload_drops,
        )
        await self._results.put(ProviderMessage.model_validate(final))
        await self._results.put(None)

    async def receive(self) -> ProviderMessage | None:
        if self._closed and self._results.empty():
            return None
        return await self._results.get()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: B030
                pass
        await self._results.put(None)
