"""Read new mail over IMAP without changing anything in the mailbox.

The folder is opened read-only and messages are fetched with ``BODY.PEEK``,
so nothing is marked as read, moved or deleted. Point ``JOBPORTAL_IMAP_FOLDER``
at a label that only holds recruiter mail rather than at your whole inbox.
"""

from __future__ import annotations

import imaplib
import logging
import re
import ssl
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


_SIZE_RE = re.compile(rb"UID (\d+)[^)]*?RFC822\.SIZE (\d+)|RFC822\.SIZE (\d+)[^)]*?UID (\d+)")


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
        # The default context checks the server's certificate and host name.
        # Without it anyone on the network path could take the password and
        # hand the app whatever "mail" they like.
        connection = imaplib.IMAP4_SSL(
            settings.imap_host,
            settings.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=30,
        )
    except (OSError, imaplib.IMAP4.error) as exc:
        raise InboxError(f"could not reach the mail server {settings.imap_host}: {exc}") from exc
    try:
        return _read(connection, settings, uidvalidity, last_uid, now)
    except InboxError:
        raise
    except (imaplib.IMAP4.error, OSError, ValueError, IndexError, TypeError) as exc:
        # A server (or something posing as one) that answers with nonsense is
        # a mailbox problem to report, never a reason to stop the whole pass.
        raise InboxError(
            f"the mail server gave an unusable answer: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        with suppress(imaplib.IMAP4.error, OSError):
            connection.logout()


def _read(
    connection: imaplib.IMAP4,
    settings: Settings,
    uidvalidity: int | None,
    last_uid: int | None,
    now: datetime,
) -> Fetched:
    assert settings.imap_username and settings.imap_password
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
    uids = uids[:MAX_MESSAGES_PER_RUN]
    fetched = Fetched(uidvalidity=current)
    if not uids:
        return fetched

    # Ask for the sizes first, so that an oversized message is never downloaded.
    sizes: dict[int, int] = {}
    status, size_data = connection.uid("FETCH", ",".join(map(str, uids)), "(RFC822.SIZE)")
    if status == "OK":
        for line in size_data or []:
            for match in _SIZE_RE.finditer(line if isinstance(line, bytes) else b""):
                uid, size = (
                    (match.group(1), match.group(2))
                    if match.group(1)
                    else (
                        match.group(4),
                        match.group(3),
                    )
                )
                sizes[int(uid)] = int(size)

    for uid in uids:
        if sizes.get(uid, MAX_MESSAGE_BYTES + 1) > MAX_MESSAGE_BYTES:
            # Too large, or the server would not say: skipped, but not read again.
            fetched.messages.append((uid, b""))
            continue
        status, parts = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        raw = None
        if status == "OK" and parts:
            raw = next(
                (part[1] for part in parts if isinstance(part, tuple) and len(part) > 1), None
            )
        if isinstance(raw, bytes) and len(raw) <= MAX_MESSAGE_BYTES:
            fetched.messages.append((uid, raw))
        else:
            fetched.messages.append((uid, b""))  # unreadable: skip, but move on
    return fetched
