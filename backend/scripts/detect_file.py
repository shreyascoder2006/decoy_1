#!/usr/bin/env python3
"""Stream a WAV file through the running backend exactly as a live call would.

This is the first rung of the test ladder. It exercises the whole server-side
stack -- session creation, the WebSocket protocol, the detector adapter, the
windowing, the alert policy and the latency measurement -- without Chrome,
without WhatsApp and without spending call minutes.

Use it to answer the question that matters before any live demo: does the
detector flag *your* AI samples at all? If it cannot flag a clean file, it will
not flag the same voice after WhatsApp's noise suppression and Opus codec.

The file is converted to the wire format (mono, 16 kHz, signed 16-bit
little-endian) in pure Python, so no ffmpeg is required for plain PCM WAVs.
For anything else (mp3, m4a, float WAV), convert first:

    ffmpeg -i input.mp3 -ar 16000 -ac 1 -c:a pcm_s16le output.wav

Usage:
    python scripts/detect_file.py sample.wav
    python scripts/detect_file.py sample.wav --fast
    python scripts/detect_file.py ai.wav --expect fake
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import sys
import time
import wave

import aiohttp

TARGET_RATE = 16000
FRAME_MS = 100
SAMPLES_PER_FRAME = TARGET_RATE * FRAME_MS // 1000


# ---------------------------------------------------------------------------
# WAV -> wire format
# ---------------------------------------------------------------------------


def load_wav_as_mono16k(path: str) -> tuple[list[float], int]:
    """Return (mono float samples at 16 kHz, original sample rate)."""
    with wave.open(path, "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)

    if width == 2:
        fmt, scale = "h", 32768.0
    elif width == 1:
        # 8-bit WAV is unsigned.
        samples = [(b - 128) / 128.0 for b in raw]
        return _to_mono_and_resample(samples, channels, rate), rate
    elif width == 4:
        fmt, scale = "i", 2147483648.0
    else:
        raise SystemExit(
            f"unsupported sample width {width * 8}-bit. Convert with:\n"
            f"  ffmpeg -i {path} -ar 16000 -ac 1 -c:a pcm_s16le converted.wav"
        )

    count = len(raw) // width
    values = struct.unpack(f"<{count}{fmt}", raw[: count * width])
    samples = [v / scale for v in values]
    return _to_mono_and_resample(samples, channels, rate), rate


def _to_mono_and_resample(samples: list[float], channels: int, rate: int) -> list[float]:
    if channels > 1:
        mono = [
            sum(samples[i : i + channels]) / channels
            for i in range(0, len(samples) - channels + 1, channels)
        ]
    else:
        mono = samples
    if rate == TARGET_RATE:
        return mono
    return _resample_linear(mono, rate, TARGET_RATE)


def _resample_linear(samples: list[float], src: int, dst: int) -> list[float]:
    """Linear interpolation, mirroring the extension's AudioWorklet."""
    if not samples:
        return []
    ratio = src / dst
    out: list[float] = []
    position = 0.0
    limit = len(samples) - 1
    while position < limit:
        index = int(position)
        frac = position - index
        a = samples[index]
        b = samples[index + 1]
        out.append(a + (b - a) * frac)
        position += ratio
    return out


def to_frames(samples: list[float]) -> list[bytes]:
    """Clamp, convert to s16le, and cut into 100 ms frames."""
    frames = []
    for start in range(0, len(samples) - SAMPLES_PER_FRAME + 1, SAMPLES_PER_FRAME):
        chunk = samples[start : start + SAMPLES_PER_FRAME]
        ints = [int(max(-1.0, min(1.0, v)) * 32767) for v in chunk]
        frames.append(struct.pack(f"<{len(ints)}h", *ints))
    return frames


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", help="path to a WAV file")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--realtime", action="store_true", default=True)
    parser.add_argument("--fast", dest="realtime", action="store_false",
                        help="stream as fast as possible rather than at 1x. "
                             "Quicker, but against a local model it can outrun "
                             "inference and trip backpressure; 1x is the "
                             "representative test")
    parser.add_argument("--expect", choices=["fake", "real"], default=None,
                        help="assert the final label, for scripted checks")
    args = parser.parse_args()

    try:
        samples, original_rate = load_wav_as_mono16k(args.wav)
    except FileNotFoundError:
        print(f"FAIL: no such file: {args.wav}")
        return 1
    except wave.Error as exc:
        print(f"FAIL: not a readable PCM WAV ({exc}). Convert with ffmpeg:")
        print(f"  ffmpeg -i {args.wav} -ar 16000 -ac 1 -c:a pcm_s16le converted.wav")
        return 1

    frames = to_frames(samples)
    duration = len(samples) / TARGET_RATE
    print(f"file:      {args.wav}")
    print(f"audio:     {duration:.2f}s, {original_rate} Hz -> {TARGET_RATE} Hz mono")
    print(f"frames:    {len(frames)} x {FRAME_MS} ms")
    if duration < 4.0:
        print("WARNING:   under 4 s. One analysis window is ~4 s, so this may")
        print("           produce no verdict at all.")
    print("-" * 70)

    events: list[dict] = []

    async with aiohttp.ClientSession() as http:
        try:
            async with http.get(f"{args.base}/health",
                                timeout=aiohttp.ClientTimeout(total=5)) as response:
                health = (await response.json())["live_call"]
        except (aiohttp.ClientError, asyncio.TimeoutError):
            print(f"FAIL: cannot reach the backend at {args.base}")
            print("  cd backend && PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8000")
            return 1

        print(f"detector:  {health['provider']} (mock={health['mock_mode']})")
        if health["mock_mode"]:
            print("WARNING:   MOCK MODE. Verdicts below are scripted and mean nothing.")
        print("-" * 70)

        async with http.post(
            f"{args.base}/api/live-calls",
            json={"source": "whatsapp_web_tab", "audio_format": "pcm_s16le",
                  "sample_rate": TARGET_RATE, "channels": 1},
        ) as response:
            if response.status != 201:
                print(f"FAIL: session creation returned HTTP {response.status}")
                return 1
            session = await response.json()

        async with http.ws_connect(session["websocket_url"]) as ws:
            async def reader() -> None:
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    event = json.loads(message.data)
                    events.append(event)
                    kind = event.get("type")
                    if kind == "detector_result":
                        window = ""
                        if event.get("window_start_s") is not None:
                            window = (f" window={event['window_start_s']:.1f}"
                                      f"-{event['window_end_s']:.1f}s")
                        score = event.get("aggregated_score")
                        print(f"  {event.get('result_kind','chunk'):<6} "
                              f"label={str(event.get('label')):<6} "
                              f"score={'n/a' if score is None else f'{score:.4f}'}"
                              f"{window} chunk={event.get('chunk_label')}")
                    elif kind == "alert":
                        print(f"  ALERT  {event['code']} "
                              f"(provisional={event['provisional']}, "
                              f"latency={event.get('latency_seconds')}s)")
                    elif kind == "error":
                        print(f"  ERROR  {event['code']}: {event['message']}")
                    elif kind == "status" and event["status"] in (
                        "ready", "inconclusive", "no_synthetic_signal"
                    ):
                        print(f"  status {event['status']}"
                              f"{' - ' + event['detail'] if event.get('detail') else ''}")

            reader_task = asyncio.create_task(reader())

            await ws.send_str(json.dumps({
                "type": "start", "sample_rate": TARGET_RATE, "channels": 1,
                "encoding": "pcm_s16le", "frame_ms": FRAME_MS,
            }))

            # Wait for the detector to be ready (model load can take seconds).
            for _ in range(1200):
                if any(e.get("status") == "ready" for e in events):
                    break
                if any(e.get("type") == "error" for e in events):
                    print("-" * 70)
                    print("RESULT: detector unavailable; no verdict produced.")
                    reader_task.cancel()
                    return 2
                await asyncio.sleep(0.05)

            await ws.send_str(json.dumps({"type": "speech_start"}))
            started = time.monotonic()
            for frame in frames:
                await ws.send_bytes(frame)
                if args.realtime:
                    await asyncio.sleep(FRAME_MS / 1000)
            await ws.send_str(json.dumps({"type": "stop"}))

            try:
                await asyncio.wait_for(reader_task, timeout=90)
            except asyncio.TimeoutError:
                reader_task.cancel()

    # --- report -----------------------------------------------------------
    print("-" * 70)
    verdicts = [e for e in events if e.get("type") == "detector_result"]
    alerts = [e for e in events if e.get("type") == "alert"]
    errors = [e for e in events if e.get("type") == "error"]
    finals = [v for v in verdicts if v.get("result_kind") == "final"]
    chunks = [v for v in verdicts if v.get("result_kind") != "final"]

    analysed = [c for c in chunks if c.get("label")]
    skipped = [c for c in chunks if not c.get("label")]
    print(f"windows: {len(chunks)} total, {len(analysed)} analysed, {len(skipped)} skipped")
    if analysed:
        scores = [c["aggregated_score"] for c in analysed if c.get("aggregated_score") is not None]
        fake_windows = sum(1 for c in analysed if c["label"] == "fake")
        print(f"labelled fake: {fake_windows}/{len(analysed)}")
        if scores:
            print(f"p_fake: min={min(scores):.4f} max={max(scores):.4f} "
                  f"mean={sum(scores)/len(scores):.4f}")
    final_label = finals[-1].get("label") if finals else None
    print(f"final label: {final_label}")
    print(f"alerts raised: {len(alerts)}")

    # Distinguish a detector failure from a transport hiccup. BACKPRESSURE
    # means frames were dropped because audio arrived faster than the model
    # could consume it -- which --fast guarantees against a local model. It
    # does not invalidate the verdicts that did arrive.
    fatal = [e for e in errors if e.get("code") == "DETECTOR_UNAVAILABLE"]
    transport = [e for e in errors if e.get("code") != "DETECTOR_UNAVAILABLE"]
    if transport:
        print(f"NOTE: transport error(s): {[e.get('code') for e in transport]}")
        print("      With --fast and a local model this is expected; the model")
        print("      cannot consume audio faster than real time. Re-run without")
        print("      --fast for a representative result.")
    if fatal:
        print("RESULT: detection unavailable.")
        return 2
    if not analysed:
        print("RESULT: nothing analysable. Too quiet, or too short for a window.")
        return 3
    if args.expect:
        if final_label == args.expect:
            print(f"RESULT: PASS (expected {args.expect})")
            return 0
        print(f"RESULT: FAIL (expected {args.expect}, got {final_label})")
        return 4
    print("RESULT: pipeline produced a verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
