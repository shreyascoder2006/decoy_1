"""Latency timestamps and counters for one live-call session.

Nothing here stores audio. Only durations, counts and provider metadata are
retained, which is what the session report needs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def _now() -> float:
    """Monotonic clock, so latency is unaffected by wall-clock adjustments."""
    return time.monotonic()


@dataclass
class SessionMetrics:
    """Counters and timestamps for a single live-call session."""

    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    first_speech_at: float | None = None
    first_audio_frame_at: float | None = None
    provider_ready_at: float | None = None
    first_verdict_at: float | None = None
    alert_at: float | None = None
    stopped_at: float | None = None

    frames_received: int = 0
    frames_dropped: int = 0
    frames_forwarded: int = 0
    bytes_received: int = 0
    audio_seconds_sent: float = 0.0
    verdicts_received: int = 0

    last_label: str | None = None
    last_aggregated_score: float | None = None
    last_consistency: float | None = None
    last_window_start_s: float | None = None
    last_window_end_s: float | None = None
    error: str | None = None

    # -- marks ------------------------------------------------------------
    def mark_started(self) -> None:
        if self.started_at is None:
            self.started_at = _now()

    def mark_first_speech(self) -> None:
        if self.first_speech_at is None:
            self.first_speech_at = _now()

    def mark_provider_ready(self) -> None:
        if self.provider_ready_at is None:
            self.provider_ready_at = _now()

    def mark_first_verdict(self) -> None:
        if self.first_verdict_at is None:
            self.first_verdict_at = _now()

    def mark_alert(self) -> None:
        if self.alert_at is None:
            self.alert_at = _now()

    def mark_stopped(self) -> None:
        if self.stopped_at is None:
            self.stopped_at = _now()

    def record_frame(self, byte_length: int, seconds: float) -> None:
        if self.first_audio_frame_at is None:
            self.first_audio_frame_at = _now()
        self.frames_received += 1
        self.bytes_received += byte_length
        self.audio_seconds_sent += seconds

    def record_forwarded(self) -> None:
        self.frames_forwarded += 1

    def record_dropped(self) -> None:
        self.frames_dropped += 1

    def record_verdict(
        self,
        *,
        label: str | None,
        aggregated_score: float | None,
        consistency: float | None,
        window_start_s: float | None,
        window_end_s: float | None,
    ) -> None:
        self.verdicts_received += 1
        self.mark_first_verdict()
        self.last_label = label
        self.last_aggregated_score = aggregated_score
        self.last_consistency = consistency
        self.last_window_start_s = window_start_s
        self.last_window_end_s = window_end_s

    # -- derived ----------------------------------------------------------
    @property
    def latency_origin(self) -> float | None:
        """The point from which detection latency should be measured.

        Preference order: first detected speech, then the first audio frame
        that actually reached the backend, then the user pressing Start. The
        specification is explicit that the clock should start at first speech
        rather than at call connection.
        """
        return self.first_speech_at or self.first_audio_frame_at or self.started_at

    def _since_origin(self, moment: float | None) -> float | None:
        origin = self.latency_origin
        if origin is None or moment is None:
            return None
        return round(max(moment - origin, 0.0), 3)

    @property
    def first_verdict_latency_seconds(self) -> float | None:
        return self._since_origin(self.first_verdict_at)

    @property
    def alert_latency_seconds(self) -> float | None:
        return self._since_origin(self.alert_at)

    def seconds_since_origin(self) -> float | None:
        """Seconds from the latency origin to now.

        Used to stamp each alert with its own latency. ``alert_latency_seconds``
        remains the time to the *first* alert, which is what the session report
        and the acceptance criteria ask for.
        """
        return self._since_origin(_now())

    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.stopped_at or _now()
        return round(max(end - self.started_at, 0.0), 3)

    def snapshot(self) -> dict[str, object]:
        """A log/report-safe summary. Contains no audio and no credentials."""
        return {
            "frames_received": self.frames_received,
            "frames_forwarded": self.frames_forwarded,
            "frames_dropped": self.frames_dropped,
            "audio_seconds_sent": round(self.audio_seconds_sent, 3),
            "verdicts_received": self.verdicts_received,
            "elapsed_seconds": self.elapsed_seconds,
            "first_verdict_latency_seconds": self.first_verdict_latency_seconds,
            "alert_latency_seconds": self.alert_latency_seconds,
            "last_label": self.last_label,
            "last_aggregated_score": self.last_aggregated_score,
            "last_consistency": self.last_consistency,
            "last_window_start_s": self.last_window_start_s,
            "last_window_end_s": self.last_window_end_s,
            "error": self.error,
        }
