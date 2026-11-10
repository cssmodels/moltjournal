"""Plain-text email, for verification codes only.

Configured from SMTP_HOST, SMTP_PORT (587, STARTTLS; 465 means implicit TLS),
SMTP_USER, SMTP_PASSWORD and SMTP_FROM in the environment. Nothing else in the
journal sends email. Without a configuration, `configured()` is false and the
academic registration route refuses to start a registration it cannot finish.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("moltjournal.mail")


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def configured() -> bool:
    return bool(_env("SMTP_HOST") and _env("SMTP_FROM"))


def _send(message: EmailMessage) -> None:
    """The transport. Tests replace it with a mock SMTP server."""
    host, port = _env("SMTP_HOST"), int(_env("SMTP_PORT") or 587)
    user, password = _env("SMTP_USER"), _env("SMTP_PASSWORD")
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls(context=context)
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


def send(to: str, subject: str, body: str) -> bool:
    message = EmailMessage()
    message["From"] = _env("SMTP_FROM")
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    try:
        _send(message)
        return True
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never with the address
        log.warning("verification email could not be sent: %s", type(exc).__name__)
        return False
