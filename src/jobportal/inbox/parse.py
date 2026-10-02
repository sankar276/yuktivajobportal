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
#: How much of a body is looked at before it is cleaned up and cut to size.
MAX_RAW_CHARS = 200_000
#: A subject longer than this is cut before it is read.
MAX_SUBJECT_CHARS = 1000
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
# "Delivery Status Notification (Delay)": still being retried, not a failure.
_DELAY_SUBJECT_RE = re.compile(r"\bdelay(?:ed)?\b|\bwarning\b|still (?:being )?retr", re.IGNORECASE)
_DSN_ACTION_RE = re.compile(r"(?im)^action\s*:\s*([a-z-]+)")
# Control characters carry no meaning in a requirement and make text handling slow.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_AUTH_RESULT_RE = re.compile(r"\b(dmarc|spf|dkim)\s*=\s*([a-z]+)", re.IGNORECASE)
# What vendors write instead of naming the end client. Anything matching is
# treated as "client not named", which keeps the double-submission guard honest.
_GENERIC_ORG = (
    r"(?:client|customer|company|firm|bank|retailer|insurer|provider|organi[sz]ation|enterprise|"
    r"corporation|institution|carrier|airline|agency|vendor|manufacturer|brand|giant|player|"
    r"leader|major|conglomerate|startup|start-up)"
)
_NO_CLIENT_RE = re.compile(
    r"\b(?:confidential|undisclosed|not\s+disclosed|non[- ]?disclos\w*"
    r"|to\s+be\s+(?:disclosed|shared|announced|confirmed|decided)"
    r"|will\s+(?:be\s+)?(?:disclos|shar)\w*|(?:up)?on\s+(?:request|selection|submission|interview)"
    r"|(?:direct|end|our|my|the)\s+client|implementation\s+partner|prime\s+vendor"
    r"|fortune\s*\d+|top\s*\d+|big\s*(?:four|4|three|3|five|5)"
    r"|(?:leading|major|large|global|reputed|prestigious|well[- ]known|premier|top|big)\s+"
    rf"(?:[\w&-]+\s+){{0,3}}{_GENERIC_ORG}"
    r"|[\w&-]+(?:\s+[\w&-]+)?\s+(?:client|customer|domain|major|giant))\b"
    r"|^(?:an?|one\s+of)\s|^(?:tb[dac]|n/?a|none|unknown|nil)$",
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
    #: A report that our message could not be delivered (not a "still trying" notice).
    is_bounce: bool = False
    raw_text: str = ""  # headers + body as text, for finding quoted message ids
    #: Where the sender asks replies to go, when that differs from the From address.
    reply_to: str = ""
    #: Your mail provider recorded that the sender checks (DMARC, or SPF and
    #: DKIM) failed: the From address may be forged.
    auth_failed: bool = False


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
            content = str(plain.get_content())[:MAX_RAW_CHARS]
            return normalize_text(_CONTROL_RE.sub(" ", content))
        except (LookupError, UnicodeDecodeError):
            pass
    html = message.get_body(preferencelist=("html",))
    if html is not None:
        try:
            content = str(html.get_content())[:MAX_RAW_CHARS]
            return html_to_text(_CONTROL_RE.sub(" ", content))
        except (LookupError, UnicodeDecodeError):
            pass
    return ""


def _is_bounce(message: EmailMessage, sender: str, subject: str) -> bool:
    """A delivery *failure* report. Delay notices and receipts are not bounces."""
    report = message.get_content_type() == "multipart/report"
    looks_like_one = (
        report
        or sender.startswith(("mailer-daemon@", "postmaster@"))
        or bool(_BOUNCE_SUBJECT_RE.search(subject))
    )
    if not looks_like_one:
        return False
    if report:
        # A proper delivery status notification says what happened to each
        # recipient; only "failed" means the message is not going to arrive.
        actions: list[str] = []
        for part in message.walk():
            if part.get_content_type() == "message/delivery-status":
                actions += _DSN_ACTION_RE.findall(part.as_string())
        if actions:
            return any(action.lower() == "failed" for action in actions)
    return not _DELAY_SUBJECT_RE.search(subject)


def _auth_failed(message: EmailMessage) -> bool:
    """Did the receiving provider record a failed sender check?

    Only the top-most ``Authentication-Results`` header counts: it is the one
    the last server (your own provider) added. A forged header further down
    can only claim a *pass*, and a pass is never used to trust anything here.
    """
    headers = message.get_all("Authentication-Results") or []
    if not headers:
        return False
    results = {
        name.lower(): verdict.lower() for name, verdict in _AUTH_RESULT_RE.findall(str(headers[0]))
    }
    if "dmarc" in results:
        return results["dmarc"] == "fail"
    return results.get("spf") in ("fail", "softfail") and results.get("dkim") != "pass"


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
    bounce = _is_bounce(message, sender, subject)
    recipients = getaddresses([str(v) for v in message.get_all("To", [])])
    reply_to = parseaddr(str(message.get("Reply-To", "")))[1].strip().lower()
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
        raw_text=raw[: MAX_BODY_CHARS * 12].decode("utf-8", errors="replace")[: MAX_BODY_CHARS * 3],
        reply_to=reply_to if reply_to and reply_to != sender else "",
        auth_failed=_auth_failed(message),
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
        # The value runs to the end of its line and is trimmed afterwards; a
        # lazy match followed by "\s*$" would rescan a long run of blanks.
        match = re.search(rf"(?im)^\W{{0,4}}{label}\s*[:\-–]\s*(.+)$", text)
        if match:
            value = squash(match.group(1)).strip("*_ ")
            if value:
                return value[:200]
    return ""


def _title_from_subject(subject: str) -> str:
    subject = squash(subject[:MAX_SUBJECT_CHARS])  # no long runs of blanks for the patterns below
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


def client_name(value: str) -> str:
    """The end client as written, or ``""`` when the vendor did not really name one.

    "Confidential", "Direct Client", "Banking client", "Not disclosed" and the
    like are ways of *not* naming the client. Treating them as names would
    hide the fact that a double submission cannot be ruled out.
    """
    # Remarks in brackets are about the client, not part of its name.
    value = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", squash(value)).strip(" .:-–*_\"'")
    if not value or len(value) > 80 or not re.search(r"[A-Za-z0-9]", value):
        return ""
    if _NO_CLIENT_RE.search(value):
        return ""
    # A name is short and has no sentence in it.
    if len(value.split()) > 6 or re.search(r"[.!?;:]\s", value):
        return ""
    return value


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
    client = client_name(_labelled(text, r"end client", r"client(?: name)?", r"customer"))
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
