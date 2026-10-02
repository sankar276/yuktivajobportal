"""Reading the mailbox: the connection is verified, and a bad server is only a mailbox problem."""

from __future__ import annotations

import ssl
from typing import Any

import pytest
from pydantic import SecretStr

from jobportal.inbox import imap
from jobportal.inbox.imap import InboxError, fetch_new
from jobportal.settings import Settings
from tests.conftest import NOW


class FakeServer:
    """Stands in for ``imaplib.IMAP4_SSL``; scripted per test."""

    created: list[dict[str, Any]] = []
    uidvalidity: bytes | None = b"7"
    sizes: dict[int, int] = {}
    fetched_bodies: list[int] = []

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        FakeServer.created.append({"host": host, "port": port, **kwargs})

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly is True
        return "OK", [b"3"]

    def response(self, name: str) -> tuple[str, list[bytes | None]]:
        return "OK", [FakeServer.uidvalidity]

    def status(self, folder: str, what: str) -> tuple[str, list[bytes]]:
        return "OK", [b"INBOX (UIDVALIDITY 7)"]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            return "OK", [b" ".join(str(uid).encode() for uid in sorted(FakeServer.sizes))]
        if "RFC822.SIZE" in args[1]:
            return "OK", [
                f"{index} (UID {uid} RFC822.SIZE {size})".encode()
                for index, (uid, size) in enumerate(sorted(FakeServer.sizes.items()), 1)
            ]
        uid = int(args[0])
        FakeServer.fetched_bodies.append(uid)
        return "OK", [(b"1 (BODY[] {5}", b"hello"), b")"]

    def logout(self) -> None:
        return None


@pytest.fixture
def mailbox(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> type[FakeServer]:
    settings.imap_host = "imap.example.com"
    settings.imap_username = "me@example.com"
    settings.imap_password = SecretStr("app-password")
    FakeServer.created, FakeServer.fetched_bodies = [], []
    FakeServer.uidvalidity, FakeServer.sizes = b"7", {11: 500, 12: 900}
    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", FakeServer)
    return FakeServer


def test_the_mail_server_certificate_is_verified(
    settings: Settings, mailbox: type[FakeServer]
) -> None:
    fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)
    context = mailbox.created[0]["ssl_context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


def test_oversized_messages_are_skipped_without_being_downloaded(
    settings: Settings, mailbox: type[FakeServer]
) -> None:
    mailbox.sizes = {11: 500, 12: imap.MAX_MESSAGE_BYTES + 1, 13: 800}
    fetched = fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)
    assert mailbox.fetched_bodies == [11, 13]
    assert fetched.messages == [(11, b"hello"), (12, b""), (13, b"hello")]


@pytest.mark.parametrize("junk", [b"abc", b"7 7", b"\xff"])
def test_a_nonsense_answer_is_a_mailbox_error_not_a_crash(
    settings: Settings, mailbox: type[FakeServer], junk: bytes
) -> None:
    mailbox.uidvalidity = junk
    with pytest.raises(InboxError, match="unusable answer"):
        fetch_new(settings, uidvalidity=None, last_uid=None, now=NOW)
