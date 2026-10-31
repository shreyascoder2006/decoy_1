"""Local detector adapter tests.

A stub engine stands in for the model so these run fast and without torch.
They pin the behaviour that protects the user from a false alert: silence is
never labelled, the threshold is honoured, and an inference failure degrades
to "unavailable" rather than to a verdict.
"""

from __future__ import annotations

import asyncio
import math
import struct

import pytest

from app.live_call.audio_format import AudioContract
from app.live_call.label_vocab import LABEL_FAKE, LABEL_REAL
from app.live_call.local_model_client import (
    ClassificationResult,
    LocalModelDetectorClient,
    ModelEngine,
    pcm16_rms,
    pcm16_to_floats,
)
from app.live_call.resemble_client import DetectorUnavailable

CONTRACT = AudioContract()
SECOND_BYTES = 16000 * 2


def tone_pcm(seconds: float, amplitude: float = 0.4, freq: float = 200.0) -> bytes:
    count = int(16000 * seconds)
    values = []
    for n in range(count):
        v = amplitude * math.sin(2 * math.pi * freq * n / 16000)
        values.append(int(max(-1.0, min(1.0, v)) * 32767))
    return struct.pack(f"<{count}h", *values)


def silence_pcm(seconds: float) -> bytes:
    return b"\x00\x00" * int(16000 * seconds)


class StubEngine:
    """Returns a scripted p_fake for every window."""

    def __init__(self, scores, label_map=None):
        self._scores = list(scores)
        self.label_map = label_map or {0: LABEL_FAKE, 1: LABEL_REAL}
        self.calls = 0
        self.window_lengths: list[int] = []

    def classify(self, samples):
        self.calls += 1
        self.window_lengths.append(len(samples))
        score = self._scores[min(self.calls - 1, len(self._scores) - 1)]
        if isinstance(score, Exception):
            raise score
        return ClassificationResult(p_fake=score, p_real=1.0 - score)


def build(engine, **overrides):
    kwargs = dict(
        session_id="s1",
        contract=CONTRACT,
        window_seconds=4.0,
        fake_threshold=0.5,
        min_rms=0.005,
        engine=engine,
    )
    kwargs.update(overrides)
    return LocalModelDetectorClient(**kwargs)


async def collect(client, limit=20, timeout=5.0):
    """Drain available result messages."""
    out = []
    for _ in range(limit):
        try:
            message = await asyncio.wait_for(client.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if message is None:
            break
        out.append(message)
    return out


# --- helpers -------------------------------------------------------------


def test_rms_of_silence_and_full_scale():
    assert pcm16_rms(silence_pcm(0.1)) == 0.0
    full = struct.pack("<4h", 32767, -32767, 32767, -32767)
    assert pcm16_rms(full) == pytest.approx(1.0, abs=1e-3)
    assert pcm16_rms(b"") == 0.0


def test_pcm_to_floats_round_trip():
    floats = pcm16_to_floats(struct.pack("<3h", 0, 16384, -16384))
    assert floats[0] == 0.0
    assert floats[1] == pytest.approx(0.5, abs=1e-3)
    assert floats[2] == pytest.approx(-0.5, abs=1e-3)


# --- windowing -----------------------------------------------------------


async def test_a_window_is_emitted_every_four_seconds():
    engine = StubEngine([0.9])
    client = build(engine)
    await client.connect()

    # 3 s is not yet a window.
    await client.send_audio(tone_pcm(3.0))
    assert engine.calls == 0

    await client.send_audio(tone_pcm(1.0))
    messages = await collect(client, limit=1)
    assert len(messages) == 1
    assert engine.calls == 1
    # The model saw exactly 4 s of 16 kHz samples.
    assert engine.window_lengths == [64000]

    message = messages[0]
    assert message.is_chunk
    assert message.window_start_s == 0.0
    assert message.window_end_s == 4.0
    await client.close()


async def test_window_timestamps_advance():
    client = build(StubEngine([0.9, 0.9, 0.9]))
    await client.connect()
    await client.send_audio(tone_pcm(12.0))
    messages = await collect(client, limit=3)
    assert [(m.window_start_s, m.window_end_s) for m in messages] == [
        (0.0, 4.0),
        (4.0, 8.0),
        (8.0, 12.0),
    ]
    await client.close()


async def test_frames_spanning_a_window_boundary_are_handled():
    engine = StubEngine([0.9, 0.9])
    client = build(engine)
    await client.connect()
    # 100 ms frames, as the extension actually sends them.
    for _ in range(80):
        await client.send_audio(tone_pcm(0.1))
    messages = await collect(client, limit=2)
    assert len(messages) == 2
    assert engine.window_lengths == [64000, 64000]
    await client.close()


# --- the honesty-critical behaviours ------------------------------------


async def test_silence_is_skipped_and_never_labelled():
    """Without this, near-silence lands on whichever class the model prefers."""
    engine = StubEngine([0.99])  # would say "fake" if it were ever consulted
    client = build(engine)
    await client.connect()
    await client.send_audio(silence_pcm(4.0))

    messages = await collect(client, limit=1)
    assert len(messages) == 1
    assert engine.calls == 0, "the model must not be run on silence"
    assert messages[0].label is None
    assert messages[0].is_skipped
    assert messages[0].aggregated_score is None
    await client.close()


async def test_very_quiet_audio_is_skipped():
    engine = StubEngine([0.99])
    client = build(engine, min_rms=0.05)
    await client.connect()
    await client.send_audio(tone_pcm(4.0, amplitude=0.001))
    messages = await collect(client, limit=1)
    assert messages[0].is_skipped
    assert engine.calls == 0
    await client.close()


@pytest.mark.parametrize(
    "score,threshold,expected",
    [
        (0.90, 0.5, LABEL_FAKE),
        (0.10, 0.5, LABEL_REAL),
        (0.50, 0.5, LABEL_FAKE),   # boundary is inclusive
        (0.49, 0.5, LABEL_REAL),
        (0.80, 0.9, LABEL_REAL),   # a stricter threshold suppresses the alert
        (0.95, 0.9, LABEL_FAKE),
    ],
)
async def test_threshold_decides_the_label(score, threshold, expected):
    client = build(StubEngine([score]), fake_threshold=threshold)
    await client.connect()
    await client.send_audio(tone_pcm(4.0))
    messages = await collect(client, limit=1)
    assert messages[0].label == expected
    assert messages[0].aggregated_score == pytest.approx(score, abs=1e-3)
    await client.close()


async def test_inference_failure_becomes_unavailable_not_a_verdict():
    client = build(StubEngine([RuntimeError("model blew up")]))
    await client.connect()
    await client.send_audio(tone_pcm(4.0))
    messages = await collect(client, limit=1)
    assert len(messages) == 1
    assert messages[0].is_error
    assert messages[0].label is None
    await client.close()


# --- aggregation ---------------------------------------------------------


async def test_final_aggregates_analysed_windows():
    client = build(StubEngine([0.9, 0.8, 0.7]))
    await client.connect()
    await client.send_audio(tone_pcm(12.0))
    await collect(client, limit=3)
    await client.send_end()
    tail = await collect(client, limit=3)
    final = [m for m in tail if m.is_final][0]
    assert final.label == LABEL_FAKE
    assert final.aggregated_score == pytest.approx((0.9 + 0.8 + 0.7) / 3, abs=1e-3)
    assert final.consistency == 100.0


async def test_final_with_nothing_analysable_has_no_label():
    client = build(StubEngine([0.9]))
    await client.connect()
    await client.send_audio(silence_pcm(4.0))
    await collect(client, limit=1)
    await client.send_end()
    tail = await collect(client, limit=3)
    final = [m for m in tail if m.is_final][0]
    assert final.label is None, "no analysable audio must not produce a verdict"


async def test_consistency_reflects_disagreement():
    client = build(StubEngine([0.9, 0.1, 0.9, 0.9]))
    await client.connect()
    await client.send_audio(tone_pcm(16.0))
    messages = await collect(client, limit=4)
    # 3 fake of 4 analysed -> majority share is 75%.
    assert messages[-1].consistency == pytest.approx(75.0)


async def test_trailing_partial_window_is_analysed_if_long_enough():
    engine = StubEngine([0.9, 0.9])
    client = build(engine)
    await client.connect()
    await client.send_audio(tone_pcm(6.0))  # one full window + 2 s remainder
    await collect(client, limit=1)
    await client.send_end()
    await collect(client, limit=4)
    # The 2 s tail is >= half a window, so it was analysed.
    assert engine.calls == 2
    assert engine.window_lengths[1] == 32000


async def test_trailing_sliver_is_discarded():
    engine = StubEngine([0.9, 0.9])
    client = build(engine)
    await client.connect()
    await client.send_audio(tone_pcm(4.5))  # 0.5 s tail, under half a window
    await collect(client, limit=1)
    await client.send_end()
    await collect(client, limit=4)
    assert engine.calls == 1


# --- lifecycle -----------------------------------------------------------


async def test_closed_client_refuses_audio():
    client = build(StubEngine([0.9]))
    await client.connect()
    await client.close()
    with pytest.raises(DetectorUnavailable):
        await client.send_audio(tone_pcm(0.1))


async def test_connect_reports_unavailable_when_torch_is_missing(monkeypatch):
    """A missing dependency must surface as DETECTOR_UNAVAILABLE."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("torch", "transformers"):
            raise ImportError(f"no module named {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(ModelEngine, "_cache", {})

    with pytest.raises(DetectorUnavailable, match="torch and transformers"):
        ModelEngine.load("some/model", "cpu")


def test_the_adapter_is_not_flagged_as_a_mock():
    client = build(StubEngine([0.9]))
    assert client.is_mock is False
    assert client.provider_name == "local-model"


# --- backpressure regression --------------------------------------------


async def test_a_burst_of_windows_is_not_dropped_as_overload():
    """Regression: an earlier version used put_nowait and dropped on QueueFull.

    A single send_audio carrying many windows fills the pending queue before
    the worker task has been scheduled even once, so windows were discarded as
    "overloaded" while the model sat idle. Every window must be analysed.
    """
    engine = StubEngine([0.9] * 20)
    client = build(engine)
    await client.connect()

    # 40 s of audio in one call: 10 windows, far more than the queue depth.
    await client.send_audio(tone_pcm(40.0))
    messages = await collect(client, limit=10)

    assert len(messages) == 10
    assert engine.calls == 10, "every window must reach the model"
    assert client._overload_drops == 0
    assert not any(m.is_skipped for m in messages)
    starts = [m.window_start_s for m in messages]
    assert starts == [float(i * 4) for i in range(10)], "no gaps in the timeline"
    await client.close()


async def test_a_genuinely_slow_model_reports_unanalysed_windows():
    """When inference really cannot keep up, say so rather than guess."""

    class SlowEngine(StubEngine):
        def classify(self, samples):
            import time

            time.sleep(0.12)
            return super().classify(samples)

    engine = SlowEngine([0.9] * 20)
    # A 0.5 s window with a 0.12 s model leaves little headroom.
    client = build(engine, window_seconds=0.5)
    await client.connect()
    await client.send_audio(tone_pcm(3.0))
    messages = await collect(client, limit=20, timeout=8.0)

    # Whatever the split, nothing may be labelled without being analysed.
    labelled = [m for m in messages if m.label is not None]
    assert engine.calls >= len(labelled)
    for message in messages:
        if message.label is None:
            assert message.is_skipped
    await client.close()


async def test_final_label_is_a_majority_vote_not_a_mean_threshold():
    """Regression: the summary used to contradict its own window labels.

    With scores [0.96, 0.95, 0.97, 0.97, 0.84, 0.98, 0.52, 0.97] and a 0.9
    threshold, 6 of 8 windows are fake but the mean is 0.8934 -- just under
    the threshold -- so a mean-vs-threshold rule reported "real" for audio it
    had already alerted on twice.
    """
    scores = [0.96, 0.95, 0.97, 0.97, 0.84, 0.98, 0.52, 0.97]
    client = build(StubEngine(scores), fake_threshold=0.9)
    await client.connect()
    await client.send_audio(tone_pcm(32.0))
    chunks = await collect(client, limit=8)
    await client.send_end()
    tail = await collect(client, limit=4)

    fake_windows = sum(1 for c in chunks if c.label == LABEL_FAKE)
    assert fake_windows == 6
    mean = sum(scores) / len(scores)
    assert mean < 0.9, "the mean must be below threshold for this to be a real test"

    final = [m for m in tail if m.is_final][0]
    assert final.label == LABEL_FAKE, "majority of windows were fake"
    assert final.aggregated_score == pytest.approx(mean, abs=1e-3)


async def test_a_tie_produces_no_final_verdict():
    client = build(StubEngine([0.99, 0.01]), fake_threshold=0.9)
    await client.connect()
    await client.send_audio(tone_pcm(8.0))
    await collect(client, limit=2)
    await client.send_end()
    tail = await collect(client, limit=4)
    final = [m for m in tail if m.is_final][0]
    assert final.label is None
