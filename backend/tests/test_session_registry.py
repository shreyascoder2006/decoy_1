"""Registry lifecycle and WebSocket origin allowlist."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.live_call.audio_format import AudioContract
from app.live_call.router import origin_allowed
from app.live_call.session_manager import SessionRegistry


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_session_ids_are_unique_and_unguessable():
    registry = SessionRegistry(make_settings())
    ids = {registry.create(AudioContract()).session_id for _ in range(20)}
    assert len(ids) == 20
    for sid in ids:
        assert len(sid) >= 20
        assert not sid.isdigit()


def test_get_and_remove():
    registry = SessionRegistry(make_settings())
    session = registry.create(AudioContract())
    assert registry.get(session.session_id) is session
    registry.remove(session.session_id)
    assert registry.get(session.session_id) is None
    assert len(registry) == 0


def test_expired_sessions_are_swept():
    registry = SessionRegistry(make_settings(LIVE_CALL_SESSION_TIMEOUT_SECONDS=0))
    session = registry.create(AudioContract())
    # A zero timeout means the session is stale as soon as any time passes.
    session.created_at -= 1.0
    assert registry.get(session.session_id) is None
    assert len(registry) == 0


def test_registry_is_bounded():
    registry = SessionRegistry(make_settings(), max_sessions=3)
    for _ in range(3):
        registry.create(AudioContract())
    with pytest.raises(RuntimeError, match="too many"):
        registry.create(AudioContract())


def test_purge_returns_the_number_removed():
    registry = SessionRegistry(make_settings(LIVE_CALL_SESSION_TIMEOUT_SECONDS=3600))
    a = registry.create(AudioContract())
    registry.create(AudioContract())
    a.created_at -= 10_000
    assert registry.purge_expired() == 1
    assert len(registry) == 1


# --- origin allowlist ----------------------------------------------------

PATTERNS = ["chrome-extension://*", "http://127.0.0.1", "http://localhost"]


@pytest.mark.parametrize(
    "origin",
    [
        "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
        "http://127.0.0.1:8000",
        "http://localhost:3000",
        None,
        "",
    ],
)
def test_allowed_origins(origin):
    assert origin_allowed(origin, PATTERNS) is True


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://attacker.test",
        "moz-extension://abc",
        "http://127.0.0.1.evil.test",
    ],
)
def test_disallowed_origins(origin):
    assert origin_allowed(origin, PATTERNS) is False


def test_narrow_allowlist_blocks_other_extensions():
    patterns = ["chrome-extension://knownextensionidknownextensionid"]
    assert origin_allowed("chrome-extension://knownextensionidknownextensionid", patterns)
    assert not origin_allowed("chrome-extension://someotherextensionid", patterns)


def test_settings_parse_the_allowlist():
    settings = make_settings(LIVE_CALL_ALLOWED_ORIGINS="a://x, b://y ,")
    assert settings.allowed_origins == ["a://x", "b://y"]


# --- provider selection safety -------------------------------------------


def test_missing_key_does_not_silently_enable_the_mock():
    settings = make_settings(RESEMBLE_API_KEY="", LIVE_CALL_PROVIDER="resemble")
    assert settings.use_mock_provider is False


def test_mock_requires_explicit_opt_in():
    assert make_settings(LIVE_CALL_MOCK_MODE=True).use_mock_provider is True
    assert make_settings(LIVE_CALL_PROVIDER="mock").use_mock_provider is True


def test_public_base_url_defaults_to_loopback():
    assert make_settings().public_base_url == "ws://127.0.0.1:8000"
    assert (
        make_settings(LIVE_CALL_PUBLIC_BASE_URL="ws://host:9/").public_base_url
        == "ws://host:9"
    )


def test_frame_byte_limit_is_validated():
    with pytest.raises(ValueError):
        make_settings(LIVE_CALL_MAX_FRAME_BYTES=10)
