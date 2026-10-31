#!/usr/bin/env python3
"""Pre-download the pretrained models this project runs, so the first request is fast.

The repository stores no model weights (see docs/MODELS.md). Models normally
download on first use; this fetches them up front into the Hugging Face cache.
The IDs come from the same settings the backend reads (backend/.env), so what
is downloaded is exactly what will run.

Usage (from the repo root, with the project venv):
    .venv\\Scripts\\python.exe scripts\\download_models.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    try:
        from transformers import (  # noqa: PLC0415
            AutoFeatureExtractor,
            AutoModelForAudioClassification,
            AutoModelForSequenceClassification,
            AutoTokenizer,
        )

        from app.config import Settings  # noqa: PLC0415
    except ImportError as exc:
        print(f"missing dependency: {exc}\nInstall backend/requirements-local-model.txt first.")
        return 1

    # Read backend/.env wherever this is run from, so the IDs match the backend's.
    settings = Settings(_env_file=str(ROOT / "backend" / ".env"))  # type: ignore[call-arg]
    audio = settings.live_call_local_model_id
    text_ids = []
    for model_id in (settings.email_model_id, settings.chat_model_id):
        if model_id and model_id not in text_ids:
            text_ids.append(model_id)

    failures = 0
    print(f"audio model: {audio}")
    try:
        AutoFeatureExtractor.from_pretrained(audio)
        AutoModelForAudioClassification.from_pretrained(audio)
        print("  ready")
    except Exception as exc:  # noqa: BLE001 - network, disk, bad repo
        failures += 1
        print(f"  FAILED: {type(exc).__name__}: {exc}")

    for model_id in text_ids:
        print(f"text model:  {model_id}")
        try:
            AutoTokenizer.from_pretrained(model_id)
            AutoModelForSequenceClassification.from_pretrained(model_id)
            print("  ready")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAILED: {type(exc).__name__}: {exc}")

    if not text_ids:
        print("no EMAIL_MODEL_ID or CHAT_MODEL_ID set: text checks run on rules only")
    print("\nAll models ready." if not failures else f"\n{failures} model(s) failed to download.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
