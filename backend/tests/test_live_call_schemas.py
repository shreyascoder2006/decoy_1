"""Protocol model tests: provider frames and extension control frames."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.live_call.schemas import (
    CreateSessionRequest,
    ProviderMessage,
    StartFrame,
    StopFrame,
)


# --- provider frames ----------------------------------------------------


def test_ready_frame():
    message = ProviderMessage.model_validate({"type": "ready"})
    assert message.is_ready
    assert not message.is_chunk
    assert not message.is_error
    assert message.label is None


def test_chunk_frame_full_fields():
    message = ProviderMessage.model_validate(
        {
            "type": "chunk",
            "label": "fake",
            "aggregated_score": 0.91,
            "consistency": 92.0,
            "chunk_info": {
                "begin_timestamp_s": 0.0,
                "end_timestamp_s": 4.0,
                "chunk_label": "fake",
            },
        }
    )
    assert message.is_chunk
    assert message.label == "fake"
    assert message.aggregated_score == pytest.approx(0.91)
    assert message.consistency == pytest.approx(92.0)
    assert message.window_start_s == 0.0
    assert message.window_end_s == 4.0
    assert message.chunk_label == "fake"
    assert not message.is_skipped


def test_skipped_window():
    message = ProviderMessage.model_validate(
        {
            "type": "chunk",
            "label": None,
            "chunk_info": {
                "begin_timestamp_s": 0.0,
                "end_timestamp_s": 4.0,
                "chunk_label": "skipped",
            },
        }
    )
    assert message.is_skipped
    assert message.label is None


def test_skipped_detection_is_case_insensitive():
    message = ProviderMessage.model_validate(
        {"type": "chunk", "chunk_info": {"chunk_label": "SKIPPED"}}
    )
    assert message.is_skipped


def test_final_frame():
    message = ProviderMessage.model_validate(
        {"type": "final", "label": "real", "aggregated_score": 0.02}
    )
    assert message.is_final
    assert message.label == "real"


def test_unknown_fields_are_ignored_for_forward_compatibility():
    message = ProviderMessage.model_validate(
        {
            "type": "chunk",
            "label": "real",
            "some_future_field": {"nested": [1, 2, 3]},
            "another": "value",
            "chunk_info": {"chunk_label": "real", "future_window_field": 7},
        }
    )
    assert message.is_chunk
    assert message.label == "real"
    assert message.chunk_label == "real"


def test_error_field_variant():
    message = ProviderMessage.model_validate({"type": "error", "error": "bad audio"})
    assert message.is_error
    assert message.error_text == "bad audio"


def test_error_message_field_variant():
    message = ProviderMessage.model_validate({"error_message": "quota exceeded"})
    assert message.is_error
    assert message.error_text == "quota exceeded"


def test_error_message_wins_over_error():
    message = ProviderMessage.model_validate(
        {"type": "error", "error": "short", "error_message": "detailed"}
    )
    assert message.error_text == "detailed"


def test_missing_chunk_info_is_safe():
    message = ProviderMessage.model_validate({"type": "chunk", "label": "fake"})
    assert message.chunk_label is None
    assert message.window_start_s is None
    assert message.window_end_s is None
    assert not message.is_skipped


def test_parse_payload_tolerates_non_dict():
    assert ProviderMessage.parse_payload(["not", "a", "dict"]).type is None
    assert ProviderMessage.parse_payload("string").type is None
    assert ProviderMessage.parse_payload(None).type is None


# --- extension control frames -------------------------------------------


def test_start_frame_defaults():
    frame = StartFrame.model_validate({"type": "start"})
    assert (frame.sample_rate, frame.channels, frame.encoding, frame.frame_ms) == (
        16000,
        1,
        "pcm_s16le",
        100,
    )


def test_start_frame_rejects_wrong_rate():
    with pytest.raises(ValidationError):
        StartFrame.model_validate({"type": "start", "sample_rate": 48000})


def test_start_frame_rejects_stereo():
    with pytest.raises(ValidationError):
        StartFrame.model_validate({"type": "start", "channels": 2})


def test_start_frame_rejects_bad_encoding():
    with pytest.raises(ValidationError):
        StartFrame.model_validate({"type": "start", "encoding": "mp3"})


@pytest.mark.parametrize("frame_ms", [5, 2000])
def test_start_frame_rejects_absurd_frame_ms(frame_ms):
    with pytest.raises(ValidationError):
        StartFrame.model_validate({"type": "start", "frame_ms": frame_ms})


def test_stop_frame():
    assert StopFrame.model_validate({"type": "stop"}).type == "stop"


def test_create_session_request_rejects_unknown_source():
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate({"source": "microphone"})


def test_create_session_request_rejects_extra_keys():
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate(
            {"source": "whatsapp_web_tab", "secret": "x"}
        )
