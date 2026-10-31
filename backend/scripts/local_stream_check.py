#!/usr/bin/env python3
"""Stream synthetic PCM at real time into a running backend and report events.

This is the local integration check from the build specification: it proves
the transport, the framing, the alert plumbing and the latency measurement
against a real uvicorn process and a real WebSocket, rather than an in-process
test harness.

It does NOT prove detection accuracy. With the mock provider the verdicts are
scripted. With the real provider it proves the provider path works and reports
whatever the provider actually returned.

Usage:
    python scripts/local_stream_check.py --seconds 12
    python scripts/local_stream_check.py --base http://127.0.0.1:8000 --silent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import struct
import sys
import time

import aiohttp

SAMPLE_RATE = 16000
FRAME_MS = 100
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000


def make_frame(index: int, *, silent: bool = False, freq: float = 180.0) -> bytes:
    """One 100 ms frame of s16le PCM: a tone, or digital silence."""
    if silent:
        return b"\x00\x00" * SAMPLES_PER_FRAME
    samples = []
    for n in range(SAMPLES_PER_FRAME):
        t = (index * SAMPLES_PER_FRAME + n) / SAMPLE_RATE
        # A couple of harmonics so it is not a pure sine.
        value = 0.45 * math.sin(2 * math.pi * freq * t)
        value += 0.20 * math.sin(2 * math.pi * freq * 2.5 * t)
        samples.append(int(max(-1.0, min(1.0, value)) * 32767))
    return struct.pack(f"<{len(samples)}h", *samples)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--silent", action="store_true", help="stream silence instead of a tone")
    parser.add_argument("--realtime", action="store_true", default=True)
    parser.add_argument("--fast", dest="realtime", action="store_false",
                        help="stream as fast as possible instead of at 1x")
    args = parser.parse_args()

    frames_total = int(args.seconds * 1000 / FRAME_MS)
    events: list[dict] = []
    first_audio_at: float | None = None
    alert_at: float | None = None

    async with aiohttp.ClientSession() as http:
        # 1. health
        try:
            async with http.get(f"{args.base}/health", timeout=aiohttp.ClientTimeout(total=5)) as response:
                health = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            print(f"FAIL: cannot reach the backend at {args.base} ({type(exc).__name__}).")
            print("Start it with:")
            print("  cd backend && PYTHONPATH=. uvicorn app.main:app --host 127.0.0.1 --port 8000")
            return 1
        print(f"backend health: {json.dumps(health['live_call'])}")

        # 2. create the session
        async with http.post(
            f"{args.base}/api/live-calls",
            json={
                "source": "whatsapp_web_tab",
                "audio_format": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
                "channels": 1,
            },
        ) as response:
            if response.status != 201:
                print(f"FAIL: session creation returned HTTP {response.status}")
                return 1
            session = await response.json()
        print(f"session:        {session['session_id']}")
        print(f"provider:       {session['provider']} (mock={session['mock_mode']})")
        print(f"websocket:      {session['websocket_url']}")
        print("-" * 68)

        # 3. stream
        async with http.ws_connect(session["websocket_url"]) as ws:
            async def reader() -> None:
                nonlocal alert_at
                async for message in ws:
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    event = json.loads(message.data)
                    events.append(event)
                    stamp = time.monotonic()
                    kind = event.get("type")
                    if kind == "status":
                        extra = event.get("detail") or event.get("reason") or ""
                        print(f"[{stamp:9.3f}] status         {event['status']} {extra}")
                    elif kind == "detector_result":
                        print(
                            f"[{stamp:9.3f}] verdict        label={event.get('label')!r} "
                            f"score={event.get('aggregated_score')} "
                            f"window={event.get('window_start_s')}-{event.get('window_end_s')}s "
                            f"chunk={event.get('chunk_label')!r}"
                        )
                    elif kind == "alert":
                        if alert_at is None:
                            alert_at = stamp
                        print(
                            f"[{stamp:9.3f}] ALERT          {event['code']} "
                            f"level={event['level']} provisional={event['provisional']} "
                            f"latency={event.get('latency_seconds')}s"
                        )
                        print(f"                           \"{event['title']}\"")
                    elif kind == "error":
                        print(f"[{stamp:9.3f}] ERROR          {event['code']}: {event['message']}")
                    elif kind == "metrics":
                        # Null fields are omitted from the wire payload, so
                        # every optional key must be read defensively.
                        print(
                            f"[{stamp:9.3f}] metrics        frames={event['frames_received']} "
                            f"dropped={event['frames_dropped']} "
                            f"audio={event['audio_seconds_sent']}s "
                            f"verdicts={event['verdicts_received']} "
                            f"first_verdict={event.get('first_verdict_latency_seconds')}s "
                            f"alert={event.get('alert_latency_seconds')}s"
                        )

            reader_task = asyncio.create_task(reader())

            await ws.send_str(
                json.dumps(
                    {
                        "type": "start",
                        "sample_rate": SAMPLE_RATE,
                        "channels": 1,
                        "encoding": "pcm_s16le",
                        "frame_ms": FRAME_MS,
                    }
                )
            )

            # Wait for the backend to say the detector is ready.
            for _ in range(200):
                if any(e.get("status") == "ready" for e in events):
                    break
                if any(e.get("type") == "error" for e in events):
                    print("-" * 68)
                    print("RESULT: the detector was unavailable; no verdict was produced.")
                    reader_task.cancel()
                    return 2
                await asyncio.sleep(0.05)

            if not args.silent:
                await ws.send_str(json.dumps({"type": "speech_start"}))

            first_audio_at = time.monotonic()
            for index in range(frames_total):
                await ws.send_bytes(make_frame(index, silent=args.silent))
                if args.realtime:
                    await asyncio.sleep(FRAME_MS / 1000)

            await ws.send_str(json.dumps({"type": "stop"}))

            try:
                await asyncio.wait_for(reader_task, timeout=15)
            except asyncio.TimeoutError:
                reader_task.cancel()

    # 4. report
    print("-" * 68)
    alerts = [e for e in events if e.get("type") == "alert"]
    verdicts = [e for e in events if e.get("type") == "detector_result"]
    errors = [e for e in events if e.get("type") == "error"]
    metrics = [e for e in events if e.get("type") == "metrics"]

    print(f"verdicts received: {len(verdicts)}")
    print(f"alerts raised:     {len(alerts)}")
    print(f"errors:            {len(errors)}")
    if alert_at is not None and first_audio_at is not None:
        print(f"wall-clock first-audio-to-alert: {alert_at - first_audio_at:.3f} s")
    if metrics:
        print(f"backend alert latency:           {metrics[-1].get('alert_latency_seconds')} s")
        print(f"backend first-verdict latency:   {metrics[-1].get('first_verdict_latency_seconds')} s")
        if metrics[-1]["frames_dropped"]:
            print(f"WARNING: {metrics[-1]['frames_dropped']} frames were dropped")

    if errors:
        print("RESULT: detection unavailable (reported honestly, no verdict invented).")
        return 2
    if not verdicts:
        print("RESULT: no verdict arrived. Too little audio, or the provider skipped every window.")
        return 3
    print("RESULT: transport, verdict delivery and latency measurement all worked.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
