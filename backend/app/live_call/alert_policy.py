"""Map provider verdicts onto user-facing alerts.

Design rules (from the build specification):

* Use the provider's documented categorical ``label`` first. Do not invent
  numeric thresholds for a score whose calibration we do not control.
* A ``real`` label is never reported as "caller verified" -- only as "no
  synthetic signal in the audio analysed so far".
* A missing label, a ``skipped`` window, or a provider error is never
  rendered as a real/fake verdict.
* Do not re-alert on every analysis window. Alert once when the first
  synthetic-audio signal appears, and once more if the evidence strengthens.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.live_call.schemas import (
    AlertEvent,
    DetectorResultEvent,
    ErrorEvent,
    ProviderMessage,
    StatusEvent,
)

LABEL_FAKE = "fake"
LABEL_REAL = "real"

CODE_POTENTIAL = "POTENTIAL_SYNTHETIC_VOICE"
CODE_STRONGER = "SYNTHETIC_VOICE_EVIDENCE"
CODE_UNAVAILABLE = "DETECTOR_UNAVAILABLE"

TITLE_POTENTIAL = "Potential AI-generated voice detected"
TITLE_STRONGER = "Stronger synthetic-audio evidence"

MESSAGE_POTENTIAL = (
    "The analysed audio contains synthetic-speech signals. Independently "
    "verify the caller before trusting requests."
)
MESSAGE_STRONGER = (
    "Multiple analysed segments carry synthetic-speech signals. Treat this "
    "call as unverified and confirm the caller through another channel."
)

STATUS_NO_SIGNAL = "no_synthetic_signal"
STATUS_INCONCLUSIVE = "inconclusive"
STATUS_ANALYSING = "analysing"

DETAIL_NO_SIGNAL = "No synthetic signal detected in analysed audio so far."
DETAIL_INCONCLUSIVE = "Insufficient speech - continuing analysis."

Event = StatusEvent | DetectorResultEvent | AlertEvent | ErrorEvent


@dataclass
class PolicyOutcome:
    """Everything the policy wants to emit for one provider message."""

    events: list[Event] = field(default_factory=list)
    alert: AlertEvent | None = None
    detector_result: DetectorResultEvent | None = None
    terminal_error: ErrorEvent | None = None


class AlertPolicy:
    """Stateful per-session policy. Not thread-safe; one instance per session."""

    def __init__(self, session_id: str, provider: str) -> None:
        self.session_id = session_id
        self.provider = provider
        self.fake_window_count = 0
        self.real_window_count = 0
        self.skipped_window_count = 0
        self.alert_raised = False
        self.upgraded = False
        self.last_label: str | None = None

    # -- helpers ---------------------------------------------------------
    def _status(self, status: str, *, reason: str | None = None, detail: str | None = None) -> StatusEvent:
        return StatusEvent(
            status=status, session_id=self.session_id, reason=reason, detail=detail
        )

    def _detector_result(self, message: ProviderMessage) -> DetectorResultEvent:
        return DetectorResultEvent(
            session_id=self.session_id,
            label=message.label,
            aggregated_score=message.aggregated_score,
            consistency=message.consistency,
            window_start_s=message.window_start_s,
            window_end_s=message.window_end_s,
            chunk_label=message.chunk_label,
            provider=self.provider,
            provisional=not self.upgraded,
            result_kind="final" if message.is_final else "chunk",
        )

    # -- main entry point -------------------------------------------------
    def evaluate(self, message: ProviderMessage) -> PolicyOutcome:
        outcome = PolicyOutcome()

        # Provider-reported failure: never a verdict.
        if message.is_error:
            error = ErrorEvent(
                code=CODE_UNAVAILABLE,
                message="Streaming detection is unavailable.",
                session_id=self.session_id,
            )
            outcome.events.append(error)
            outcome.terminal_error = error
            return outcome

        if message.is_ready:
            outcome.events.append(self._status("ready"))
            return outcome

        if not (message.is_chunk or message.is_final):
            # Unknown frame type: ignore for forward compatibility.
            return outcome

        label = (message.label or "").lower() or None
        self.last_label = label

        result = self._detector_result(message)
        outcome.detector_result = result
        outcome.events.append(result)

        # Window carried too little voice activity to judge.
        if message.is_skipped or label is None:
            self.skipped_window_count += 1
            outcome.events.append(
                self._status(
                    STATUS_INCONCLUSIVE,
                    reason="insufficient_voice_activity",
                    detail=DETAIL_INCONCLUSIVE,
                )
            )
            return outcome

        if label == LABEL_FAKE:
            self.fake_window_count += 1
            if not self.alert_raised:
                self.alert_raised = True
                alert = AlertEvent(
                    level="high",
                    code=CODE_POTENTIAL,
                    title=TITLE_POTENTIAL,
                    message=MESSAGE_POTENTIAL,
                    provisional=True,
                    session_id=self.session_id,
                )
                outcome.alert = alert
                outcome.events.append(alert)
            elif not self.upgraded:
                self.upgraded = True
                alert = AlertEvent(
                    level="high",
                    code=CODE_STRONGER,
                    title=TITLE_STRONGER,
                    message=MESSAGE_STRONGER,
                    provisional=False,
                    session_id=self.session_id,
                )
                outcome.alert = alert
                outcome.events.append(alert)
            # Further fake windows update state silently -- no new notification.
            return outcome

        if label == LABEL_REAL:
            self.real_window_count += 1
            outcome.events.append(
                self._status(STATUS_NO_SIGNAL, detail=DETAIL_NO_SIGNAL)
            )
            return outcome

        # A label we do not recognise is treated as inconclusive, never as a
        # verdict in either direction.
        outcome.events.append(
            self._status(
                STATUS_INCONCLUSIVE,
                reason="unrecognised_label",
                detail=DETAIL_INCONCLUSIVE,
            )
        )
        return outcome
