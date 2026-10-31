"""Integration tests for the extension <-> backend WebSocket relay.

A scripted fake detector stands in for the provider so the transport, the
alert plumbing and the cleanup paths can be exercised deterministically. These
tests prove the plumbing only -- they say nothing about detection accuracy.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.live_call.resemble_client import DetectorClient, DetectorUnavailable
from app.live_call.schemas import ProviderMessage
from app.live_call.session_manager import SessionRegistry
from app.main import create_app

SILENT_FRAME = b"\x00\x00" * 1600  # 100 ms of 16 kHz mono s16le


def chunk(label, chunk_label=None, begin=0.0, end=4.0, score=0.91):
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


class FakeDetectorClient(DetectorClient):
    """Emits scripted provider messages, one per N forwarded audio frames."""

    provider_name = "fake"
    is_mock = True

    def __init__(self, script, frames_per_window=1, fail_on_connect=False):
        self._script = list(script)
        self._frames_per_window = frames_per_window
        self._fail_on_connect = fail_on_connect
        self._queue: asyncio.Queue = asyncio.Queue()
        self._frames = 0
        self.closed = False
        self.end_sent = False
        self.header_prefixed_frames: list[int] = []

    async def connect(self) -> None:
        if self._fail_on_connect:
            raise DetectorUnavailable("no provider entitlement in this environment")

    async def send_audio(self, pcm: bytes) -> None:
        if self.closed:
            raise DetectorUnavailable("closed")
        self._frames += 1
        self.header_prefixed_frames.append(len(pcm))
        if self._frames % self._frames_per_window == 0 and self._script:
            await self._queue.put(self._script.pop(0))

    async def send_end(self) -> None:
        self.end_sent = True
        await self._queue.put(None)

    async def receive(self):
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)


@contextlib.contextmanager
def harness(
    client_obj: DetectorClient, min_fake_windows: int = 1
) -> Iterator[tuple[TestClient, FakeDetectorClient]]:
    """Run the app with a scripted detector.

    ``min_fake_windows`` is explicit in every test rather than inherited from
    the production default, so a change to that default cannot silently turn
    these tests into a hang (which is exactly what happened once).
    """
    app = create_app()
    with TestClient(app) as test_client:
        app.state.live_call_client_factory = lambda session: client_obj
        app.state.live_call_registry = SessionRegistry(
            Settings(  # type: ignore[call-arg]
                _env_file=None,
                LIVE_CALL_ALERT_MIN_FAKE_WINDOWS=min_fake_windows,
            )
        )
        yield test_client, client_obj


def create_session(test_client: TestClient) -> dict:
    response = test_client.post(
        "/api/live-calls",
        json={
            "source": "whatsapp_web_tab",
            "audio_format": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def drain_until(socket, predicate, limit=30):
    """Collect events until one satisfies `predicate`. Returns all of them."""
    seen = []
    for _ in range(limit):
        event = socket.receive_json()
        seen.append(event)
        if predicate(event):
            return seen
    raise AssertionError(f"expected event never arrived; saw {seen}")


# --- session creation ---------------------------------------------------


def test_create_session_returns_random_id_and_ws_url():
    with harness(FakeDetectorClient([])) as (test_client, _):
        first = create_session(test_client)
        second = create_session(test_client)

    assert first["status"] == "created"
    assert first["session_id"] != second["session_id"]
    # Unguessable, not a sequential integer.
    assert len(first["session_id"]) >= 20
    assert first["websocket_url"].endswith(f"/ws/live-calls/{first['session_id']}")
    assert first["websocket_url"].startswith("ws://127.0.0.1:8000")


def test_create_session_rejects_wrong_audio_contract():
    with harness(FakeDetectorClient([])) as (test_client, _):
        response = test_client.post(
            "/api/live-calls",
            json={"source": "whatsapp_web_tab", "sample_rate": 48000},
        )
    assert response.status_code == 422


def test_health_reports_provider_configuration_without_the_key():
    with harness(FakeDetectorClient([])) as (test_client, _):
        body = test_client.get("/health").json()
    assert body["status"] == "ok"
    assert set(body["live_call"]) == {
        "provider",
        "mock_mode",
        "provider_key_configured",
        "sample_rate",
        "channels",
    }
    # The key itself is never exposed, only whether one is configured.
    assert isinstance(body["live_call"]["provider_key_configured"], bool)
    assert "api_key" not in str(body).lower().replace("provider_key_configured", "")


# --- happy path ----------------------------------------------------------


def test_fake_verdict_produces_detector_result_and_alert():
    detector = FakeDetectorClient([chunk("fake")], frames_per_window=2)
    with harness(detector, min_fake_windows=1) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            assert ws.receive_json()["status"] == "connecting_to_detector"
            ws.send_json(
                {
                    "type": "start",
                    "sample_rate": 16000,
                    "channels": 1,
                    "encoding": "pcm_s16le",
                    "frame_ms": 100,
                }
            )
            assert ws.receive_json()["status"] == "ready"

            for _ in range(2):
                ws.send_bytes(SILENT_FRAME)

            events = drain_until(ws, lambda e: e["type"] == "alert")

            statuses = [e for e in events if e["type"] == "status"]
            assert any(s["status"] == "analysing" for s in statuses)

            results = [e for e in events if e["type"] == "detector_result"]
            assert len(results) == 1
            assert results[0]["label"] == "fake"
            assert results[0]["window_start_s"] == 0.0
            assert results[0]["window_end_s"] == 4.0
            assert results[0]["provider"] == "fake"

            alert = [e for e in events if e["type"] == "alert"][0]
            assert alert["code"] == "POTENTIAL_SYNTHETIC_VOICE"
            assert alert["level"] == "high"
            assert alert["provisional"] is True
            assert "Potential AI-generated voice" in alert["title"]
            assert alert["latency_seconds"] >= 0.0

            ws.send_json({"type": "stop"})
            tail = drain_until(ws, lambda e: e.get("status") == "stopped")

    metrics = [e for e in tail if e["type"] == "metrics"][0]
    assert metrics["frames_received"] == 2
    assert metrics["verdicts_received"] == 1
    assert metrics["audio_seconds_sent"] == pytest.approx(0.2, abs=1e-6)
    assert metrics["alert_latency_seconds"] is not None
    assert detector.end_sent is True
    assert detector.closed is True


def test_real_verdict_is_reported_as_no_signal_not_verified():
    detector = FakeDetectorClient([chunk("real", score=0.02)])
    with harness(detector) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(SILENT_FRAME)

            events = drain_until(ws, lambda e: e.get("status") == "no_synthetic_signal")

    assert not [e for e in events if e["type"] == "alert"]
    status = [e for e in events if e.get("status") == "no_synthetic_signal"][0]
    assert "no synthetic signal detected" in status["detail"].lower()


def test_skipped_window_is_inconclusive():
    detector = FakeDetectorClient([chunk(None, chunk_label="skipped")])
    with harness(detector) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(SILENT_FRAME)
            events = drain_until(ws, lambda e: e.get("status") == "inconclusive")

    assert not [e for e in events if e["type"] == "alert"]
    status = [e for e in events if e.get("status") == "inconclusive"][0]
    assert status["reason"] == "insufficient_voice_activity"


def test_a_sustained_run_upgrades_the_alert():
    detector = FakeDetectorClient(
        [chunk("fake", begin=b * 4.0, end=(b + 1) * 4.0) for b in range(4)]
    )
    with harness(detector, min_fake_windows=1) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            for _ in range(4):
                ws.send_bytes(SILENT_FRAME)
            events = drain_until(
                ws, lambda e: e.get("code") == "SYNTHETIC_VOICE_EVIDENCE"
            )

    alerts = [e for e in events if e["type"] == "alert"]
    assert [a["code"] for a in alerts] == [
        "POTENTIAL_SYNTHETIC_VOICE",
        "SYNTHETIC_VOICE_EVIDENCE",
    ]
    assert alerts[0]["provisional"] is True
    assert alerts[1]["provisional"] is False


# --- failure paths -------------------------------------------------------


def test_provider_unavailable_reports_error_and_never_a_verdict():
    detector = FakeDetectorClient([], fail_on_connect=True)
    with harness(detector) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            assert ws.receive_json()["status"] == "connecting_to_detector"
            ws.send_json({"type": "start"})
            error = ws.receive_json()

    assert error["type"] == "error"
    assert error["code"] == "DETECTOR_UNAVAILABLE"
    assert "unavailable" in error["message"].lower()
    # The internal reason is not leaked to the browser.
    assert "entitlement" not in error["message"].lower()


def test_provider_error_midstream_is_unavailable_not_a_verdict():
    detector = FakeDetectorClient(
        [ProviderMessage.model_validate({"type": "error", "error_message": "no credits"})]
    )
    with harness(detector) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(SILENT_FRAME)
            events = drain_until(ws, lambda e: e["type"] == "error")

    error = [e for e in events if e["type"] == "error"][0]
    assert error["code"] == "DETECTOR_UNAVAILABLE"
    assert not [e for e in events if e["type"] == "alert"]
    assert not [e for e in events if e["type"] == "detector_result"]


def test_unknown_session_is_rejected_with_a_reason():
    with harness(FakeDetectorClient([])) as (test_client, _):
        with test_client.websocket_connect("/ws/live-calls/does-not-exist") as ws:
            event = ws.receive_json()
    assert event["type"] == "error"
    assert event["code"] == "UNKNOWN_SESSION"


def test_oversized_frame_is_rejected():
    detector = FakeDetectorClient([])
    with harness(detector) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(b"\x00" * 20000)  # over the 16384 byte limit
            events = drain_until(ws, lambda e: e.get("code") == "FRAME_TOO_LARGE")
    assert [e for e in events if e.get("code") == "FRAME_TOO_LARGE"]


def test_misaligned_frame_is_discarded_without_killing_the_session():
    detector = FakeDetectorClient([chunk("real")])
    with harness(detector) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(b"\x00\x01\x02")  # odd byte count -> not whole samples
            ws.send_bytes(SILENT_FRAME)
            events = drain_until(ws, lambda e: e["type"] == "detector_result")

    # Only the aligned frame was counted.
    assert [e for e in events if e["type"] == "detector_result"]
    assert detector.header_prefixed_frames == [len(SILENT_FRAME)]


def test_malformed_start_frame_is_rejected():
    with harness(FakeDetectorClient([])) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start", "sample_rate": 44100})
            error = ws.receive_json()
    assert error["code"] == "BAD_START_FRAME"


# --- cleanup -------------------------------------------------------------


def test_stop_cleans_up_the_backend_session():
    detector = FakeDetectorClient([])
    app = create_app()
    with TestClient(app) as test_client:
        app.state.live_call_client_factory = lambda session: detector
        session = create_session(test_client)
        assert len(app.state.live_call_registry) == 1
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_json({"type": "stop"})
            drain_until(ws, lambda e: e.get("status") == "stopped")

        assert len(app.state.live_call_registry) == 0
        assert detector.closed is True
        assert detector.end_sent is True
        # The session id is single-use: reconnecting is refused.
        assert test_client.get(f"/api/live-calls/{session['session_id']}").status_code == 404


def test_client_disconnect_cleans_up_the_provider():
    detector = FakeDetectorClient([])
    app = create_app()
    with TestClient(app) as test_client:
        app.state.live_call_client_factory = lambda session: detector
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(SILENT_FRAME)
            # Leaving the context manager closes the browser side abruptly.

        assert len(app.state.live_call_registry) == 0
    assert detector.closed is True


def test_status_endpoint_exposes_metrics_but_no_audio_or_key():
    detector = FakeDetectorClient([])
    app = create_app()
    with TestClient(app) as test_client:
        app.state.live_call_client_factory = lambda session: detector
        session = create_session(test_client)
        body = test_client.get(f"/api/live-calls/{session['session_id']}").json()

    assert body["session_id"] == session["session_id"]
    assert body["state"] == "created"
    assert body["frames_received"] == 0
    serialised = str(body).lower()
    for forbidden in ("bearer", "api_key", "authorization", "pcm"):
        assert forbidden not in serialised


def test_speech_start_mark_moves_the_latency_origin():
    detector = FakeDetectorClient([chunk("fake")])
    with harness(detector, min_fake_windows=1) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_json({"type": "speech_start"})
            ws.send_bytes(SILENT_FRAME)
            events = drain_until(ws, lambda e: e["type"] == "alert")

    alert = [e for e in events if e["type"] == "alert"][0]
    # Latency is measured from the speech mark, so it is small in a fast test.
    assert 0.0 <= alert["latency_seconds"] < 5.0


# --- false-alarm suppression --------------------------------------------


def test_a_single_fake_window_does_not_alert_under_the_default_policy():
    """The production default needs a run of two consecutive fake windows.

    The best benchmarked local model false-alarms on roughly 10% of
    individual 4 s windows. Alerting on one window would flag a genuine human
    caller most of the way through a one-minute call.
    """
    detector = FakeDetectorClient([chunk("fake"), chunk("real", begin=4.0, end=8.0)])
    with harness(detector, min_fake_windows=2) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(SILENT_FRAME)
            ws.send_bytes(SILENT_FRAME)
            ws.send_json({"type": "stop"})
            events = drain_until(ws, lambda e: e.get("status") == "stopped", limit=40)

    assert not [e for e in events if e["type"] == "alert"], "one window must not alert"
    # The user is told evidence is building, not that nothing was found.
    building = [e for e in events if e.get("reason") == "building_evidence"]
    assert building, "a sub-threshold signal should still be surfaced"
    assert "needed to warn" in building[0]["detail"]


def test_two_consecutive_fake_windows_do_alert():
    detector = FakeDetectorClient([chunk("fake"), chunk("fake", begin=4.0, end=8.0)])
    with harness(detector, min_fake_windows=2) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            ws.send_bytes(SILENT_FRAME)
            ws.send_bytes(SILENT_FRAME)
            events = drain_until(ws, lambda e: e["type"] == "alert")

    alert = [e for e in events if e["type"] == "alert"][0]
    assert alert["code"] == "POTENTIAL_SYNTHETIC_VOICE"
    assert alert["provisional"] is True


def test_isolated_false_positives_separated_by_a_clean_window_never_alert():
    """A clean window breaks the run, so scattered noise cannot accumulate."""
    detector = FakeDetectorClient(
        [
            chunk("fake", begin=0.0, end=4.0),
            chunk("real", begin=4.0, end=8.0),
            chunk("fake", begin=8.0, end=12.0),
            chunk("real", begin=12.0, end=16.0),
            chunk("fake", begin=16.0, end=20.0),
        ]
    )
    with harness(detector, min_fake_windows=2) as (test_client, _):
        session = create_session(test_client)
        with test_client.websocket_connect(f"/ws/live-calls/{session['session_id']}") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            ws.receive_json()
            for _ in range(5):
                ws.send_bytes(SILENT_FRAME)
            ws.send_json({"type": "stop"})
            events = drain_until(ws, lambda e: e.get("status") == "stopped", limit=60)

    assert not [e for e in events if e["type"] == "alert"]
