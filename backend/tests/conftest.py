from __future__ import annotations

import os
import sys
import pathlib

# Make `app` importable without installing the package.
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must never pick up a developer's real .env or real credential.
os.environ.setdefault("RESEMBLE_API_KEY", "")
os.environ.setdefault("LIVE_CALL_PROVIDER", "resemble")
os.environ.setdefault("LIVE_CALL_MOCK_MODE", "false")
# An empty value beats backend/.env, so the real email model is never loaded by
# the suite; tests that want a model opt in with a fake.
os.environ.setdefault("EMAIL_MODEL_ID", "")
os.environ.setdefault("CHAT_MODEL_ID", "")

import pytest  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402


@pytest.fixture()
def settings() -> Settings:
    get_settings.cache_clear()
    return Settings(_env_file=None)  # type: ignore[call-arg]
