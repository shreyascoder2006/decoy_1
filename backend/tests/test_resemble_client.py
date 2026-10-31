"""Provider adapter tests: URL construction, framing, and the mock detector."""

from __future__ import annotations

import json
import urllib.parse

import pytest

from app.live_call.audio_format import (
    WAV_HEADER_BYTES,
    AudioContract,
    streaming_wav_header,
)
from app.live_call.resemble_client import (
    DetectorUnavailable,
    MockDetectorClient,
    ResembleStreamingClient,
    _decode_text_frame,
)

CONTRACT = AudioContract()
FRAME = b"\x01\x02" * 1600  # 100 ms


class FakeUpstreamSocket:
    """Captures what the adapter would put on the wire."""

    def __init__(self):
        self.binary: list[bytes] = []
        self.text: list[str] = []
        self.closed = False

    async def send_bytes(self, payload: bytes) -> None:
        self.binary.append(payload)

    async def send_str(self, payload: str) -> None:
        self.text.append(payload)

    async def close(self) -> None:
        self.closed = True


def build_client(**overrides) -> ResembleStreamingClient:
    kwargs = {
        "api_key": "test-key-not-real",
        "session_id": "abc123",
        "endpoint": "wss://stream.resemble.ai/api/v1/detect/audio",
        "contract": CONTRACT,
    }
    kwargs.update(overrides)
    return ResembleStreamingClient(**kwargs)


# --- construction and URL ------------------------------------------------


def test_missing_api_key_refuses_to_construct():
    with pytest.raises(DetectorUnavailable):
        build_client(api_key="")


def test_upstream_url_encodes_extra_params_json():
    url = build_client()._upstream_url()
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "wss"
    assert parsed.netloc == "stream.resemble.ai"
    assert parsed.path == "/api/v1/detect/audio"

    query = urllib.parse.parse_qs(parsed.query)
    assert json.loads(query["extra_params"][0]) == {"session_id": "abc123"}
    # The raw JSON braces/quotes must be percent-encoded, not inlined.
    assert '{"session_id"' not in parsed.query


def test_upstream_url_appends_to_an_existing_query():
    client = build_client(endpoint="wss://example.test/detect?mode=live")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(client._upstream_url()).query)
    assert query["mode"] == ["live"]
    assert json.loads(query["extra_params"][0])["session_id"] == "abc123"


def test_url_never_contains_the_api_key():
    url = build_client(api_key="super-secret-key-value")._upstream_url()
    assert "super-secret-key-value" not in url


# --- framing -------------------------------------------------------------


async def test_wav_header_is_prepended_to_the_first_frame_only():
    client = build_client()
    socket = FakeUpstreamSocket()
    client._ws = socket  # type: ignore[assignment]

    await client.send_audio(FRAME)
    await client.send_audio(FRAME)
    await client.send_audio(FRAME)

    assert len(socket.binary) == 3
    first = socket.binary[0]
    assert len(first) == WAV_HEADER_BYTES + len(FRAME)
    assert first[:WAV_HEADER_BYTES] == streaming_wav_header(16000, 1)
    assert first[WAV_HEADER_BYTES:] == FRAME

    # Later frames are raw PCM with no header.
    for payload in socket.binary[1:]:
        assert payload == FRAME
        assert not payload.startswith(b"RIFF")


async def test_empty_frame_is_not_sent_and_does_not_consume_the_header():
    client = build_client()
    socket = FakeUpstreamSocket()
    client._ws = socket  # type: ignore[assignment]

    await client.send_audio(b"")
    assert socket.binary == []
    await client.send_audio(FRAME)
    assert socket.binary[0].startswith(b"RIFF")


async def test_send_audio_without_a_socket_raises_unavailable():
    with pytest.raises(DetectorUnavailable):
        await build_client().send_audio(FRAME)


async def test_end_marker_is_a_json_text_frame():
    client = build_client()
    socket = FakeUpstreamSocket()
    client._ws = socket  # type: ignore[assignment]

    await client.send_end()
    assert [json.loads(t) for t in socket.text] == [{"type": "end"}]
    # The socket is deliberately left open to await the final result.
    assert socket.closed is False


async def test_send_end_on_a_closed_stream_is_a_no_op():
    client = build_client()
    await client.send_end()  # must not raise


# --- frame decoding ------------------------------------------------------


def test_decode_valid_frame():
    message = _decode_text_frame('{"type":"chunk","label":"fake"}')
    assert message.is_chunk and message.label == "fake"


def test_decode_malformed_json_is_inert():
    assert _decode_text_frame("not json at all").type is None


def test_decode_json_array_is_inert():
    assert _decode_text_frame("[1,2,3]").type is None


# --- mock detector -------------------------------------------------------


async def test_mock_emits_ready_then_a_window_per_four_seconds():
    mock = MockDetectorClient(
        session_id="s", contract=CONTRACT, labels=("skipped", "fake", "fake")
    )
    await mock.connect()
    assert (await mock.receive()).is_ready

    # 4 s of audio == 40 frames of 100 ms.
    for _ in range(40):
        await mock.send_audio(FRAME)
    first = await mock.receive()
    assert first.is_chunk
    assert first.is_skipped
    assert first.label is None
    assert first.window_start_s == 0.0
    assert first.window_end_s == 4.0

    for _ in range(40):
        await mock.send_audio(FRAME)
    second = await mock.receive()
    assert second.label == "fake"
    assert second.window_start_s == 4.0


async def test_mock_is_clearly_flagged_as_a_mock():
    mock = MockDetectorClient(session_id="s", contract=CONTRACT)
    assert mock.is_mock is True
    assert mock.provider_name == "mock"


async def test_mock_emits_a_final_then_ends():
    mock = MockDetectorClient(session_id="s", contract=CONTRACT, labels=("fake",))
    await mock.connect()
    await mock.receive()  # ready
    for _ in range(40):
        await mock.send_audio(FRAME)
    await mock.receive()  # chunk
    await mock.send_end()
    final = await mock.receive()
    assert final.is_final
    assert await mock.receive() is None


async def test_mock_refuses_audio_after_close():
    mock = MockDetectorClient(session_id="s", contract=CONTRACT)
    await mock.close()
    with pytest.raises(DetectorUnavailable):
        await mock.send_audio(FRAME)
