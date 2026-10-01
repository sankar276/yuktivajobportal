"""Read new mail over IMAP without changing anything in the mailbox.

The folder is opened read-only and messages are fetched with ``BODY.PEEK``,
so nothing is marked as read, moved or deleted. Point ``JOBPORTAL_IMAP_FOLDER``
at a label that only holds recruiter mail rather than at your whole inbox.
"""

from __future__ import annotations

import imaplib
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jobportal.settings import Settings

log = logging.getLogger(__name__)

FIRST_RUN_LOOKBACK = timedelta(days=14)
MAX_MESSAGES_PER_RUN = 200
MAX_MESSAGE_BYTES = 2_000_000
_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")


class InboxError(Exception):
    """The mailbox could not be read. The message is safe to show."""


@dataclass
class Fetched:
    uidvalidity: int
    #: ``(uid, raw message)`` in ascending uid order.
    messages: list[tuple[int, bytes]] = field(default_factory=list)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def fetch_new(
    settings: Settings, *, uidvalidity: int | None, last_uid: int | None, now: datetime
) -> Fetched:
    """Messages that arrived after ``last_uid`` (or in the last two weeks on first use)."""
    if not settings.imap_configured:
        raise InboxError(
            "incoming mail is not configured (set JOBPORTAL_IMAP_HOST, _USERNAME, _PASSWORD)"
        )
    assert settings.imap_host and settings.imap_username and settings.imap_password
    try:
        connection = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
    except OSError as exc:
        raise InboxError(f"could not reach the mail server {settings.imap_host}: {exc}") from exc
    try:
        try:
            connection.login(settings.imap_username, settings.imap_password.get_secret_value())
        except imaplib.IMAP4.error as exc:
            raise InboxError(f"the mail server refused the login: {exc}") from exc
        status, _data = connection.select(_quote(settings.imap_folder), readonly=True)
        if status != "OK":
            raise InboxError(f"mail folder {settings.imap_folder!r} could not be opened")
        _status, validity_data = connection.response("UIDVALIDITY")
        current = int(validity_data[0]) if validity_data and validity_data[0] else 0
        if not current:
            _status, raw_status = connection.status(_quote(settings.imap_folder), "(UIDVALIDITY)")
            found = _UIDVALIDITY_RE.search(raw_status[0] or b"") if raw_status else None
            current = int(found.group(1)) if found else 0

        if uidvalidity is not None and current == uidvalidity and last_uid:
            criteria = f"UID {last_uid + 1}:*"
        else:  # first run, or the server renumbered the folder
            criteria = f"SINCE {(now - FIRST_RUN_LOOKBACK):%d-%b-%Y}"
            last_uid = None
        status, found_uids = connection.uid("SEARCH", None, criteria)  # type: ignore[arg-type]
        if status != "OK":
            raise InboxError("the mail server rejected the search")
        uids = sorted(int(u) for u in (found_uids[0] or b"").split())
        if last_uid:
            uids = [uid for uid in uids if uid > last_uid]  # "n:*" always returns the last message
        fetched = Fetched(uidvalidity=current)
        for uid in uids[:MAX_MESSAGES_PER_RUN]:
            status, parts = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not parts:
                continue
            raw = next(
                (part[1] for part in parts if isinstance(part, tuple) and len(part) > 1), None
            )
            if isinstance(raw, bytes) and len(raw) <= MAX_MESSAGE_BYTES:
                fetched.messages.append((uid, raw))
            else:
                fetched.messages.append((uid, b""))  # too large or unreadable: skip, but move on
        return fetched
    except imaplib.IMAP4.error as exc:
        raise InboxError(f"the mail server reported an error: {exc}") from exc
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            connection.logout()
