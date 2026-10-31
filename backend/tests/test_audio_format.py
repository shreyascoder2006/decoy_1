"""Tests for the streaming WAV header and PCM frame helpers."""

from __future__ import annotations

import struct

import pytest

from app.live_call.audio_format import (
    UNKNOWN_LENGTH,
    WAV_HEADER_BYTES,
    AudioContract,
    is_plausible_pcm_frame,
    pcm16_sample_count,
    streaming_wav_header,
)


def test_header_is_44_bytes():
    assert len(streaming_wav_header()) == WAV_HEADER_BYTES


def test_header_matches_documented_unknown_length_format():
    header = streaming_wav_header(sample_rate=16000, channels=1)
    (
        riff,
        riff_size,
        wave,
        fmt,
        fmt_size,
        fmt_tag,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        data,
        data_size,
    ) = struct.unpack("<4sI4s4sIHHIIHH4sI", header)

    assert riff == b"RIFF"
    assert wave == b"WAVE"
    assert fmt == b"fmt "
    assert data == b"data"
    # Unknown total length is signalled with 0xFFFFFFFF in both size fields.
    assert riff_size == UNKNOWN_LENGTH == 0xFFFFFFFF
    assert data_size == UNKNOWN_LENGTH == 0xFFFFFFFF
    assert fmt_size == 16
    assert fmt_tag == 1  # WAVE_FORMAT_PCM
    assert channels == 1
    assert sample_rate == 16000
    assert bits == 16
    # Derived fields must be internally consistent.
    assert block_align == channels * bits // 8 == 2
    assert byte_rate == sample_rate * block_align == 32000


def test_header_derived_fields_for_stereo_48k():
    header = streaming_wav_header(sample_rate=48000, channels=2)
    _, _, _, _, _, _, channels, rate, byte_rate, block_align, bits, _, _ = struct.unpack(
        "<4sI4s4sIHHIIHH4sI", header
    )
    assert (channels, rate, bits) == (2, 48000, 16)
    assert block_align == 4
    assert byte_rate == 48000 * 4


@pytest.mark.parametrize("bad_rate", [0, -1])
def test_header_rejects_bad_sample_rate(bad_rate):
    with pytest.raises(ValueError):
        streaming_wav_header(sample_rate=bad_rate)


def test_header_rejects_zero_channels():
    with pytest.raises(ValueError):
        streaming_wav_header(channels=0)


def test_contract_frame_sizes():
    contract = AudioContract()
    # 100 ms at 16 kHz mono s16le == 1600 samples == 3200 bytes
    assert contract.bytes_per_frame == 3200
    assert contract.bytes_per_second == 32000
    assert contract.frame_duration_seconds(3200) == pytest.approx(0.1)
    assert contract.frame_duration_seconds(0) == 0.0


def test_frame_alignment_checks():
    assert is_plausible_pcm_frame(b"\x00\x01" * 1600)
    assert not is_plausible_pcm_frame(b"")
    # An odd byte count cannot be whole 16-bit samples.
    assert not is_plausible_pcm_frame(b"\x00\x01\x02")
    # Stereo requires 4-byte alignment.
    assert not is_plausible_pcm_frame(b"\x00\x01\x02\x03\x04\x05", channels=2)
    assert is_plausible_pcm_frame(b"\x00\x01\x02\x03", channels=2)


def test_sample_count():
    assert pcm16_sample_count(b"\x00\x01" * 1600) == 1600
    assert pcm16_sample_count(b"\x00\x01\x02\x03" * 10, channels=2) == 10
