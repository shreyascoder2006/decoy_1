"""Logs must never contain credentials or raw audio.

Spec requirement 10.1: "No provider key or PCM data appears in logs."
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.live_call.audio_format import AudioContract
from app.live_call.resemble_client import MockDetectorClient
from app.logging_config import REDACTED, SecretRedactingFilter

SECRET = "rsmbl-live-0123456789abcdef"


def scrub(message: str, *args) -> str:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=message, args=args, exc_info=None,
    )
    SecretRedactingFilter((SECRET,)).filter(record)
    return record.getMessage()


def test_literal_key_is_redacted():
    out = scrub("connecting with key %s", SECRET)
    assert SECRET not in out
    assert REDACTED in out


def test_bearer_header_is_redacted():
    out = scrub(f"headers: Authorization: Bearer {SECRET}")
    assert SECRET not in out
    assert REDACTED in out


def test_bearer_pattern_is_redacted_even_for_an_unknown_key():
    out = scrub("sent Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out
    assert REDACTED in out


def test_api_key_assignment_is_redacted():
    out = scrub("config api_key=some-other-secret-value")
    assert "some-other-secret-value" not in out


def test_short_values_are_not_treated_as_secrets():
    # An 8-character minimum avoids redacting ordinary words.
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="state is ready", args=(), exc_info=None,
    )
    SecretRedactingFilter(("ready",)).filter(record)
    assert record.getMessage() == "state is ready"


def test_filter_never_raises_on_bad_args():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="missing %s %s", args=("only-one",), exc_info=None,
    )
    assert SecretRedactingFilter((SECRET,)).filter(record) is True


# --- call-site behaviour -------------------------------------------------


def test_provider_adapter_does_not_log_audio_or_keys(caplog):
    """Streaming a frame through the mock must not put PCM in the log."""
    caplog.set_level(logging.DEBUG)
    pcm = bytes(range(256)) * 12  # recognisable, non-silent byte pattern

    import asyncio

    async def stream():
        mock = MockDetectorClient(session_id="s", contract=AudioContract())
        await mock.connect()
        for _ in range(45):
            await mock.send_audio(pcm)
        await mock.send_end()
        await mock.close()

    asyncio.run(stream())

    text = caplog.text
    # No raw audio bytes, in any obvious encoding.
    assert pcm.hex() not in text
    assert repr(pcm)[:40] not in text
    assert "\\x00\\x01\\x02" not in text
    # The mock must announce itself loudly though.
    assert "MOCK" in text


def test_settings_repr_does_not_print_the_key():
    settings = Settings(_env_file=None, RESEMBLE_API_KEY=SECRET)  # type: ignore[call-arg]
    assert settings.resemble_api_key == SECRET
    # Guard against a stray f-string of the settings object reaching a log.
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="settings: %s", args=(repr(settings),), exc_info=None,
    )
    SecretRedactingFilter((settings.resemble_api_key,)).filter(record)
    assert SECRET not in record.getMessage()
