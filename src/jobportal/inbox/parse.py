"""Turn recruiter emails into requirements.

Vendors send contract requirements as free-form mail. This reads one message
and pulls out what the rest of the pipeline needs: the role, where, the terms,
the end client if named, and who to reply to. It is heuristic by nature, so
every field is optional and the original text is kept as the description.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from jobportal.comp import Comp, extract_comp
from jobportal.config import Employment
from jobportal.sources.base import infer_remote
from jobportal.text import html_to_text, normalize_text, squash

MAX_BODY_CHARS = 20_000
_FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "aol.com", "proton.me", "protonmail.com", "msn.com",
}  # fmt: skip
_SUBJECT_NOISE_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)*"
    r"(?:(?:urgent|immediate|hot|new|direct client)\s+)*"
    r"(?:(?:job\s+)?(?:requirement|opening|opportunity|position|role|need|hiring|req)s?\s*"
    r"(?:for|[:\-–|])?\s*)?",
    re.IGNORECASE,
)
_SUBJECT_TAIL_RE = re.compile(
    r"\s*(?:[|(\[]|\s[-–]\s|\bat\b|\bin\b|\s@\s|//).*$",
    re.IGNORECASE,
)
_SIGNALS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:job\s+)?(?:requirement|opening|opportunity)\b",
        r"\b(?:position|role|job title|title)\s*[:\-]",
        r"\blocation\s*[:\-]",
        r"\bduration\s*[:\-]|\b\d+\+?\s*months?\b",
        r"\b(?:c2c|corp[- ]to[- ]corp|w2|1099|contract(?:[- ]to[- ]hire)?)\b",
        r"\brate\s*[:\-]|\$\s?\d+\s*(?:/|per)\s*h(?:ou)?r",
        r"\b(?:job description|responsibilities|must have|required skills|mandatory skills)\b",
        r"\b(?:share|send)\b.{0,30}\b(?:updated )?resume\b",
        r"\bclient\s*[:\-]",
    )
]
_AUTO_SUBJECT_RE = re.compile(
    r"^\s*(?:automatic reply|auto[- ]?reply|out of (?:the )?office|autoreply)", re.IGNORECASE
)
_BOUNCE_SUBJECT_RE = re.compile(
    r"undeliver|delivery (?:status notification|failure|has failed)|returned to sender|"
    r"mail delivery (?:failed|subsystem)|failure notice",
    re.IGNORECASE,
)


@dataclass
class ParsedMail:
    message_id: str
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    from_name: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    subject: str = ""
    date: datetime | None = None
    text: str = ""
    auto_submitted: bool = False
    is_bounce: bool = False
    raw_text: str = ""  # headers + body as text, for finding quoted message ids


@dataclass
class Requirement:
    title: str
    vendor: str
    client: str = ""
    location: str = ""
    remote: bool | None = None
    employment_type: str | None = None
    duration: str = ""
    comp: Comp | None = None
    contact_name: str = ""
    contact_email: str = ""
    description: str = ""


# ------------------------------------------------------------------ parsing


def _body_text(message: EmailMessage) -> str:
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        try:
            return normalize_text(plain.get_content())
        except (LookupError, UnicodeDecodeError):
            pass
    html = message.get_body(preferencelist=("html",))
    if html is not None:
        try:
            return html_to_text(html.get_content())
        except (LookupError, UnicodeDecodeError):
            pass
    return ""


def parse_message(raw: bytes) -> ParsedMail:
    message = message_from_bytes(raw, policy=default_policy)
    assert isinstance(message, EmailMessage)
    from_name, from_addr = parseaddr(str(message.get("From", "")))
    date: datetime | None = None
    try:
        date = parsedate_to_datetime(str(message.get("Date")))
        if date.tzinfo is None:
            date = None
    except (TypeError, ValueError):
        date = None
    subject = squash(str(message.get("Subject", "")))
    auto = str(message.get("Auto-Submitted", "no")).strip().lower() not in ("", "no")
    sender = from_addr.lower()
    bounce = (
        sender.startswith(("mailer-daemon@", "postmaster@"))
        or message.get_content_type() == "multipart/report"
        or bool(_BOUNCE_SUBJECT_RE.search(subject))
    )
    recipients = getaddresses([str(v) for v in message.get_all("To", [])])
    return ParsedMail(
        message_id=squash(str(message.get("Message-ID", ""))),
        in_reply_to=squash(str(message.get("In-Reply-To", ""))),
        references=str(message.get("References", "")).split(),
        from_name=squash(from_name),
        from_addr=sender,
        to_addrs=[addr.lower() for _name, addr in recipients if addr],
        subject=subject,
        date=date,
        text=_body_text(message)[:MAX_BODY_CHARS],
        auto_submitted=auto or bool(_AUTO_SUBJECT_RE.search(subject)),
        is_bounce=bounce,
        raw_text=raw.decode("utf-8", errors="replace")[: MAX_BODY_CHARS * 3],
    )


# ---------------------------------------------------------- classification


def looks_like_requirement(mail: ParsedMail) -> bool:
    """Does this read like a recruiter describing a role? Needs several signals."""
    haystack = f"{mail.subject}\n{mail.text}"
    hits = sum(1 for pattern in _SIGNALS if pattern.search(haystack))
    return hits >= 3


# --------------------------------------------------------------- extraction


def _labelled(text: str, *labels: str) -> str:
    """The value of a ``Label: value`` line, for the first label that occurs."""
    for label in labels:
        match = re.search(rf"(?im)^\W{{0,4}}{label}\s*[:\-–]\s*(.+?)\s*$", text)
        if match:
            value = squash(match.group(1)).strip("*_ ")
            if value:
                return value[:200]
    return ""


def _title_from_subject(subject: str) -> str:
    cleaned = _SUBJECT_NOISE_RE.sub("", subject)
    cleaned = _SUBJECT_TAIL_RE.sub("", cleaned)
    return squash(cleaned).strip(" -:|,")[:200]


def _vendor_name(mail: ParsedMail) -> str:
    domain = mail.from_addr.rsplit("@", 1)[-1] if "@" in mail.from_addr else ""
    if not domain or domain in _FREEMAIL:
        return mail.from_name or domain
    label = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    return label.replace("-", " ").title()


def _employment(text: str) -> str | None:
    if re.search(r"\b(?:c2c|corp[- ]to[- ]corp|w2|1099|contract)\b", text, re.IGNORECASE):
        return Employment.contract.value
    if re.search(r"\b(?:full[- ]time|fte|permanent|direct hire)\b", text, re.IGNORECASE):
        return Employment.full_time.value
    return None


def extract_requirement(mail: ParsedMail) -> Requirement:
    text = mail.text
    title = _labelled(text, r"job title", r"position", r"role", r"title") or _title_from_subject(
        mail.subject
    )
    location = _labelled(text, r"(?:work |job )?location", r"work site")
    if not location:
        tail = re.search(
            r"(?:[(|\-–]\s*)(remote[^)|]*|[A-Z][a-zA-Z .]+,\s*[A-Z]{2})\s*\)?", mail.subject
        )
        location = squash(tail.group(1)) if tail else ""
    client = _labelled(text, r"end client", r"client(?: name)?", r"customer")
    if re.fullmatch(
        r"(?i)(confidential|tbd|n/?a|to be disclosed|will disclose.*|undisclosed)", client
    ):
        client = ""
    haystack = f"{mail.subject}\n{text}"
    return Requirement(
        title=title or "Untitled requirement",
        vendor=_vendor_name(mail),
        client=client,
        location=location,
        remote=infer_remote(f"{location} {mail.subject}"),
        employment_type=_employment(haystack),
        duration=_labelled(text, r"duration", r"contract length", r"term"),
        comp=extract_comp(haystack),
        contact_name=mail.from_name,
        contact_email=mail.from_addr,
        description=text,
    )
