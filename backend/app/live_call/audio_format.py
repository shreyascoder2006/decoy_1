"""Audio format helpers for the live-call channel.

The browser extension is responsible for downmixing, resampling and PCM
conversion. The backend is responsible for the streaming WAV header that the
provider expects on the first binary frame, and for validating that incoming
frames look like the PCM contract we advertised.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# A live stream has no known total length. The documented convention for a
# streaming/unknown-length WAV is to write the maximum unsigned 32-bit value in
# both the RIFF chunk size and the data chunk size fields.
UNKNOWN_LENGTH = 0xFFFFFFFF

WAV_HEADER_BYTES = 44
PCM_FORMAT_TAG = 1  # WAVE_FORMAT_PCM
BITS_PER_SAMPLE = 16
BYTES_PER_SAMPLE = BITS_PER_SAMPLE // 8


@dataclass(frozen=True)
class AudioContract:
    """The wire audio format agreed between extension, backend and provider."""

    sample_rate: int = 16000
    channels: int = 1
    encoding: str = "pcm_s16le"
    frame_ms: int = 100

    @property
    def bytes_per_frame(self) -> int:
        """Expected byte length of one `frame_ms` chunk."""
        samples = self.sample_rate * self.frame_ms // 1000
        return samples * self.channels * BYTES_PER_SAMPLE

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * BYTES_PER_SAMPLE

    def frame_duration_seconds(self, byte_length: int) -> float:
        """How many seconds of audio a byte payload of this format represents."""
        if self.bytes_per_second <= 0:
            return 0.0
        return byte_length / self.bytes_per_second


DEFAULT_CONTRACT = AudioContract()


def streaming_wav_header(
    sample_rate: int = 16000,
    channels: int = 1,
    bits_per_sample: int = BITS_PER_SAMPLE,
) -> bytes:
    """Build a 44-byte WAV header for a stream of unknown total length.

    Both the RIFF size and the data size are set to 0xFFFFFFFF, which signals
    "length unknown" to a streaming consumer.
    """
    if channels < 1:
        raise ValueError("channels must be >= 1")
    if sample_rate < 1:
        raise ValueError("sample_rate must be >= 1")
    if bits_per_sample % 8 != 0:
        raise ValueError("bits_per_sample must be a multiple of 8")

    bytes_per_sample = bits_per_sample // 8
    block_align = channels * bytes_per_sample
    byte_rate = sample_rate * block_align

    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        UNKNOWN_LENGTH,
        b"WAVE",
        b"fmt ",
        16,  # size of the fmt sub-chunk for PCM
        PCM_FORMAT_TAG,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        UNKNOWN_LENGTH,
    )


def is_plausible_pcm_frame(payload: bytes, channels: int = 1) -> bool:
    """Cheap sanity check that a binary frame could be s16le PCM.

    Signed 16-bit frames must contain a whole number of samples for every
    channel. This intentionally does not inspect sample values -- we never want
    to log or analyse raw audio content here.
    """
    if not payload:
        return False
    alignment = BYTES_PER_SAMPLE * max(channels, 1)
    return len(payload) % alignment == 0


def pcm16_sample_count(payload: bytes, channels: int = 1) -> int:
    """Number of per-channel samples in an s16le payload."""
    alignment = BYTES_PER_SAMPLE * max(channels, 1)
    return len(payload) // alignment
