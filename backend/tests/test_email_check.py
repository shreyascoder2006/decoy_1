"""Tests for the email phishing-signal scorer and its HTTP endpoint.

These prove each rule fires on what it should and stays quiet on ordinary mail.
They say nothing about how well the rules catch real-world phishing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.email_check.schemas import EmailAnalyzeRequest
from app.email_check.scorer import registrable_domain, score_email
from app.main import create_app


def req(**kwargs) -> EmailAnalyzeRequest:
    return EmailAnalyzeRequest.model_validate(kwargs)


def codes(request: EmailAnalyzeRequest) -> set[str]:
    return {f.code for f in score_email(request)[2]}


def test_ordinary_mail_is_low_with_no_findings():
    score, level, findings = score_email(
        req(
            sender_name="Priya Sharma",
            sender_email="priya@example.com",
            subject="Lunch on Friday?",
            body="Are you free for lunch on Friday? See the menu: https://example.com/menu",
            links=[{"text": "the menu", "href": "https://example.com/menu"}],
        )
    )
    assert (score, level, findings) == (0, "low", [])


def test_empty_message_is_unknown_not_low():
    assert score_email(req())[1] == "unknown"


def test_brand_name_from_unrelated_domain_is_flagged():
    assert "SENDER_BRAND_MISMATCH" in codes(
        req(sender_name="PayPal Support", sender_email="help@secure-mail.biz", body="hi")
    )


def test_genuine_brand_domain_is_not_flagged():
    assert "SENDER_BRAND_MISMATCH" not in codes(
        req(sender_name="PayPal", sender_email="service@paypal.com", body="hi")
    )


def test_display_name_spoofing_another_address():
    assert "SENDER_NAME_SPOOFS_ADDRESS" in codes(
        req(sender_name="billing@paypal.com", sender_email="x@evil.test", body="hi")
    )


def test_link_text_that_differs_from_destination():
    result = codes(
        req(
            body="Sign in",
            links=[{"text": "https://paypal.com/login", "href": "https://paypa1-login.com/x"}],
        )
    )
    assert "LINK_TEXT_MISMATCH" in result


def test_matching_link_text_is_fine():
    assert "LINK_TEXT_MISMATCH" not in codes(
        req(
            body="Sign in",
            links=[{"text": "www.paypal.com", "href": "https://www.paypal.com/login"}],
        )
    )


def test_google_redirect_wrapper_is_unwrapped():
    wrapped = "https://www.google.com/url?q=https%3A%2F%2Fevil.test%2Fx&sa=D"
    assert "LINK_TEXT_MISMATCH" in codes(
        req(body="x", links=[{"text": "paypal.com", "href": wrapped}])
    )


def test_ip_shortener_punycode_http_and_tld():
    result = codes(
        req(
            body="x",
            links=[
                {"text": "a", "href": "http://192.168.4.4/login"},
                {"text": "b", "href": "https://bit.ly/abc"},
                {"text": "c", "href": "https://xn--pypal-4ve.com/"},
                {"text": "d", "href": "https://free-gift.zip/"},
            ],
        )
    )
    assert {"LINK_IP_HOST", "LINK_SHORTENER", "LINK_PUNYCODE", "LINK_SUSPICIOUS_TLD", "LINK_NOT_HTTPS"} <= result


def test_attachments():
    assert "ATTACHMENT_DOUBLE_EXTENSION" in codes(req(attachments=["invoice.pdf.exe"]))
    assert "ATTACHMENT_EXECUTABLE" in codes(req(attachments=["setup.exe"]))
    assert "ATTACHMENT_MACRO" in codes(req(attachments=["report.xlsm"]))
    assert codes(req(attachments=["report.pdf"])) == set()


def test_text_rules():
    result = codes(
        req(
            subject="URGENT: verify your account",
            body="Send the OTP and use our new bank details. Buy gift cards.",
        )
    )
    assert {"URGENCY_LANGUAGE", "CREDENTIAL_REQUEST", "PAYMENT_REQUEST"} <= result


@pytest.mark.parametrize(
    "phrase", ["Your account is suspended", "account was locked", "account has been disabled"]
)
def test_account_suspension_phrasings(phrase):
    assert "URGENCY_LANGUAGE" in codes(req(body=phrase))


def test_pressure_wording_with_a_genuine_link_stays_below_high():
    # A link that really goes where it says is not a mismatch, so wording
    # alone must not reach "high".
    score, level, _ = score_email(
        req(
            subject="Urgent !!! Your account is suspended",
            body="Click this link to complete the payment immediately",
            links=[{"text": "https://paypal.com/login", "href": "https://paypal.com/login"}],
        )
    )
    assert level != "high"


def test_classic_phish_reaches_high_and_score_is_capped():
    score, level, findings = score_email(
        req(
            sender_name="PayPal Security",
            sender_email="alert@mail-secure.top",
            subject="Final notice: account suspended",
            body="Verify your account immediately and enter your password.",
            links=[{"text": "https://paypal.com", "href": "http://198.51.100.7/p"}],
            attachments=["statement.pdf.exe"],
        )
    )
    assert level == "high"
    assert score == 100
    assert findings == sorted(findings, key=lambda f: f.points, reverse=True)


@pytest.mark.parametrize(
    "host,expected",
    [("mail.example.com", "example.com"), ("a.b.example.co.uk", "example.co.uk"), ("x.com", "x.com")],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


def test_endpoint_round_trip_and_validation():
    client = TestClient(create_app())
    ok = client.post(
        "/api/email/analyze",
        json={"sender_name": "PayPal", "sender_email": "a@evil.test", "body": "hello"},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["level"] == "medium"
    assert body["findings"][0]["code"] == "SENDER_BRAND_MISMATCH"
    assert "cannot prove" in body["disclaimer"]

    assert client.post("/api/email/analyze", json={"body": "x" * 20_001}).status_code == 422
    assert client.post("/api/email/analyze", json={"surprise": 1}).status_code == 422


def test_endpoint_does_not_log_message_content(caplog):
    caplog.set_level("INFO", logger="app.email_check")
    TestClient(create_app()).post(
        "/api/email/analyze",
        json={"sender_email": "secret@private.test", "subject": "TOPSECRETSUBJECT", "body": "TOPSECRETBODY"},
    )
    assert "TOPSECRET" not in caplog.text
    assert "private.test" not in caplog.text
