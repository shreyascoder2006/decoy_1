"""Tests for the pretrained-model integration in the email check.

A fake model stands in for the real one, so these prove the label handling, the
points maths, the cap, and the fail-open behaviour. They say nothing about how
accurate any real model is; that is what scripts/benchmark_email_models.py is for.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.email_check.model_client import ModelUnavailable, build_phishing_indexes, classify_label
from app.main import create_app

# The package re-exports an APIRouter named `router`, shadowing the submodule.
email_router = importlib.import_module("app.email_check.router")

PLAIN = {"subject": "Hello", "body": "Just checking in about the plan for next week, thanks."}
PHISHY_RULES = {
    "sender_name": "PayPal Security",
    "sender_email": "a@mail-secure.top",
    "subject": "Final notice",
    "body": "Verify your account immediately and enter your password.",
    "links": [{"text": "https://paypal.com", "href": "http://198.51.100.7/p"}],
}


class FakeModel:
    def __init__(self, p):
        self._p = p

    def p_phishing(self, text):
        return self._p


@pytest.fixture()
def configure(monkeypatch):
    def _configure(p=None, model_id="fake/model", error=None, **env):
        monkeypatch.setenv("EMAIL_MODEL_ID", model_id)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

        def fake_load(mid, override=""):
            if error is not None:
                raise error
            return FakeModel(p)

        monkeypatch.setattr(email_router, "load_model", fake_load)
        return TestClient(create_app())

    yield _configure
    get_settings.cache_clear()


def analyze(client, payload):
    return client.post("/api/email/analyze", json=payload).json()


def test_model_is_off_by_default():
    get_settings.cache_clear()
    body = analyze(TestClient(create_app()), PLAIN)
    assert body["model"]["status"] == "disabled"
    assert "MODEL_PHISHING_TEXT" not in {f["code"] for f in body["findings"]}


def test_model_finding_adds_scaled_points(configure):
    body = analyze(configure(p=0.95), PLAIN)
    finding = next(f for f in body["findings"] if f["code"] == "MODEL_PHISHING_TEXT")
    assert finding["points"] == 38  # round(40 * 0.95)
    assert "95%" in finding["message"]
    assert "cannot see hidden link targets" in finding["reason"]
    assert body["model"] == {"id": "fake/model", "p_phishing": 0.95, "status": "used", "detail": None}
    assert body["level"] == "medium"


def test_model_alone_can_never_reach_high(configure):
    body = analyze(configure(p=1.0), PLAIN)
    assert body["score"] == 40
    assert body["level"] == "medium"


def test_model_plus_rules_reaches_high(configure):
    rules_only = analyze(TestClient(create_app()), PHISHY_RULES)
    with_model = analyze(configure(p=0.9), PHISHY_RULES)
    assert with_model["raw_points"] == rules_only["raw_points"] + 36
    assert with_model["level"] == "high"


def test_below_threshold_adds_nothing(configure):
    body = analyze(configure(p=0.2), PLAIN)
    assert body["model"]["status"] == "below_threshold"
    assert body["level"] == "low"
    assert body["findings"] == []


def test_points_cap_and_threshold_are_configurable(configure):
    body = analyze(configure(p=0.8, EMAIL_MODEL_MAX_POINTS=20, EMAIL_MODEL_THRESHOLD=0.9), PLAIN)
    assert body["model"]["status"] == "below_threshold"
    # Environment values persist within a test, so reset the threshold explicitly.
    body = analyze(configure(p=0.8, EMAIL_MODEL_MAX_POINTS=20, EMAIL_MODEL_THRESHOLD=0.5), PLAIN)
    assert next(f for f in body["findings"] if f["code"] == "MODEL_PHISHING_TEXT")["points"] == 16


def test_unavailable_model_falls_back_to_rules_and_says_so(configure):
    client = configure(error=ModelUnavailable("could not load 'fake/model': OSError"))
    body = analyze(client, PHISHY_RULES)
    assert body["model"]["status"] == "unavailable"
    assert "could not load" in body["model"]["detail"]
    assert body["level"] == "high"  # rules still work
    assert "MODEL_PHISHING_TEXT" not in {f["code"] for f in body["findings"]}


def test_unexpected_inference_error_is_contained(configure):
    body = analyze(configure(error=RuntimeError("boom")), PLAIN)
    assert body["model"]["status"] == "unavailable"
    assert body["model"]["detail"] == "RuntimeError"  # type only, never the message


def test_tiny_messages_skip_the_model(configure):
    body = analyze(configure(p=0.99), {"subject": "hi", "body": "ok", "links": [{"text": "a", "href": "http://x.test"}]})
    assert body["model"]["status"] == "disabled"


def test_empty_message_does_not_call_the_model(configure):
    assert analyze(configure(p=0.99), {})["model"]["status"] == "disabled"


def test_chat_endpoint_does_not_use_the_email_model(configure):
    # Only EMAIL_MODEL_ID is set here: chat has its own model setting and stays off.
    client = configure(p=0.99)
    body = client.post("/api/chat/analyze", json=PLAIN).json()
    assert body["model"]["status"] == "disabled"
    assert "MODEL_PHISHING_TEXT" not in {f["code"] for f in body["findings"]}


def test_chat_model_is_separate_and_capped(configure):
    client = configure(p=0.97, CHAT_MODEL_ID="fake/chat-model", EMAIL_MODEL_ID="")
    body = client.post("/api/chat/analyze", json=PLAIN).json()
    assert body["model"]["id"] == "fake/chat-model"
    finding = next(f for f in body["findings"] if f["code"] == "MODEL_PHISHING_TEXT")
    assert finding["points"] == 39  # round(40 * 0.97)
    assert body["level"] == "medium"  # the model alone cannot reach high


def test_chat_model_unavailable_falls_back_to_rules(configure):
    client = configure(error=ModelUnavailable("no weights"), CHAT_MODEL_ID="fake/chat-model", EMAIL_MODEL_ID="")
    body = client.post(
        "/api/chat/analyze",
        json={"body": "You have won the lottery, claim your prize and verify your account immediately"},
    ).json()
    assert body["model"]["status"] == "unavailable"
    assert body["level"] in {"medium", "high"}  # rules still scored it


# -- label handling ------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("phishing", "pos"),
        ("Phishing Email", "pos"),
        ("spam", "pos"),
        ("unsafe", "pos"),
        ("legitimate", "neg"),
        ("ham", "neg"),
        ("safe", "neg"),
        ("not_phishing", "neg"),
        ("non-spam", "neg"),
        ("LABEL_1", None),
    ],
)
def test_classify_label(name, expected):
    assert classify_label(name) == expected


def test_informative_labels_are_used():
    assert build_phishing_indexes({0: "legitimate", 1: "phishing"}) == {1}
    assert build_phishing_indexes({0: "phishing", 1: "legitimate"}) == {0}


def test_generic_labels_are_refused_without_an_explicit_choice():
    with pytest.raises(ModelUnavailable):
        build_phishing_indexes({0: "LABEL_0", 1: "LABEL_1"})


def test_override_picks_the_phishing_indexes():
    assert build_phishing_indexes({0: "LABEL_0", 1: "LABEL_1"}, "1") == {1}
    assert build_phishing_indexes({0: "a", 1: "b", 2: "c", 3: "d"}, "1,3") == {1, 3}


@pytest.mark.parametrize("override", ["0,1", "5", ",", "x"])
def test_bad_override_is_refused(override):
    with pytest.raises((ModelUnavailable, ValueError)):
        build_phishing_indexes({0: "LABEL_0", 1: "LABEL_1"}, override)
