"""Alert policy tests.

The important properties are negative ones: a `real` label must never be
worded as verification, and a null/skipped/error result must never become a
verdict in either direction.
"""

from __future__ import annotations

from app.live_call.alert_policy import (
    CODE_POTENTIAL,
    CODE_STRONGER,
    CODE_UNAVAILABLE,
    STATUS_INCONCLUSIVE,
    STATUS_NO_SIGNAL,
    AlertPolicy,
)
from app.live_call.schemas import AlertEvent, DetectorResultEvent, StatusEvent
from app.live_call.schemas import ProviderMessage


def chunk(label, chunk_label=None, score=None, begin=0.0, end=4.0):
    return ProviderMessage.model_validate(
        {
            "type": "chunk",
            "label": label,
            "aggregated_score": score,
            "chunk_info": {
                "begin_timestamp_s": begin,
                "end_timestamp_s": end,
                "chunk_label": chunk_label if chunk_label is not None else label,
            },
        }
    )


def policy():
    return AlertPolicy("session-1", "resemble")


def test_ready_emits_status_only():
    outcome = policy().evaluate(ProviderMessage.model_validate({"type": "ready"}))
    assert outcome.alert is None
    assert outcome.detector_result is None
    assert [type(e) for e in outcome.events] == [StatusEvent]
    assert outcome.events[0].status == "ready"


def test_first_fake_chunk_raises_provisional_alert():
    p = policy()
    outcome = p.evaluate(chunk("fake", score=0.91))

    assert outcome.alert is not None
    assert outcome.alert.code == CODE_POTENTIAL
    assert outcome.alert.level == "high"
    assert outcome.alert.provisional is True
    assert p.alert_raised is True
    assert p.fake_window_count == 1

    assert outcome.detector_result is not None
    assert outcome.detector_result.label == "fake"
    assert outcome.detector_result.aggregated_score == 0.91


def test_alert_wording_is_hedged_not_definitive():
    outcome = policy().evaluate(chunk("fake"))
    title = outcome.alert.title.lower()
    body = outcome.alert.message.lower()
    assert "potential" in title
    # Must not assert the caller *is* an AI or is malicious.
    for forbidden in ("definitely", "confirmed ai", "is an ai", "scammer", "fraudster"):
        assert forbidden not in title
        assert forbidden not in body
    assert "verify" in body


def test_the_alert_upgrades_once_then_goes_quiet():
    """With min_fake_windows=1: warn on window 1, upgrade at a run of 3.

    The gap between warning and upgrade is deliberate -- the upgrade claims
    stronger evidence, so it should need more than one extra window.
    """
    p = policy()
    results = [
        p.evaluate(chunk("fake", begin=i * 4.0, end=(i + 1) * 4.0)) for i in range(5)
    ]

    assert results[0].alert.code == CODE_POTENTIAL
    assert results[0].alert.provisional is True

    # Window 2 adds evidence but does not yet justify the stronger claim.
    assert results[1].alert is None

    assert results[2].alert is not None
    assert results[2].alert.code == CODE_STRONGER
    assert results[2].alert.provisional is False

    # No repeated notification for every subsequent window.
    assert results[3].alert is None
    assert results[4].alert is None
    # State still advances silently.
    assert p.fake_window_count == 5
    assert p.consecutive_fake_windows == 5


def test_real_label_is_not_worded_as_verification():
    p = policy()
    outcome = p.evaluate(chunk("real", score=0.03))

    assert outcome.alert is None
    statuses = [e for e in outcome.events if isinstance(e, StatusEvent)]
    assert len(statuses) == 1
    status = statuses[0]
    assert status.status == STATUS_NO_SIGNAL
    detail = (status.detail or "").lower()
    assert "no synthetic signal detected in analysed audio so far" in detail
    for forbidden in ("authentic", "verified", "genuine caller", "safe"):
        assert forbidden not in detail
    assert p.real_window_count == 1


def test_null_label_is_inconclusive():
    p = policy()
    outcome = p.evaluate(chunk(None, chunk_label=None))

    assert outcome.alert is None
    status = [e for e in outcome.events if isinstance(e, StatusEvent)][0]
    assert status.status == STATUS_INCONCLUSIVE
    assert status.reason == "insufficient_voice_activity"
    assert p.skipped_window_count == 1


def test_skipped_window_is_inconclusive_even_with_a_label():
    p = policy()
    outcome = p.evaluate(chunk("fake", chunk_label="skipped"))

    # A skipped window must not raise an alert, whatever label rides along.
    assert outcome.alert is None
    assert p.fake_window_count == 0
    status = [e for e in outcome.events if isinstance(e, StatusEvent)][0]
    assert status.status == STATUS_INCONCLUSIVE


def test_unrecognised_label_is_inconclusive_not_a_verdict():
    p = policy()
    outcome = p.evaluate(chunk("maybe-synthetic"))
    assert outcome.alert is None
    status = [e for e in outcome.events if isinstance(e, StatusEvent)][0]
    assert status.status == STATUS_INCONCLUSIVE
    assert status.reason == "unrecognised_label"


def test_provider_error_is_unavailable_not_a_verdict():
    p = policy()
    outcome = p.evaluate(
        ProviderMessage.model_validate({"type": "error", "error_message": "no credits"})
    )
    assert outcome.terminal_error is not None
    assert outcome.terminal_error.code == CODE_UNAVAILABLE
    assert outcome.alert is None
    assert outcome.detector_result is None
    # The user-facing text must not contain a real/fake claim.
    text = outcome.terminal_error.message.lower()
    assert "unavailable" in text
    assert "fake" not in text and "real" not in text
    # The provider's internal reason is not forwarded to the browser.
    assert "credits" not in text


def test_provider_error_without_type_still_detected():
    outcome = policy().evaluate(
        ProviderMessage.model_validate({"error": "upstream failure"})
    )
    assert outcome.terminal_error is not None


def test_final_fake_after_real_chunks_still_alerts():
    p = policy()
    p.evaluate(chunk("real"))
    outcome = p.evaluate(
        ProviderMessage.model_validate({"type": "final", "label": "fake"})
    )
    assert outcome.alert is not None
    assert outcome.alert.code == CODE_POTENTIAL
    assert outcome.detector_result.result_kind == "final"


def test_unknown_frame_type_produces_nothing():
    outcome = policy().evaluate(
        ProviderMessage.model_validate({"type": "heartbeat", "label": "fake"})
    )
    assert outcome.events == []
    assert outcome.alert is None
    assert outcome.detector_result is None


def test_detector_result_carries_window_timestamps_and_provider():
    outcome = policy().evaluate(chunk("fake", begin=4.0, end=8.0))
    result = [e for e in outcome.events if isinstance(e, DetectorResultEvent)][0]
    assert result.window_start_s == 4.0
    assert result.window_end_s == 8.0
    assert result.provider == "resemble"


def test_mixed_sequence_alerts_once_on_first_fake():
    p = policy()
    sequence = [
        chunk(None, chunk_label="skipped"),
        chunk("real"),
        chunk("fake"),
        chunk("real"),
    ]
    alerts = [e for m in sequence for e in p.evaluate(m).events if isinstance(e, AlertEvent)]
    assert len(alerts) == 1
    assert alerts[0].code == CODE_POTENTIAL


# --- run-length requirement ----------------------------------------------


def test_default_policy_alerts_on_the_first_fake_window():
    """min_fake_windows=1 preserves the original behaviour."""
    p = AlertPolicy("s", "resemble", min_fake_windows=1)
    assert p.evaluate(chunk("fake")).alert is not None


def test_two_window_policy_waits_for_a_run():
    p = AlertPolicy("s", "local-model", min_fake_windows=2)
    first = p.evaluate(chunk("fake", begin=0.0, end=4.0))
    assert first.alert is None
    assert p.consecutive_fake_windows == 1
    statuses = [e for e in first.events if isinstance(e, StatusEvent)]
    assert statuses and statuses[0].reason == "building_evidence"

    second = p.evaluate(chunk("fake", begin=4.0, end=8.0))
    assert second.alert is not None
    assert second.alert.code == CODE_POTENTIAL


def test_a_clean_window_breaks_the_run():
    p = AlertPolicy("s", "local-model", min_fake_windows=2)
    p.evaluate(chunk("fake"))
    p.evaluate(chunk("real"))
    assert p.consecutive_fake_windows == 0
    # A later isolated fake must not combine with the earlier one.
    assert p.evaluate(chunk("fake")).alert is None
    assert p.alert_raised is False


def test_skipped_windows_do_not_break_the_run():
    """A pause in speech is not evidence that the voice became genuine."""
    p = AlertPolicy("s", "local-model", min_fake_windows=2)
    p.evaluate(chunk("fake"))
    p.evaluate(chunk(None, chunk_label="skipped"))
    assert p.consecutive_fake_windows == 1
    assert p.evaluate(chunk("fake")).alert is not None


def test_upgrade_requires_a_longer_run():
    p = AlertPolicy("s", "local-model", min_fake_windows=2)
    assert p.evaluate(chunk("fake")).alert is None
    assert p.evaluate(chunk("fake")).alert.code == CODE_POTENTIAL
    assert p.evaluate(chunk("fake")).alert is None
    upgrade = p.evaluate(chunk("fake"))
    assert upgrade.alert is not None
    assert upgrade.alert.code == CODE_STRONGER
    assert upgrade.alert.provisional is False


def test_min_fake_windows_is_floored_at_one():
    for bad in (0, -5):
        assert AlertPolicy("s", "p", min_fake_windows=bad).min_fake_windows == 1
