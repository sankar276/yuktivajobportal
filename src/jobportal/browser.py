"""One place that starts Chromium (for PDF rendering and for application forms).

The browser is launched exactly as Playwright ships it. No stealth plugins, no
fingerprint spoofing, no automation-flag removal: if a site checks for
automation it is entitled to see it, and the form flow hands such pages to you.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from playwright.sync_api import Browser, Error, sync_playwright

from jobportal.settings import Settings, get_settings

log = logging.getLogger(__name__)

#: WebRTC would otherwise send UDP straight to any address a page names,
#: around the proxy that every other request of the form filler goes through.
CHROMIUM_ARGS = ("--force-webrtc-ip-handling-policy=disable_non_proxied_udp",)
#: Set once the sandbox has failed to start here, so it is not tried again each time.
_sandbox_unusable = False


class BrowserUnavailable(RuntimeError):
    """Chromium could not be started. The message says how to install it."""


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def start_chromium(playwright: Any, settings: Settings, *, headless: bool | None = None) -> Browser:
    """Launch Chromium, inside its own sandbox wherever that is possible.

    With ``chromium_sandbox`` on ``auto`` the sandbox is tried first; where it
    cannot start (as root, in most containers) Chromium runs without it and
    the log says so once. ``true`` never falls back.
    """
    global _sandbox_unusable
    wanted = settings.chromium_sandbox
    if wanted == "auto":
        # Chromium refuses to sandbox itself as root; do not pay for the failed start.
        attempts = [False] if (_sandbox_unusable or _is_root()) else [True, False]
    else:
        attempts = [bool(wanted)]
    failure: Error | None = None
    for sandbox in attempts:
        try:
            browser: Browser = playwright.chromium.launch(
                headless=settings.headless if headless is None else headless,
                executable_path=settings.chromium_path or None,
                chromium_sandbox=sandbox,
                args=list(CHROMIUM_ARGS),
            )
        except Error as exc:
            failure = failure or exc
            continue
        if wanted == "auto" and not sandbox and not _sandbox_unusable:
            _sandbox_unusable = True
            log.warning(
                "Chromium's own sandbox cannot start on this system (running as root, in a "
                "container, or on a kernel that restricts it), so pages are opened without "
                "it. Set JOBPORTAL_CHROMIUM_SANDBOX=true to refuse to run that way."
            )
        return browser
    assert failure is not None
    hint = (
        " The sandbox is required (JOBPORTAL_CHROMIUM_SANDBOX=true) and may be what failed."
        if wanted is True
        else ""
    )
    raise BrowserUnavailable(
        "Chromium could not be started. Install it with `playwright install chromium` "
        f"or set JOBPORTAL_CHROMIUM_PATH.{hint} ({failure.message.splitlines()[0]})"
    ) from failure


@contextmanager
def launch_browser(
    settings: Settings | None = None, *, headless: bool | None = None
) -> Iterator[Browser]:
    settings = settings or get_settings()
    with sync_playwright() as playwright:
        browser = start_chromium(playwright, settings, headless=headless)
        try:
            yield browser
        finally:
            browser.close()


class LazyBrowser:
    """Starts Chromium on first use and reuses it until closed.

    Playwright's sync API cannot be started twice on one thread, so a run that
    renders resumes *and* opens forms shares a single instance through this.
    Use it from one thread only.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        headless: bool | None = None,
        browser: Browser | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._headless = headless
        self._browser = browser  # an already-running browser to borrow (never closed here)
        self._manager: AbstractContextManager[Browser] | None = None

    def get(self) -> Browser:
        if self._browser is None:
            self._manager = launch_browser(self._settings, headless=self._headless)
            self._browser = self._manager.__enter__()
        return self._browser

    def close(self) -> None:
        manager, self._manager = self._manager, None
        if manager is not None:
            self._browser = None
            manager.__exit__(None, None, None)

    def __enter__(self) -> LazyBrowser:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
