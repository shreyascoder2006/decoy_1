"""Tests for the WhatsApp-facing checks: chat-text scam scoring, the live-call
alert evidence, and voice-note scoring.

They prove each rule and calculation behaves as described. They say nothing
about how well the detector or the rules perform on real WhatsApp traffic.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.email_check.schemas import EmailAnalyzeRequest
from app.email_check.scorer import REASONS, score_email
from app.live_call.alert_policy import AlertPolicy
from app.live_call.schemas import ProviderMessage
from app.main import create_app
from app.voice_check.scoring import WindowScore, longest_run, summarise

# The package re-exports an APIRouter named `router`, which shadows the
# submodule attribute, so the module has to be fetched explicitly to patch it.
voice_router = importlib.import_module("app.voice_check.router")


def chat(body, **kwargs):
    return EmailAnalyzeRequest.model_validate({"body": body, **kwargs})


# -- chat text ---------------------------------------------------------------


def test_chat_rules_only_apply_to_chat():
    text = "Congratulations, you have won the lottery! Claim your prize"
    assert "SCAM_OFFER" not in {f.code for f in score_email(chat(text))[2]}
    assert "SCAM_OFFER" in {f.code for f in score_email(chat(text), chat=True)[2]}


def test_family_impersonation_scam():
    findings = score_email(chat("Hi Mum, this is my new number, I lost my phone"), chat=True)[2]
    assert "FAMILY_IMPERSONATION" in {f.code for f in findings}


def test_ordinary_chat_is_low():
    score, level, findings, _ = score_email(chat("see you at 7, bring the charger"), chat=True)
    assert (score, level, findings) == (0, "low", [])


def test_every_rule_family_has_a_reason():
    text = (
        "Your account is suspended. Send your password. New bank details attached. "
        "You have won the lottery. Hi Mum, this is my new number."
    )
    _, _, findings, _ = score_email(
        chat(
            text,
            sender_name="PayPal billing@paypal.com",
            sender_email="x@evil.test",
            links=[
                {"text": "https://paypal.com", "href": "http://10.0.0.1/a"},
                {"text": "b", "href": "https://bit.ly/x"},
                {"text": "c", "href": "https://xn--pypal-4ve.com/"},
                {"text": "d", "href": "https://prize.zip/"},
            ],
            attachments=["a.pdf.exe", "b.exe", "c.xlsm"],
        ),
        chat=True,
    )
    assert {f.code for f in findings} == set(REASONS) - {"MODEL_PHISHING_TEXT"}
    assert all(f.reason for f in findings)


def test_whatsapp_link_wrapper_is_unwrapped():
    wrapped = "https://l.wl.co/l?u=https%3A%2F%2Fevil.test%2Flogin&h=AT0abc"
    assert "LINK_TEXT_MISMATCH" in {
        f.code
        for f in score_email(chat("x", links=[{"text": "paypal.com", "href": wrapped}]), chat=True)[2]
    }


def test_chat_endpoint_scores_and_does_not_log_content(caplog):
    caplog.set_level("INFO", logger="app.email_check")
    body = TestClient(create_app()).post(
        "/api/chat/analyze",
        json={
            "sender_name": "Unknown",
            "body": "TOPSECRETCHAT You have won the lottery, verify your account immediately",
            "links": [{"text": "claim", "href": "http://198.51.100.9/x"}],
        },
    ).json()
    assert body["level"] in {"medium", "high"}
    assert body["raw_points"] == sum(f["points"] for f in body["findings"])
    assert "TOPSECRETCHAT" not in caplog.text
    assert "198.51.100.9" not in caplog.text


# -- live-call alert evidence -------------------------------------------------


def window(label, score):
    return ProviderMessage.model_validate(
        {
            "type": "chunk",
            "label": label,
            "aggregated_score": score,
            "chunk_info": {"begin_timestamp_s": 0.0, "end_timestamp_s": 4.0, "chunk_label": label},
        }
    )


def test_alert_carries_risk_score_and_reasons():
    policy = AlertPolicy("s", "local-model", min_fake_windows=2)
    assert policy.evaluate(window("fake", 0.80)).alert is None
    alert = policy.evaluate(window("fake", 0.90)).alert
    assert alert is not None
    assert alert.risk_score == 85  # mean of 0.80 and 0.90
    assert any("2 analysis windows in a row" in r for r in alert.reasons)
    assert any("80%, 90%" in r and "not a calibrated probability" in r for r in alert.reasons)
    assert alert.reasons[-1].startswith("Why this matters")


def test_real_window_resets_the_scores_behind_the_alert():
    policy = AlertPolicy("s", "local-model", min_fake_windows=2)
    policy.evaluate(window("fake", 0.99))
    policy.evaluate(window("real", 0.01))
    policy.evaluate(window("fake", 0.60))
    alert = policy.evaluate(window("fake", 0.70)).alert
    assert alert.risk_score == 65  # only the run after the real window
    assert any("earlier window(s) were classified as human" in r for r in alert.reasons)


def test_alert_without_numeric_score_has_no_risk_score():
    policy = AlertPolicy("s", "resemble", min_fake_windows=1)
    alert = policy.evaluate(window("fake", None)).alert
    assert alert.risk_score is None
    assert alert.reasons


# -- voice-note scoring --------------------------------------------------------


def ws(*scores):
    return [WindowScore(i * 4.0, i * 4.0 + 4.0, s) for i, s in enumerate(scores)]


def test_skipped_windows_do_not_break_a_run():
    assert longest_run(ws(0.9, None, 0.9), 0.5) == 2


def test_voice_levels():
    kw = {"threshold": 0.5, "min_run": 2, "window_seconds": 4.0}
    assert summarise(ws(0.9, 0.95, 0.99), **kw).level == "high"
    assert summarise(ws(0.9, 0.1, 0.9), **kw).level == "medium"
    assert summarise(ws(0.1, 0.05), **kw).level == "low"
    assert summarise(ws(None, None), **kw).level == "unknown"


def test_voice_score_is_share_of_windows_flagged_and_explained():
    summary = summarise(ws(0.9, 0.9, 0.1, None), threshold=0.5, min_run=2, window_seconds=4.0)
    assert summary.score == 67  # 2 of 3 analysed
    assert summary.skipped == 1
    assert any("too quiet" in r for r in summary.reasons)


def test_low_voice_result_never_claims_genuine():
    summary = summarise(ws(0.1, 0.05), threshold=0.5, min_run=2, window_seconds=4.0)
    text = " ".join(summary.reasons).lower()
    assert "does not prove the voice is genuine" in text
    assert "verified" not in text


# -- voice-note endpoint -------------------------------------------------------


class FakeEngine:
    model_id = "fake/model"

    def __init__(self, p_fake):
        self._p = p_fake

    def classify(self, samples):
        class Result:
            p_fake = self._p

        return Result()


@pytest.fixture()
def local_app(monkeypatch):
    monkeypatch.setenv("LIVE_CALL_PROVIDER", "local")
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def loud_pcm(seconds):
    # A 440 Hz-ish square wave comfortably above the voice-activity floor.
    block = (b"\xff\x3f" * 20 + b"\x01\xc0" * 20) * (16000 * seconds // 40)
    return block


def test_voice_endpoint_flags_a_run_of_synthetic_windows(local_app, monkeypatch):
    monkeypatch.setattr(voice_router, "load_engine", lambda: FakeEngine(0.95))
    body = TestClient(local_app).post("/api/voice/analyze", content=loud_pcm(12)).json()
    assert body["level"] == "high"
    assert body["score"] == 100
    assert len(body["windows"]) == 3
    assert body["model_id"] == "fake/model"


def test_voice_endpoint_skips_silence(local_app, monkeypatch):
    monkeypatch.setattr(voice_router, "load_engine", lambda: FakeEngine(0.95))
    body = TestClient(local_app).post("/api/voice/analyze", content=b"\x00\x00" * 16000 * 8).json()
    assert body["level"] == "unknown"
    assert all(w["p_fake"] is None for w in body["windows"])


def test_voice_endpoint_validates_input(local_app):
    client = TestClient(local_app)
    assert client.post("/api/voice/analyze", content=b"").status_code == 422
    assert client.post("/api/voice/analyze", content=b"\x00\x00\x00").status_code == 422
    assert client.post("/api/voice/analyze", content=b"\x00\x00" * 16000 * 301).status_code == 413


def test_voice_endpoint_refuses_without_local_detector():
    get_settings.cache_clear()
    response = TestClient(create_app()).post("/api/voice/analyze", content=b"\x00\x00" * 16000)
    assert response.status_code == 503
