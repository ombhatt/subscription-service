"""Telling a person about something a job found but must not fix itself.

Email over plain SMTP, so any provider works and nothing new is installed. The
interface is one method, so a Slack webhook or a pager can stand in later
without reconcile knowing.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings, get_settings


class Notifier(Protocol):
    async def send(self, subject: str, body: str) -> None:
        """Deliver, or raise. Returning means the message was accepted."""
        ...


class EmailNotifier:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.recipients = [a.strip() for a in settings.alert_email_to.split(",") if a.strip()]

    async def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._s.alert_email_from
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(body)
        # smtplib is synchronous; a worker thread keeps it off the event loop.
        await asyncio.to_thread(self._deliver, msg)

    def _deliver(self, msg: EmailMessage) -> None:
        s = self._s
        tls = ssl.create_default_context()
        if s.smtp_port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds, context=tls
            )
        else:
            smtp = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
        with smtp:
            if s.smtp_port != 465 and s.smtp_starttls:
                smtp.starttls(context=tls)
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)


def email_notifier() -> EmailNotifier | None:
    """The configured notifier, or None when email is not set up."""
    s = get_settings()
    if not (s.smtp_host and s.alert_email_to.strip() and s.alert_email_from):
        return None
    return EmailNotifier(s)
