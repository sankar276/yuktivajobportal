"""Outgoing mail: build the message, then send it over SMTP as you.

SMTP with an app password works with Gmail, Outlook and nearly everything
else, and keeps the app free of provider-specific OAuth plumbing. Mail goes
out from your own account, so replies land in your own inbox.
"""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from jobportal.config import EMAIL_RE
from jobportal.settings import Settings
from jobportal.text import squash

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class MailError(Exception):
    """Mail could not be built or sent. The message is safe to show."""


@dataclass
class OutgoingMail:
    sender: str
    sender_name: str
    to: str
    subject: str
    body: str
    attachments: list[Path] = field(default_factory=list)
    bcc: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    references: str = ""

    def recipients(self) -> list[str]:
        return list(dict.fromkeys([self.to, *self.bcc]))


def _address(value: str, what: str) -> str:
    value = value.strip()
    if not EMAIL_RE.match(value) or any(char in value for char in ",;<>\r\n"):
        raise MailError(f"{what} is not a single valid email address: {value!r}")
    return value


def build_message(mail: OutgoingMail) -> EmailMessage:
    sender = _address(mail.sender, "sender")
    recipient = _address(mail.to, "recipient")
    for extra in mail.bcc:
        _address(extra, "bcc")
    subject = squash(mail.subject)  # no line breaks can reach a header
    if not subject:
        raise MailError("the message has no subject")
    if not mail.body.strip():
        raise MailError("the message has no body")

    message = EmailMessage()
    message["From"] = formataddr((squash(mail.sender_name), sender))
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    if mail.in_reply_to:
        message["In-Reply-To"] = squash(mail.in_reply_to)
        message["References"] = squash(mail.references or mail.in_reply_to)
    # Bcc is never written as a header; it only goes into the SMTP envelope.
    message.set_content(mail.body.replace("\r\n", "\n"))

    for path in mail.attachments:
        if not path.is_file():
            raise MailError(f"attachment is missing: {path}")
        guessed, _encoding = mimetypes.guess_type(path.name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        message.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )
    return message


class MailTransport(Protocol):
    def send(self, message: EmailMessage, recipients: list[str]) -> None: ...


class SmtpTransport:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, message: EmailMessage, recipients: list[str]) -> None:
        settings = self.settings
        if not settings.smtp_host:
            raise MailError("outgoing mail is not configured (set JOBPORTAL_SMTP_HOST)")
        security = settings.smtp_security.lower()
        host, port = settings.smtp_host, settings.smtp_port
        context = ssl.create_default_context()
        try:
            if security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
            with server:
                if security == "starttls":
                    server.starttls(context=context)
                elif security not in ("ssl", "none"):
                    raise MailError(f"unknown JOBPORTAL_SMTP_SECURITY: {settings.smtp_security!r}")
                if settings.smtp_username:
                    if security == "none" and host not in _LOOPBACK:
                        raise MailError("refusing to send a password to a mail server without TLS")
                    password = (
                        settings.smtp_password.get_secret_value() if settings.smtp_password else ""
                    )
                    server.login(settings.smtp_username, password)
                refused = server.send_message(message, to_addrs=recipients)
        except smtplib.SMTPException as exc:
            raise MailError(f"the mail server refused the message: {exc}") from exc
        except OSError as exc:
            raise MailError(f"could not reach the mail server {host}:{port}: {exc}") from exc
        if refused:
            raise MailError(f"the mail server refused these recipients: {', '.join(refused)}")


def save_draft(message: EmailMessage, directory: Path, name: str) -> Path:
    """Write the message as an .eml draft you can open in a mail client and send."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.eml"
    # Outlook and Thunderbird open a file carrying this header as an editable draft.
    path.write_bytes(b"X-Unsent: 1\n" + message.as_bytes())
    return path
