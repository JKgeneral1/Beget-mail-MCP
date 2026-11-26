import base64
import time
from email.message import EmailMessage

import pytest

from beget_mail_mcp.server import (
    Settings,
    _attachment_names,
    _make_confirmation_token,
    _message_text,
    _validate_email_content,
    _verify_confirmation_token,
)


SETTINGS = Settings(
    email="mailbox@example.com",
    password="unused",
    bearer_token="x" * 32,
)


def test_confirmation_token_is_bound_to_exact_message():
    token = _make_confirmation_token(
        SETTINGS, ["alice@example.com"], [], "Subject", "Hello"
    )
    _verify_confirmation_token(
        SETTINGS, token, ["alice@example.com"], [], "Subject", "Hello"
    )
    with pytest.raises(ValueError, match="changed"):
        _verify_confirmation_token(
            SETTINGS, token, ["alice@example.com"], [], "Changed", "Hello"
        )


def test_expired_confirmation_token_is_rejected(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000)
    token = _make_confirmation_token(
        SETTINGS, ["alice@example.com"], [], "Subject", "Hello"
    )
    monkeypatch.setattr(time, "time", lambda: 2000)
    with pytest.raises(ValueError, match="expired"):
        _verify_confirmation_token(
            SETTINGS, token, ["alice@example.com"], [], "Subject", "Hello"
        )


def test_header_injection_is_rejected():
    with pytest.raises(ValueError, match="line breaks"):
        _validate_email_content(
            ["victim@example.com\nBcc: attacker@example.com"], [], "Hello", "Body"
        )
    with pytest.raises(ValueError, match="line breaks"):
        _validate_email_content(
            ["victim@example.com"], [], "Hello\nBcc: attacker@example.com", "Body"
        )


def test_message_text_and_attachment_names():
    message = EmailMessage()
    message.set_content("Plain body")
    message.add_attachment(b"data", maintype="application", subtype="octet-stream", filename="a.bin")
    assert _message_text(message) == "Plain body\n"
    assert _attachment_names(message) == ["a.bin"]

