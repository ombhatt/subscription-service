"""Operator email: configuration, and what is said to the SMTP server.

Delivery itself is not faked end to end here -- the SMTP conversation is
recorded -- because the failure modes worth a unit test are ours: sending with
half a configuration, skipping STARTTLS, or logging in when no user is set.
"""

from __future__ import annotations

import pytest

from app import notify
from app.config import Settings


def settings(**overrides) -> Settings:
    base = {
        "smtp_host": "smtp.example.test",
        "alert_email_to": "ops@example.test, cfo@example.test",
        "alert_email_from": "billing@example.test",
    }
    return Settings(**{**base, **overrides})


@pytest.mark.parametrize("missing", ["smtp_host", "alert_email_to", "alert_email_from"])
def test_email_is_off_unless_host_sender_and_recipient_are_all_set(monkeypatch, missing):
    monkeypatch.setattr(notify, "get_settings", lambda: settings(**{missing: ""}))
    assert notify.email_notifier() is None


def test_a_complete_configuration_gives_a_notifier_for_every_recipient(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda: settings())
    n = notify.email_notifier()
    assert n is not None and n.recipients == ["ops@example.test", "cfo@example.test"]


class Conversation:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what was asked of it."""

    def __init__(self):
        self.log: list[str] = []
        self.messages = []

    def factory(self, kind):
        conv = self

        class Server:
            def __init__(self, host, port, timeout=None, context=None):
                conv.log.append(f"{kind} {host}:{port}")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                conv.log.append("quit")

            def starttls(self, context=None):
                conv.log.append("starttls")

            def login(self, user, password):
                conv.log.append(f"login {user}")

            def send_message(self, msg):
                conv.messages.append(msg)
                conv.log.append("send")

        return Server


@pytest.fixture
def smtp(monkeypatch):
    conv = Conversation()
    monkeypatch.setattr(notify.smtplib, "SMTP", conv.factory("SMTP"))
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", conv.factory("SMTP_SSL"))
    return conv


async def test_port_587_upgrades_with_starttls_before_logging_in(smtp):
    await notify.EmailNotifier(settings(smtp_username="u", smtp_password="p")).send("S", "B")
    assert smtp.log == ["SMTP smtp.example.test:587", "starttls", "login u", "send", "quit"]
    [msg] = smtp.messages
    assert msg["To"] == "ops@example.test, cfo@example.test"
    assert msg["From"] == "billing@example.test" and msg["Subject"] == "S"


async def test_port_465_is_tls_from_the_first_byte(smtp):
    await notify.EmailNotifier(settings(smtp_port=465)).send("S", "B")
    assert smtp.log[0] == "SMTP_SSL smtp.example.test:465"
    assert "starttls" not in smtp.log


async def test_no_login_without_a_username(smtp):
    await notify.EmailNotifier(settings(smtp_starttls=False, smtp_port=1025)).send("S", "B")
    assert smtp.log == ["SMTP smtp.example.test:1025", "send", "quit"]
