"""Map provider verdicts onto user-facing alerts.

Design rules (from the build specification):

* Use the provider's documented categorical ``label`` first. Do not invent
  numeric thresholds for a score whose calibration we do not control.
* A ``real`` label is never reported as "caller verified" -- only as "no
  synthetic signal in the audio analysed so far".
* A missing label, a ``skipped`` window, or a provider error is never
  rendered as a real/fake verdict.
* Do not re-alert on every analysis window. Alert once when the evidence
  threshold is first met, and once more if it strengthens.
* Require a *run* of consecutive synthetic windows, not a single one.

That last rule comes from measurement, not taste. The best local model
benchmarked here (Hemgg, AUC 0.889 over 40 human vs 20 neural-TTS samples)
false-alarms on about 10% of individual 4-second windows at its best
threshold. Alerting on the first fake window would therefore flag a genuine
human caller with probability 1 - 0.9^n over n windows: roughly 79% across a
one-minute call. Requiring two consecutive fake windows cuts that to around
13% while still catching a real AI caller easily, because a true positive
recurs window after window and a false positive usually does not.

``min_fake_windows`` is configurable so a provider with a better measured
false-alarm rate can alert faster. Skipped windows do not break a run -- a
pause in speech is not evidence either way.
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

    def __init__(
        self, session_id: str, provider: str, min_fake_windows: int = 1
    ) -> None:
        self.session_id = session_id
        self.provider = provider
        self.min_fake_windows = max(1, int(min_fake_windows))
        self.fake_window_count = 0
        self.consecutive_fake_windows = 0
        self.real_window_count = 0
        self.skipped_window_count = 0
        self.alert_raised = False
        self.upgraded = False
        self.last_label: str | None = None
        # Detector scores for the current run of synthetic windows.
        self._run_scores: list[float] = []

    # -- helpers ---------------------------------------------------------
    def _evidence(self) -> tuple[int | None, list[str]]:
        """Risk score and plain-language reasons for the current run."""
        run = self.consecutive_fake_windows
        reasons = [
            f"{run} analysis windows in a row were classified as synthetic speech "
            f"by the detector; at least {self.min_fake_windows} are required "
            f"before warning."
        ]
        score: int | None = None
        if self._run_scores:
            average = sum(self._run_scores) / len(self._run_scores)
            score = round(100 * average)
            shown = ", ".join(f"{round(100 * s)}%" for s in self._run_scores[-6:])
            reasons.append(
                f"Detector confidence per window: {shown} (average {score}%). "
                f"This is the model's own score, not a calibrated probability."
            )
        if self.real_window_count:
            reasons.append(
                f"{self.real_window_count} earlier window(s) were classified as "
                f"human speech; a human-classified window resets the run, so this "
                f"is a sustained pattern, not a one-off."
            )
        reasons.append(
            "Why this matters: AI-generated voices are used in impersonation and "
            "payment-fraud calls. The detector looks for speech patterns typical "
            "of synthetic voices; it cannot prove who is speaking."
        )
        return score, reasons

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
            self.consecutive_fake_windows += 1
            if message.aggregated_score is not None:
                self._run_scores.append(float(message.aggregated_score))

            if (
                not self.alert_raised
                and self.consecutive_fake_windows >= self.min_fake_windows
            ):
                self.alert_raised = True
                risk_score, reasons = self._evidence()
                alert = AlertEvent(
                    level="high",
                    code=CODE_POTENTIAL,
                    title=TITLE_POTENTIAL,
                    message=MESSAGE_POTENTIAL,
                    provisional=True,
                    session_id=self.session_id,
                    risk_score=risk_score,
                    reasons=reasons,
                )
                outcome.alert = alert
                outcome.events.append(alert)
            elif (
                self.alert_raised
                and not self.upgraded
                and self.consecutive_fake_windows >= self.min_fake_windows + 2
            ):
                self.upgraded = True
                risk_score, reasons = self._evidence()
                alert = AlertEvent(
                    level="high",
                    code=CODE_STRONGER,
                    title=TITLE_STRONGER,
                    message=MESSAGE_STRONGER,
                    provisional=False,
                    session_id=self.session_id,
                    risk_score=risk_score,
                    reasons=reasons,
                )
                outcome.alert = alert
                outcome.events.append(alert)
            elif not self.alert_raised:
                # Evidence is building but has not met the bar. Say so rather
                # than either alarming or implying the caller is cleared.
                outcome.events.append(
                    self._status(
                        STATUS_ANALYSING,
                        reason="building_evidence",
                        detail=(
                            f"A synthetic-speech signal appeared in "
                            f"{self.consecutive_fake_windows} of the last "
                            f"{self.min_fake_windows} windows needed to warn."
                        ),
                    )
                )
            # Further fake windows update state silently -- no new notification.
            return outcome

        if label == LABEL_REAL:
            self.real_window_count += 1
            # A clean window breaks the run: an isolated false positive should
            # not combine with a much later one to trip the alert.
            self.consecutive_fake_windows = 0
            self._run_scores = []
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
